"""Hybrid search: full-text + HNSW fused with Reciprocal Rank Fusion (CT-36, RAG-12, TASK-062).

- `rrf_fuse(rankings, k, rrf_k=60)`: each ranking is a list of `ScoredChunk` best-first; a chunk
  at 1-based rank `r` in a ranking contributes `1 / (rrf_k + r)`; the fused score is the sum over
  the rankings where it appears. Returns the top `k` by fused score, highest first.
- `search_hybrid(session, query, query_vector, language, k)`: runs `search_fulltext` and
  `search_hnsw` for the question language and fuses them with `rrf_k = settings.rrf_k`. This is
  the production default mode (`ATLAS_RETRIEVAL_MODE=hybrid`). No reranker (LAC-18).
"""

from dataclasses import dataclass
from fractions import Fraction

from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.messages import Lang
from api.rag.fulltext import ScoredChunk, search_fulltext
from api.rag.vector_hnsw import search_hnsw

# Each search returns `CANDIDATE_FACTOR * k` candidates before the fusion: a chunk ranked just
# below `k` in both lists can still beat one ranked high in a single list.
CANDIDATE_FACTOR = 2


@dataclass
class _Fused:
    score: Fraction
    best_rank: int


def rrf_fuse(rankings: list[list[ScoredChunk]], k: int, rrf_k: int = 60) -> list[ScoredChunk]:
    """Fuse best-first `rankings` with RRF and return up to `k` chunks, best fused score first.

    The input scores are ignored: only the positions count. A chunk repeated inside one ranking
    counts only at its first (best) position. Scores are summed as exact fractions, so equal
    sums tie exactly; ties go to the chunk with the best individual rank in any ranking, then to
    the lowest `chunk_id`. `k <= 0` or no ranked chunk returns `[]`; `rrf_k <= 0` raises
    `ValueError` (checked first, whatever `k` is).
    """
    if rrf_k <= 0:
        raise ValueError(f"rrf_k must be positive, got {rrf_k}")
    if k <= 0:
        return []
    fused: dict[int, _Fused] = {}
    for ranking in rankings:
        seen: set[int] = set()
        for rank, hit in enumerate(ranking, start=1):
            if hit.chunk_id in seen:
                continue
            seen.add(hit.chunk_id)
            contribution = Fraction(1, rrf_k + rank)
            entry = fused.get(hit.chunk_id)
            if entry is None:
                fused[hit.chunk_id] = _Fused(score=contribution, best_rank=rank)
            else:
                entry.score += contribution
                entry.best_rank = min(entry.best_rank, rank)
    ordered = sorted(fused.items(), key=lambda item: (-item[1].score, item[1].best_rank, item[0]))
    return [
        ScoredChunk(chunk_id=chunk_id, score=float(entry.score)) for chunk_id, entry in ordered[:k]
    ]


async def search_hybrid(
    session: AsyncSession, query: str, query_vector: list[float], language: Lang, k: int
) -> list[ScoredChunk]:
    """Return up to `k` chunks of `language` ranked by RRF over full-text and HNSW.

    Both searches run in `session`, one after the other (an `AsyncSession` is not concurrent),
    each for `CANDIDATE_FACTOR * k` candidates. When one of them finds nothing, the result is
    the other one's top `k` in its own order (with RRF scores). `k <= 0` searches nothing.
    """
    if k <= 0:
        return []
    candidates = max(k, CANDIDATE_FACTOR * k)
    text_hits = await search_fulltext(session, query, language, candidates)
    vector_hits = await search_hnsw(session, query_vector, language, candidates)
    return rrf_fuse([text_hits, vector_hits], k, rrf_k=get_settings().rrf_k)
