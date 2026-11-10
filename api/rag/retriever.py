"""Retriever facade with pluggable retrieval modes (CT-30, DA-16).

Modes come from `api.config.RetrievalMode` (`none`, `fulltext`, `vector_bruteforce`,
`vector_hnsw`, `hybrid`). Text searchers (`register_mode`) receive the question; vector
searchers (`register_vector_mode`, CT-35) receive the question embedding, computed here with the
retriever's `embedder` (one `embed([query])` call per retrieval). Phase 03 registers `fulltext`,
phase 04 `vector_bruteforce`, phase 05 `vector_hnsw` and `hybrid` (`register_hybrid_mode`:
the searcher receives both the question and its embedding; full-text + HNSW fused with RRF,
CT-36). A vector or hybrid mode without an embedder, an `EmbeddingError` and any
search failure all become `RetrievalUnavailable` (RAG-94); an empty index yields `[]` (RAG-90).

Phase 14 (TASK-133, OBS-01): each retrieval that searches is one `retrieval` span (CT-62) with
the mode, `k`, and the ids, doc keys and scores of the returned chunks (in order); a failure is
recorded on the span with its error type only (never the message).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from opentelemetry.trace import Status, StatusCode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.config import RetrievalMode, Settings
from api.messages import Lang
from api.observability.logging import log_event
from api.observability.tracing import start_span
from api.rag.embeddings import EmbeddingClient
from api.rag.fulltext import ScoredChunk, search_fulltext
from api.rag.hybrid import search_hybrid
from api.rag.models import Chunk, Document
from api.rag.vector_bruteforce import search_bruteforce
from api.rag.vector_hnsw import search_hnsw


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
VectorSearcher = Callable[[AsyncSession, list[float], Lang, int], Awaitable[list[ScoredChunk]]]
HybridSearcher = Callable[[AsyncSession, str, list[float], Lang, int], Awaitable[list[ScoredChunk]]]
# Internal shape of every registered mode: (session, query, query_vector or None, language, k).
_Search = Callable[[AsyncSession, str, list[float] | None, Lang, int], Awaitable[list[ScoredChunk]]]


@dataclass(frozen=True)
class _Registration:
    needs_vector: bool
    search: _Search


# `none` never reaches the registry: it returns `[]` without opening a session.
_REGISTRY: dict[RetrievalMode, _Registration] = {}


def _register(mode: RetrievalMode, registration: _Registration) -> None:
    if mode == "none":
        raise ValueError("Mode 'none' does not take a searcher")
    _REGISTRY[mode] = registration


def register_mode(mode: RetrievalMode, searcher: Searcher) -> None:
    """Register (or replace) the text searcher used for `mode`; it receives the question."""

    async def search(
        session: AsyncSession, query: str, _: list[float] | None, language: Lang, k: int
    ) -> list[ScoredChunk]:
        return await searcher(session, query, language, k)

    _register(mode, _Registration(needs_vector=False, search=search))


def register_vector_mode(mode: RetrievalMode, searcher: VectorSearcher) -> None:
    """Register (or replace) the vector searcher used for `mode` (CT-35)."""

    async def search(
        session: AsyncSession, _: str, vector: list[float] | None, language: Lang, k: int
    ) -> list[ScoredChunk]:
        if vector is None:  # pragma: no cover - the retriever always embeds for these modes
            raise RetrievalUnavailable("Vector search needs a query embedding")
        return await searcher(session, vector, language, k)

    _register(mode, _Registration(needs_vector=True, search=search))


def register_hybrid_mode(mode: RetrievalMode, searcher: HybridSearcher) -> None:
    """Register (or replace) a searcher that takes both the question and its embedding (CT-36)."""

    async def search(
        session: AsyncSession, query: str, vector: list[float] | None, language: Lang, k: int
    ) -> list[ScoredChunk]:
        if vector is None:  # pragma: no cover - the retriever always embeds for these modes
            raise RetrievalUnavailable("Hybrid search needs a query embedding")
        return await searcher(session, query, vector, language, k)

    _register(mode, _Registration(needs_vector=True, search=search))


def is_vector_mode(mode: RetrievalMode) -> bool:
    """True when `mode` needs a query embedding (an embedder must be configured)."""
    registration = _REGISTRY.get(mode)
    return registration is not None and registration.needs_vector


register_mode("fulltext", search_fulltext)
register_vector_mode("vector_bruteforce", search_bruteforce)
register_vector_mode("vector_hnsw", search_hnsw)
register_hybrid_mode("hybrid", search_hybrid)


class Retriever:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedder: EmbeddingClient | None,
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
        registration = _REGISTRY.get(mode)
        if registration is None or (registration.needs_vector and self.embedder is None):
            raise RetrievalUnavailable(f"Retrieval mode '{mode}' is not available")
        failure: Exception | None = None
        chunks: list[RetrievedChunk] = []
        with start_span(
            "retrieval",
            **{
                "atlas.retrieval.mode": mode,
                "atlas.retrieval.k": limit,
                "atlas.retrieval.language": language,
            },
        ) as span:
            try:
                query_vector: list[float] | None = None
                if registration.needs_vector and self.embedder is not None:
                    [query_vector] = await self.embedder.embed([query])
                async with self.session_factory() as session:
                    scored = await registration.search(
                        session, query, query_vector, language, limit
                    )
                    chunks = await _hydrate(session, scored)
            except Exception as exc:
                # The message stays generic: driver errors may carry SQL text and parameters.
                log_event("retrieval.failed", mode=mode, error_type=type(exc).__name__)
                # Only the error type reaches the span: the exception is raised after the span
                # ends, so its message and chained driver error are never recorded.
                span.set_attribute("error.type", type(exc).__name__)
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                failure = exc
            else:
                span.set_attribute("atlas.retrieval.count", len(chunks))
                span.set_attribute("atlas.retrieval.chunk_ids", [c.chunk_id for c in chunks])
                span.set_attribute("atlas.retrieval.doc_keys", [c.doc_key for c in chunks])
                span.set_attribute("atlas.retrieval.scores", [float(c.score) for c in chunks])
        if failure is not None:
            raise RetrievalUnavailable(f"Retrieval mode '{mode}' failed") from failure
        return chunks


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
