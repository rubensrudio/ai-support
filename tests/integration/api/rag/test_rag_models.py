import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.rag.models import TS_CONFIGS, Chunk, Document

pytestmark = pytest.mark.integration


def _document(language: str, doc_key: str = "billing") -> Document:
    return Document(
        doc_key=doc_key,
        title="Billing",
        language=language,
        doc_type="manual",
        format="md",
        source_path=f"corpus/{language}/{doc_key}.md",
        content_hash="h",
        status="indexed",
    )


def _chunk(document: Document, content: str) -> Chunk:
    return Chunk(
        document_id=document.id,
        ordinal=0,
        content=content,
        language=document.language,
        token_count=len(content.split()),
        ts_config=TS_CONFIGS[document.language],
    )


def test_migration_0004_downgrade_and_upgrade(
    alembic_config: Config, migrated_database: str
) -> None:
    downgrade(alembic_config, "0003")
    upgrade(alembic_config, "head")


async def test_fulltext_column_uses_the_chunk_language(db_session: AsyncSession) -> None:
    pt, en = _document("pt"), _document("en")
    db_session.add_all([pt, en])
    await db_session.flush()
    db_session.add_all(
        [
            _chunk(pt, "Gerencie suas assinaturas pelo painel."),
            _chunk(en, "Manage your subscriptions from the dashboard."),
        ]
    )
    await db_session.flush()
    query = func.to_tsquery(text("'portuguese'::regconfig"), "assinatura")
    matches = (
        await db_session.execute(select(Chunk.language).where(Chunk.tsv.op("@@")(query)))
    ).scalars()
    assert list(matches) == ["pt"]


async def test_deleting_a_document_deletes_its_chunks(db_session: AsyncSession) -> None:
    document = _document("en", doc_key="cascade")
    db_session.add(document)
    await db_session.flush()
    db_session.add(_chunk(document, "Refunds take five days."))
    await db_session.flush()
    await db_session.delete(document)
    await db_session.flush()
    count = (
        await db_session.execute(
            select(func.count()).select_from(Chunk).where(Chunk.document_id == document.id)
        )
    ).scalar_one()
    assert count == 0
