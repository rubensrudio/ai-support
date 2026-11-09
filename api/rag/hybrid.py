"""Hybrid search: full-text + HNSW fused with Reciprocal Rank Fusion (CT-36, RAG-12, TASK-062).

- `rrf_fuse(rankings, k, rrf_k=60)`: each ranking is a list of `ScoredChunk` best-first; a chunk
  at 1-based rank `r` in a ranking contributes `1 / (rrf_k + r)`; the fused score is the sum over
  the rankings where it appears. Returns the top `k` by fused score, highest first.
- `search_hybrid(session, query, query_vector, language, k)`: runs `search_fulltext` and
  `search_hnsw` for the question language and fuses them with `rrf_k = settings.rrf_k`. This is
  the production default mode (`ATLAS_RETRIEVAL_MODE=hybrid`). No reranker (LAC-18).
"""

from sqlalchemy.ext.asyncio import AsyncSession

from api.messages import Lang
from api.rag.fulltext import ScoredChunk


def rrf_fuse(rankings: list[list[ScoredChunk]], k: int, rrf_k: int = 60) -> list[ScoredChunk]:
    raise NotImplementedError("TASK-062")


async def search_hybrid(
    session: AsyncSession, query: str, query_vector: list[float], language: Lang, k: int
) -> list[ScoredChunk]:
    raise NotImplementedError("TASK-062")
