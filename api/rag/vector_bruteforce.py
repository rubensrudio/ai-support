"""Brute-force cosine vector search (CT-35, RAG-08, RAG-17, TASK-056).

Loads the embeddings of the indexed chunks in the question language into a NumPy matrix and
ranks them by cosine similarity to the query vector (phase 04: no pgvector operator, no index).
Only chunks whose document has status `indexed` and whose `embedding` is not null are searched.
Scores are the cosine similarity, highest first.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from api.messages import Lang
from api.rag.fulltext import ScoredChunk


async def search_bruteforce(
    session: AsyncSession, query_vector: list[float], language: Lang, k: int
) -> list[ScoredChunk]:
    raise NotImplementedError("TASK-056")
