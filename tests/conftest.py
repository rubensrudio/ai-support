"""Shared pytest fixtures.

Integration tests get a PostgreSQL database of their own per pytest session
(`atlas_test_<uuid>`), migrated with `alembic upgrade head` and dropped at the end, so parallel
runs never share state (DA-12). The database is created only when a test asks for
`test_database_url`, `migrated_database` or `db_session`, so unit tests stay offline.

Prerequisite for integration tests: `docker compose up -d db`.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic.command import upgrade
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.db.engine import dispose_engine, get_engine, reset_engine

ROOT_DIR = Path(__file__).resolve().parents[1]
TEST_DB_PREFIX = "atlas_test_"


@pytest.fixture(scope="session")
def admin_database_url() -> str:
    """URL of the configured server database, used only to create and drop test databases."""
    get_settings.cache_clear()
    url = get_settings().database_url
    get_settings.cache_clear()
    return url


@pytest.fixture(scope="session")
def test_database_url(admin_database_url: str) -> Iterator[str]:
    name = f"{TEST_DB_PREFIX}{uuid.uuid4().hex}"
    url = make_url(admin_database_url).set(database=name).render_as_string(hide_password=False)

    admin_engine = create_engine(admin_database_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("ATLAS_DATABASE_URL", url)
            get_settings.cache_clear()
            reset_engine()
            try:
                yield url
            finally:
                reset_engine()
                get_settings.cache_clear()
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin_engine.dispose()


@pytest.fixture(scope="session")
def alembic_config(test_database_url: str) -> Config:
    config = Config(str(ROOT_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT_DIR / "api" / "db" / "migrations"))
    # ConfigParser interpolation: a literal "%" (URL-encoded password) must be doubled.
    config.set_main_option("sqlalchemy.url", test_database_url.replace("%", "%%"))
    config.attributes["configure_logger"] = False
    return config


@pytest.fixture(scope="session")
def migrated_database(alembic_config: Config, test_database_url: str) -> str:
    upgrade(alembic_config, "head")
    return test_database_url


@pytest.fixture
async def db_session(migrated_database: str) -> AsyncIterator[AsyncSession]:
    """Session inside an outer transaction rolled back after the test.

    `await session.commit()` in the code under test only releases a savepoint, so nothing
    persists between tests.
    """
    connection = await get_engine().connect()
    transaction = await connection.begin()
    session = AsyncSession(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    try:
        yield session
    finally:
        await session.close()
        if transaction.is_active:
            await transaction.rollback()
        await connection.close()


@pytest.fixture(scope="session", autouse=True)
async def _dispose_engine_at_end() -> AsyncIterator[None]:
    yield
    await dispose_engine()
