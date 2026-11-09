"""Rebuild the chunks HNSW index with the current configuration (RAG-10, TASK-060).

`python -m ingest reindex-hnsw` drops and recreates `ix_chunks_embedding_hnsw` with
`m = ATLAS_HNSW_M` and `ef_construction = ATLAS_HNSW_EF_CONSTRUCTION`, without code changes.
Drop and create run in one transaction, so a failed build leaves the previous index in place.
"""

import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.config import Settings
from api.observability.logging import log_event
from api.rag.models import CHUNKS_HNSW_INDEX


def _positive_int(name: str, value: object) -> int:
    """The parameters go into DDL as literals: accept only positive integers, never text."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


async def reindex_hnsw(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> str:
    """Recreate the index and return its new definition (`pg_indexes.indexdef`)."""
    m = _positive_int("hnsw_m", settings.hnsw_m)
    ef_construction = _positive_int("hnsw_ef_construction", settings.hnsw_ef_construction)
    started = time.perf_counter()
    async with session_factory() as session, session.begin():
        await session.execute(text(f"DROP INDEX IF EXISTS {CHUNKS_HNSW_INDEX}"))
        await session.execute(
            text(
                f"CREATE INDEX {CHUNKS_HNSW_INDEX} ON chunks "
                "USING hnsw (embedding vector_cosine_ops) "
                f"WITH (m = {m}, ef_construction = {ef_construction})"
            )
        )
        indexdef: str = (
            await session.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
                {"name": CHUNKS_HNSW_INDEX},
            )
        ).scalar_one()
    log_event(
        "ingest_reindex_hnsw",
        m=m,
        ef_construction=ef_construction,
        duration_ms=round((time.perf_counter() - started) * 1000, 3),
    )
    return indexdef
