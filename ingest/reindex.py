"""Rebuild the chunks HNSW index with the current configuration (RAG-10, TASK-060).

`python -m ingest reindex-hnsw` drops and recreates `ix_chunks_embedding_hnsw` with
`m = ATLAS_HNSW_M` and `ef_construction = ATLAS_HNSW_EF_CONSTRUCTION`, without code changes.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.config import Settings


async def reindex_hnsw(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> str:
    """Recreate the index and return its new definition (`pg_indexes.indexdef`)."""
    raise NotImplementedError("TASK-060")
