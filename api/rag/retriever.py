"""Retriever facade with pluggable retrieval modes (CT-30). Skeleton stub: TASK-047.

Modes come from `api.config.RetrievalMode` (`none`, `fulltext`, `vector_bruteforce`,
`vector_hnsw`, `hybrid`); phase 03 implements `none` and `fulltext`, later phases register the
others with `register_mode`. Any search failure becomes `RetrievalUnavailable`.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.config import RetrievalMode, Settings
from api.messages import Lang
from api.rag.fulltext import ScoredChunk


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


def register_mode(mode: RetrievalMode, searcher: Searcher) -> None:
    raise NotImplementedError("TASK-047")


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
        raise NotImplementedError("TASK-047")
