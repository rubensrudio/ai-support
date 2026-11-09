"""`python -m ingest reindex-hnsw` rebuilds the chunks HNSW index (TASK-060, RAG-10, CT-28).

Every test that rebuilds the index restores it with the configured defaults afterwards: other
tests on the same per-session database check `m='16'` (`test_hnsw_migration.py`).
"""

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ingest.__main__ as cli
import ingest.reindex as reindex_module
from api.config import Settings, get_settings
from api.db.engine import get_sessionmaker
from api.rag.models import CHUNKS_HNSW_INDEX
from ingest.reindex import reindex_hnsw

pytestmark = pytest.mark.integration

ROOT_DIR = Path(__file__).resolve().parents[3]

Factory = async_sessionmaker[AsyncSession]

COSINE_HNSW = "USING hnsw (embedding vector_cosine_ops)"


async def _indexdef(factory: Factory) -> str:
    async with factory() as session:
        indexdef: str = (
            await session.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
                {"name": CHUNKS_HNSW_INDEX},
            )
        ).scalar_one()
    return indexdef


@pytest.fixture
async def factory(migrated_database: str) -> AsyncIterator[Factory]:
    """Real session factory; the index is rebuilt with the configured defaults at the end."""
    session_factory = get_sessionmaker()
    yield session_factory
    get_settings.cache_clear()
    await reindex_hnsw(session_factory, get_settings())
    default = await _indexdef(session_factory)
    assert f"m='{get_settings().hnsw_m}'" in default


def _settings(m: int, ef_construction: int) -> Settings:
    return get_settings().model_copy(update={"hnsw_m": m, "hnsw_ef_construction": ef_construction})


async def test_reindex_applies_the_configured_m_and_ef_construction(factory: Factory) -> None:
    indexdef = await reindex_hnsw(factory, _settings(8, 40))

    stored = await _indexdef(factory)
    assert indexdef == stored
    assert "m='8'" in stored
    assert "ef_construction='40'" in stored
    assert COSINE_HNSW in stored
    assert "ON public.chunks" in stored


async def test_reindex_can_run_again_and_keeps_a_single_index(factory: Factory) -> None:
    await reindex_hnsw(factory, _settings(8, 40))
    indexdef = await reindex_hnsw(factory, _settings(12, 48))

    assert "m='12'" in indexdef
    assert "ef_construction='48'" in indexdef
    async with factory() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM pg_indexes WHERE indexname = :name"),
                {"name": CHUNKS_HNSW_INDEX},
            )
        ).scalar_one()
    assert count == 1


async def test_reindex_logs_the_parameters_and_duration(
    factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        reindex_module, "log_event", lambda event, **fields: events.append((event, fields))
    )

    await reindex_hnsw(factory, _settings(8, 40))

    assert [event for event, _ in events] == ["ingest_reindex_hnsw"]
    fields = events[0][1]
    assert fields["m"] == 8
    assert fields["ef_construction"] == 40
    assert isinstance(fields["duration_ms"], float)


@pytest.mark.parametrize(("m", "ef_construction"), [(0, 64), (16, 0), (-1, 64)])
async def test_reindex_rejects_non_positive_parameters_without_touching_the_index(
    factory: Factory, m: int, ef_construction: int
) -> None:
    before = await _indexdef(factory)

    with pytest.raises(ValueError, match="must be a positive integer"):
        await reindex_hnsw(factory, _settings(m, ef_construction))

    assert await _indexdef(factory) == before


async def test_cli_reindex_hnsw_uses_the_environment_configuration(
    factory: Factory, test_database_url: str
) -> None:
    env = {
        **os.environ,
        "ATLAS_DATABASE_URL": test_database_url,
        "ATLAS_LLM_PROVIDER": "fake",
        "ATLAS_EMBEDDING_PROVIDER": "fake",
        "ATLAS_HNSW_M": "8",
        "ATLAS_HNSW_EF_CONSTRUCTION": "32",
    }

    completed = subprocess.run(  # noqa: S603 - fixed interpreter and arguments
        [sys.executable, "-m", "ingest", "reindex-hnsw"],
        cwd=ROOT_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    stored = await _indexdef(factory)
    assert "m='8'" in stored
    assert "ef_construction='32'" in stored
    assert COSINE_HNSW in stored
    assert stored in completed.stdout


def test_cli_reports_a_database_error_without_sql(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    statement = f"DROP INDEX IF EXISTS {CHUNKS_HNSW_INDEX}"

    async def failing(factory: object, settings: Settings) -> str:
        raise OperationalError(statement, {}, Exception("connection refused"))

    async def no_dispose() -> None:
        return None

    monkeypatch.setattr(cli, "reindex_hnsw", failing)
    monkeypatch.setattr(cli, "get_sessionmaker", lambda: None)
    monkeypatch.setattr(cli, "dispose_engine", no_dispose)

    code = cli.main(["reindex-hnsw"])

    assert code == cli.EXIT_FAILED
    captured = capsys.readouterr()
    assert "error: could not rebuild the HNSW index (database error)" in captured.err
    assert "DROP INDEX" not in captured.err
    assert "connection refused" not in captured.err
    assert "DROP INDEX" not in captured.out
