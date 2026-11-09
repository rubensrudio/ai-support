import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.rag.models import TS_CONFIGS, Chunk, Document

pytestmark = pytest.mark.integration


async def test_chunk_stores_and_reads_an_embedding_of_the_configured_size(
    db_session: AsyncSession,
) -> None:
    dimensions = get_settings().embedding_dimensions
    document = Document(
        doc_key="vectors",
        title="Vectors",
        language="en",
        doc_type="manual",
        format="md",
        source_path="corpus/en/vectors.md",
        content_hash="h",
        status="indexed",
    )
    db_session.add(document)
    await db_session.flush()
    vector = [float(i % 7) / 7 for i in range(dimensions)]
    chunk = Chunk(
        document_id=document.id,
        ordinal=0,
        content="Vectors are stored per chunk.",
        language="en",
        token_count=5,
        ts_config=TS_CONFIGS["en"],
        embedding=vector,
    )
    db_session.add(chunk)
    await db_session.flush()

    stored = (
        await db_session.execute(select(Chunk.embedding).where(Chunk.id == chunk.id))
    ).scalar_one()
    assert stored is not None
    assert len(stored) == dimensions
    assert [round(float(x), 5) for x in stored] == [round(x, 5) for x in vector]

    declared = (
        await db_session.execute(
            text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
            )
        )
    ).scalar_one()
    assert declared == f"vector({dimensions})"


async def test_embedding_is_nullable(db_session: AsyncSession) -> None:
    document = Document(
        doc_key="no-vector",
        title="No vector",
        language="pt",
        doc_type="faq",
        format="md",
        source_path="corpus/pt/no-vector.md",
        content_hash="h",
        status="indexed",
    )
    db_session.add(document)
    await db_session.flush()
    chunk = Chunk(
        document_id=document.id,
        ordinal=0,
        content="Sem vetor ainda.",
        language="pt",
        token_count=3,
        ts_config=TS_CONFIGS["pt"],
    )
    db_session.add(chunk)
    await db_session.flush()
    stored = (
        await db_session.execute(select(Chunk.embedding).where(Chunk.id == chunk.id))
    ).scalar_one()
    assert stored is None


def test_migration_0005_downgrade_and_upgrade(
    alembic_config: Config, migrated_database: str
) -> None:
    downgrade(alembic_config, "0004")
    upgrade(alembic_config, "head")
