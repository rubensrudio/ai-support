"""Documents and chunks with a generated full-text column.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import REGCONFIG, TSVECTOR

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("doc_key", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("doc_type", sa.Text(), nullable=False),
        sa.Column("format", sa.Text(), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("doc_key", "language", name="uq_documents_doc_key_language"),
        sa.CheckConstraint("language IN ('pt', 'en')", name="ck_documents_language"),
        sa.CheckConstraint("doc_type IN ('manual', 'policy', 'faq')", name="ck_documents_doc_type"),
        sa.CheckConstraint("format IN ('md', 'pdf')", name="ck_documents_format"),
        sa.CheckConstraint(
            "status IN ('indexed', 'quarantined', 'failed')", name="ck_documents_status"
        ),
    )
    op.create_table(
        "chunks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "document_id",
            sa.Integer(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("section", sa.Text(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("ts_config", REGCONFIG(), nullable=False),
        sa.Column(
            "tsv",
            TSVECTOR(),
            sa.Computed("to_tsvector(ts_config, content)", persisted=True),
        ),
        sa.CheckConstraint("language IN ('pt', 'en')", name="ck_chunks_language"),
    )
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], postgresql_using="gin")
    op.create_index("ix_chunks_language", "chunks", ["language"])
    op.create_index("ix_chunks_document_ordinal", "chunks", ["document_id", "ordinal"])


def downgrade() -> None:
    op.drop_index("ix_chunks_document_ordinal", table_name="chunks")
    op.drop_index("ix_chunks_language", table_name="chunks")
    op.drop_index("ix_chunks_tsv", table_name="chunks")
    op.drop_table("chunks")
    op.drop_table("documents")
