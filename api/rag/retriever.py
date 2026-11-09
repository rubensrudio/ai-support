"""Retriever facade with pluggable retrieval modes (CT-30, DA-16).

Modes come from `api.config.RetrievalMode` (`none`, `fulltext`, `vector_bruteforce`,
`vector_hnsw`, `hybrid`); phase 03 implements `none` and `fulltext`, later phases register the
others with `register_mode`. Any search failure becomes `RetrievalUnavailable` (RAG-94); an
empty index yields `[]` (RAG-90).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.config import RetrievalMode, Settings
from api.messages import Lang
from api.observability.logging import log_event
from api.rag.fulltext import ScoredChunk, search_fulltext
from api.rag.models import Chunk, Document


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: int
    document_title: str
    doc_key: str
    section: str | None
    page: int | None
    language: Lang
    content: str
    score: float


class RetrievalUnavailable(RuntimeError):
    """Search or embedding failed; the chat answers with `KNOWLEDGE_UNAVAILABLE` (RAG-94)."""


Searcher = Callable[[AsyncSession, str, Lang, int], Awaitable[list[ScoredChunk]]]

# `none` never reaches the registry: it returns `[]` without opening a session.
_SEARCHERS: dict[RetrievalMode, Searcher] = {}


def register_mode(mode: RetrievalMode, searcher: Searcher) -> None:
    """Register (or replace) the searcher used for `mode`."""
    if mode == "none":
        raise ValueError("Mode 'none' does not take a searcher")
    _SEARCHERS[mode] = searcher


register_mode("fulltext", search_fulltext)


class Retriever:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedder: object | None,
        settings: Settings,
    ) -> None:
        self.session_factory = session_factory
        self.embedder = embedder
        self.settings = settings

    async def retrieve(
        self,
        query: str,
        language: Lang,
        k: int | None = None,
        mode: RetrievalMode | None = None,
    ) -> list[RetrievedChunk]:
        """`k` defaults to `settings.retrieval_top_k`, `mode` to `settings.retrieval_mode`."""
        mode = mode or self.settings.retrieval_mode
        limit = self.settings.retrieval_top_k if k is None else k
        if mode == "none" or limit <= 0:
            return []
        searcher = _SEARCHERS.get(mode)
        if searcher is None:
            raise RetrievalUnavailable(f"Retrieval mode '{mode}' is not available")
        try:
            async with self.session_factory() as session:
                scored = await searcher(session, query, language, limit)
                return await _hydrate(session, scored)
        except Exception as exc:
            # The message stays generic: driver errors may carry SQL text and parameters.
            log_event("retrieval.failed", mode=mode, error_type=type(exc).__name__)
            raise RetrievalUnavailable(f"Retrieval mode '{mode}' failed") from exc


async def _hydrate(session: AsyncSession, scored: list[ScoredChunk]) -> list[RetrievedChunk]:
    """Load chunk and document fields, keeping the searcher order."""
    if not scored:
        return []
    statement = (
        select(Chunk, Document.title, Document.doc_key)
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.id.in_([hit.chunk_id for hit in scored]))
    )
    rows = {
        chunk.id: (chunk, title, doc_key)
        for chunk, title, doc_key in await session.execute(statement)
    }
    hydrated: list[RetrievedChunk] = []
    for hit in scored:
        row = rows.get(hit.chunk_id)
        if row is None:  # deleted between search and hydration
            continue
        chunk, title, doc_key = row
        hydrated.append(
            RetrievedChunk(
                chunk_id=chunk.id,
                document_title=title,
                doc_key=doc_key,
                section=chunk.section,
                page=chunk.page,
                language=cast(Lang, chunk.language),
                content=chunk.content,
                score=hit.score,
            )
        )
    return hydrated
