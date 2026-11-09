"""Nullable embedding column on chunks (RAG-07).

The dimension is `ATLAS_EMBEDDING_DIMENSIONS` at migration time (DA-19). The table holds no
production data yet (AS-4 NO), so the column is added without a backfill; ingestion fills it.
No vector index here: HNSW arrives in phase 05.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

from api.config import get_settings

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    dimensions = get_settings().embedding_dimensions
    op.add_column("chunks", sa.Column("embedding", Vector(dimensions), nullable=True))


def downgrade() -> None:
    op.drop_column("chunks", "embedding")
