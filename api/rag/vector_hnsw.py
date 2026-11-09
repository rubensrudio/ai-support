"""HNSW vector search with pgvector (CT-35, RAG-10, TASK-061).

`SET LOCAL hnsw.ef_search = settings.hnsw_ef_search` in the search transaction, then
`ORDER BY chunks.embedding <=> :query_vector LIMIT k` over the chunks of the question language,
from documents with status `indexed` and a non-null embedding, using the
`ix_chunks_embedding_hnsw` index (migration 0006, cosine ops). Score = `1 - cosine distance`
(cosine similarity), highest first.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from api.messages import Lang
from api.rag.fulltext import ScoredChunk


async def search_hnsw(
    session: AsyncSession, query_vector: list[float], language: Lang, k: int
) -> list[ScoredChunk]:
    raise NotImplementedError("TASK-061")
