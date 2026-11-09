import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.rag.models import CHUNKS_HNSW_INDEX

pytestmark = pytest.mark.integration


async def test_chunks_have_an_hnsw_cosine_index_with_the_configured_parameters(
    db_session: AsyncSession,
) -> None:
    settings = get_settings()
    indexdef = (
        await db_session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
            {"name": CHUNKS_HNSW_INDEX},
        )
    ).scalar_one()

    assert "USING hnsw (embedding vector_cosine_ops)" in indexdef
    assert f"m='{settings.hnsw_m}'" in indexdef
    assert f"ef_construction='{settings.hnsw_ef_construction}'" in indexdef


async def test_bench_vectors_table_stores_256_dimension_vectors(
    db_session: AsyncSession,
) -> None:
    declared = (
        await db_session.execute(
            text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = 'bench_vectors'::regclass AND attname = 'embedding'"
            )
        )
    ).scalar_one()
    assert declared == "vector(256)"
    await db_session.execute(
        text("INSERT INTO bench_vectors (embedding) VALUES (:v)"),
        {"v": "[" + ",".join("0.5" for _ in range(256)) + "]"},
    )


def test_migration_0006_downgrade_and_upgrade(
    alembic_config: Config, migrated_database: str
) -> None:
    downgrade(alembic_config, "0005")
    upgrade(alembic_config, "head")
