"""Fixed test personas seed (TASK-027, CT-23, DEMO-06)."""

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import Customer, CustomerSession
from api.auth.service import authenticate, create_session
from api.db.engine import session_scope
from api.errors import AtlasError, ErrorCode
from api.seed.personas import PERSONAS, seed_personas

pytestmark = pytest.mark.integration

ROOT_DIR = Path(__file__).resolve().parents[4]
PASSWORD = "persona-password-for-tests"


async def _customer_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(Customer)) or 0)


def test_personas_are_fictitious_and_cover_both_languages() -> None:
    assert [(p.username, p.language) for p in PERSONAS][:2] == [("alex", "en"), ("bia", "pt")]
    assert all(p.email.endswith("@example.com") for p in PERSONAS)


async def test_seed_twice_leaves_two_customers_that_authenticate(
    db_session: AsyncSession,
) -> None:
    first = await seed_personas(db_session, PASSWORD)
    second = await seed_personas(db_session, PASSWORD)

    assert first == second
    assert len(first) == len(PERSONAS) == 2
    assert await _customer_count(db_session) == 2
    for persona, customer_id in zip(PERSONAS, first, strict=True):
        current = await authenticate(db_session, persona.username, PASSWORD)
        assert current.id == customer_id
        assert (current.display_name, current.email, current.language) == (
            persona.display_name,
            persona.email,
            persona.language,
        )


async def test_seed_updates_password_and_data_of_existing_personas(
    db_session: AsyncSession,
) -> None:
    ids = await seed_personas(db_session, PASSWORD)
    alex = await db_session.get(Customer, ids[0])
    assert alex is not None
    alex.display_name = "Tampered"
    await db_session.flush()

    assert await seed_personas(db_session, "new-password") == ids

    current = await authenticate(db_session, "alex", "new-password")
    assert current.display_name == "Alex Morgan"
    with pytest.raises(AtlasError) as error:
        await authenticate(db_session, "alex", PASSWORD)
    assert error.value.code is ErrorCode.INVALID_CREDENTIALS


@pytest.fixture
async def committed_personas(migrated_database: str) -> AsyncIterator[str]:
    """For CLI tests that commit to the test database: remove the personas afterwards."""
    yield migrated_database
    usernames = [p.username for p in PERSONAS]
    async with session_scope() as session:
        await session.execute(delete(Customer).where(Customer.username.in_(usernames)))


def _run_cli(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "ATLAS_DATABASE_URL": database_url,
        "ATLAS_PERSONA_PASSWORD": PASSWORD,
        "ATLAS_LLM_PROVIDER": "fake",
    }
    return subprocess.run(  # noqa: S603 - fixed interpreter and module, test-only arguments
        [sys.executable, "-m", "api.seed", *args],
        cwd=ROOT_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


async def test_cli_seeds_personas_with_the_configured_password(committed_personas: str) -> None:
    result = _run_cli(committed_personas)

    assert result.returncode == 0, result.stderr
    assert PASSWORD not in result.stdout + result.stderr
    async with session_scope() as session:
        assert await _customer_count(session) == 2
        for persona in PERSONAS:
            await authenticate(session, persona.username, PASSWORD)


async def test_cli_reset_recreates_the_personas(committed_personas: str) -> None:
    assert _run_cli(committed_personas).returncode == 0
    async with session_scope() as session:
        before = (await authenticate(session, "alex", PASSWORD)).id
        await create_session(session, before)

    result = _run_cli(committed_personas, "--reset")

    assert result.returncode == 0, result.stderr
    async with session_scope() as session:
        after = (await authenticate(session, "alex", PASSWORD)).id
        sessions = await session.scalar(select(func.count()).select_from(CustomerSession))
        assert await _customer_count(session) == 2
    assert after != before
    assert sessions == 0
