"""Brute-force cosine vector search (CT-35, RAG-08, RAG-17, TASK-056).

Loads the embeddings of the indexed chunks in the question language into a NumPy matrix and
ranks them by cosine similarity to the query vector (phase 04: no pgvector operator, no index).
Only chunks whose document has status `indexed` and whose `embedding` is not null are searched.
Scores are the cosine similarity, highest first; ties keep the lowest `chunk_id` first.
"""

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.messages import Lang
from api.rag.fulltext import ScoredChunk
from api.rag.models import Chunk, Document


async def search_bruteforce(
    session: AsyncSession, query_vector: list[float], language: Lang, k: int
) -> list[ScoredChunk]:
    """Return up to `k` chunks of `language` closest to `query_vector`, best score first.

    `k <= 0`, a zero-norm query vector or no searchable chunk returns `[]`.
    """
    if k <= 0:
        return []
    query = np.asarray(query_vector, dtype=np.float64)
    query_norm = float(np.linalg.norm(query))
    if query.ndim != 1 or query_norm == 0.0:
        return []

    statement = (
        select(Chunk.id, Chunk.embedding)
        .join(Document, Document.id == Chunk.document_id)
        .where(
            Chunk.language == language,
            Document.status == "indexed",
            Chunk.embedding.is_not(None),
        )
        .order_by(Chunk.id)
    )
    rows = (await session.execute(statement)).all()
    if not rows:
        return []

    ids = np.fromiter((chunk_id for chunk_id, _ in rows), dtype=np.int64, count=len(rows))
    matrix = np.vstack([np.asarray(embedding, dtype=np.float64) for _, embedding in rows])
    norms = np.linalg.norm(matrix, axis=1)
    # A zero-norm chunk vector has no direction: its cosine is defined here as 0.
    safe_norms = np.where(norms == 0.0, 1.0, norms)
    scores = (matrix @ (query / query_norm)) / safe_norms

    top = _top_k(scores, ids, k)
    return [ScoredChunk(chunk_id=int(ids[i]), score=float(scores[i])) for i in top]


def _top_k(scores: np.ndarray, ids: np.ndarray, k: int) -> np.ndarray:
    """Indices of the `k` best scores, descending, ties broken by the lowest id."""
    candidates = np.arange(scores.size)
    if k < scores.size:
        # Keep every score tied with the k-th best so the tie-break below stays exact.
        threshold = scores[np.argpartition(-scores, k - 1)[k - 1]]
        candidates = np.flatnonzero(scores >= threshold)
    order = np.lexsort((ids[candidates], -scores[candidates]))
    return candidates[order][:k]
