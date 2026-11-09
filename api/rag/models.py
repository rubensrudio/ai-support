"""Knowledge-base documents and chunks (CT-27, migration 0004, DA-13).

`chunks.tsv` is a generated column: `to_tsvector(ts_config, content)`, where `ts_config` is
`english` or `portuguese` according to the chunk language, indexed with GIN.
`chunks.embedding` (migration 0005, RAG-07) is a nullable `vector(ATLAS_EMBEDDING_DIMENSIONS)`;
the dimension is read from the settings when this module is imported. Migration 0006 adds the
HNSW index `CHUNKS_HNSW_INDEX` (cosine ops) over it; it is managed in SQL (migration and
`python -m ingest reindex-hnsw`), not declared here, so `m`/`ef_construction` can change without
touching the model.
"""

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import REGCONFIG, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from api.config import get_settings
from api.db.engine import Base

DOC_TYPES = ("manual", "policy", "faq")
DOC_FORMATS = ("md", "pdf")
DOC_STATUSES = ("indexed", "quarantined", "failed")
TS_CONFIGS = {"en": "english", "pt": "portuguese"}
EMBEDDING_DIMENSIONS = get_settings().embedding_dimensions
CHUNKS_HNSW_INDEX = "ix_chunks_embedding_hnsw"


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("doc_key", "language", name="uq_documents_doc_key_language"),
        CheckConstraint("language IN ('pt', 'en')", name="ck_documents_language"),
        CheckConstraint("doc_type IN ('manual', 'policy', 'faq')", name="ck_documents_doc_type"),
        CheckConstraint("format IN ('md', 'pdf')", name="ck_documents_format"),
        CheckConstraint(
            "status IN ('indexed', 'quarantined', 'failed')", name="ck_documents_status"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_key: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    doc_type: Mapped[str] = mapped_column(Text, nullable=False)
    format: Mapped[str] = mapped_column(Text, nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index("ix_chunks_language", "language"),
        Index("ix_chunks_document_ordinal", "document_id", "ordinal"),
        CheckConstraint("language IN ('pt', 'en')", name="ck_chunks_language"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    section: Mapped[str | None] = mapped_column(Text, nullable=True)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ts_config: Mapped[str] = mapped_column(REGCONFIG, nullable=False)
    tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector(ts_config, content)", persisted=True)
    )
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS), nullable=True
    )
