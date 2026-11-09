"""HNSW vector search with pgvector (CT-35, RAG-10, TASK-061).

`SET LOCAL hnsw.ef_search = settings.hnsw_ef_search` in the search transaction, then
`ORDER BY chunks.embedding <=> :query_vector LIMIT k` over the chunks of the question language,
from documents with status `indexed` and a non-null embedding, using the
`ix_chunks_embedding_hnsw` index (migration 0006, cosine ops). Score = `1 - cosine distance`
(cosine similarity), highest first.

The language and status filters are applied after the index scan, so a plain HNSW scan can stop
with fewer than `k` rows when most of the `ef_search` candidates belong to another language.
The search therefore also sets `hnsw.iterative_scan = strict_order` (pgvector >= 0.8; the
`pgvector/pgvector:pg16` image ships 0.8.7): the index keeps scanning until `k` rows pass the
filters, and the rows still come back in exact distance order. Both settings are `SET LOCAL`
(`set_config(..., true)`), so they end with the transaction and never leak into a pooled
connection.
"""

from typing import cast

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.messages import Lang
from api.rag.fulltext import ScoredChunk
from api.rag.models import Chunk, Document

ITERATIVE_SCAN = "strict_order"


def hnsw_query(query_vector: list[float], language: Lang, k: int) -> Select[int, float]:
    """`(chunk_id, cosine distance)` of the `k` nearest searchable chunks of `language`.

    Ordered by the raw `<=>` expression so the planner can use the HNSW index.
    """
    # pgvector types the operator as `Operators`; it is a float column expression.
    distance = cast(ColumnElement[float], Chunk.embedding.cosine_distance(query_vector))
    return (
        select(Chunk.id, distance.label("distance"))
        .join(Document, Document.id == Chunk.document_id)
        .where(
            Chunk.language == language,
            Document.status == "indexed",
            Chunk.embedding.is_not(None),
        )
        .order_by(distance)
        .limit(k)
    )


async def search_hnsw(
    session: AsyncSession, query_vector: list[float], language: Lang, k: int
) -> list[ScoredChunk]:
    """Return up to `k` chunks of `language` closest to `query_vector`, best score first.

    `k <= 0`, a zero-norm query vector (no direction: the cosine distance is NaN) or no
    searchable chunk returns `[]`. Runs in the session's current transaction (autobegun if
    none), which the caller owns: no commit here.
    """
    if k <= 0 or not any(query_vector):
        return []
    ef_search = int(get_settings().hnsw_ef_search)
    # Same transaction as the query below: `is_local=true` is `SET LOCAL`.
    await session.execute(
        select(
            func.set_config("hnsw.ef_search", str(ef_search), True),
            func.set_config("hnsw.iterative_scan", ITERATIVE_SCAN, True),
        )
    )
    rows = (await session.execute(hnsw_query(query_vector, language, k))).all()
    return [ScoredChunk(chunk_id=int(chunk_id), score=1.0 - float(d)) for chunk_id, d in rows]
