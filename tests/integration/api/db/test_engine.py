import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.engine import session_scope

pytestmark = pytest.mark.integration


async def test_pgvector_extension_is_enabled(db_session: AsyncSession) -> None:
    result = await db_session.execute(
        text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
    )
    assert result.scalars().all() == ["vector"]


async def test_session_scope_rolls_back_on_error(migrated_database: str) -> None:
    async with session_scope() as session:
        await session.execute(text("CREATE TABLE IF NOT EXISTS scope_probe (v int)"))
    with pytest.raises(RuntimeError):
        async with session_scope() as session:
            await session.execute(text("INSERT INTO scope_probe VALUES (1)"))
            raise RuntimeError("boom")
    async with session_scope() as session:
        count = (await session.execute(text("SELECT count(*) FROM scope_probe"))).scalar_one()
        await session.execute(text("DROP TABLE scope_probe"))
    assert count == 0


async def test_session_scope_commits_on_success(migrated_database: str) -> None:
    async with session_scope() as session:
        await session.execute(text("CREATE TABLE commit_probe (v int)"))
        await session.execute(text("INSERT INTO commit_probe VALUES (7)"))
    async with session_scope() as session:
        value = (await session.execute(text("SELECT v FROM commit_probe"))).scalar_one()
        await session.execute(text("DROP TABLE commit_probe"))
    assert value == 7


def test_downgrade_and_upgrade_round_trip(alembic_config: Config, migrated_database: str) -> None:
    downgrade(alembic_config, "base")
    upgrade(alembic_config, "head")
