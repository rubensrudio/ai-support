"""HNSW index on chunk embeddings and the synthetic benchmark table (RAG-10, RAG-11).

- `ix_chunks_embedding_hnsw`: `hnsw (embedding vector_cosine_ops) WITH (m, ef_construction)`,
  with `m = ATLAS_HNSW_M` and `ef_construction = ATLAS_HNSW_EF_CONSTRUCTION` at migration time.
  `python -m ingest reindex-hnsw` rebuilds it with the current configuration (TASK-060).
- `bench_vectors(id bigserial, embedding vector(256))`: synthetic vectors for demo 05. The
  dimension is 256, the same as the phase 04 in-memory benchmark (operator decision; the plan
  said 1536, which makes 1M vectors ~6 GB plus the index). Demo 05 creates its own HNSW index on
  this table; no index here.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

from api.config import get_settings

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHUNKS_HNSW_INDEX = "ix_chunks_embedding_hnsw"
BENCH_DIMENSIONS = 256


def upgrade() -> None:
    settings = get_settings()
    op.execute(
        f"CREATE INDEX {CHUNKS_HNSW_INDEX} ON chunks USING hnsw (embedding vector_cosine_ops) "
        f"WITH (m = {int(settings.hnsw_m)}, ef_construction = {int(settings.hnsw_ef_construction)})"
    )
    op.create_table(
        "bench_vectors",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("embedding", Vector(BENCH_DIMENSIONS), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("bench_vectors")
    op.execute(f"DROP INDEX IF EXISTS {CHUNKS_HNSW_INDEX}")
