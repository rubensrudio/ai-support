"""Fixed test personas (CT-23, DEMO-06, TASK-027): idempotent seed, login with the configured
`ATLAS_PERSONA_PASSWORD`, password stored only as an argon2 hash (DA-10)."""

from uuid import UUID

import httpx
import pytest
from auth_contract_helpers import PERSONA_PASSWORD
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import Customer
from api.config import get_settings
from api.seed.personas import PERSONAS, seed_personas

pytestmark = pytest.mark.integration


async def test_personas_contain_alex_en_and_bia_pt() -> None:
    by_username = {spec.username: spec for spec in PERSONAS}

    assert len(PERSONAS) >= 2
    assert by_username["alex"].language == "en"
    assert by_username["bia"].language == "pt"
    assert all(spec.email.endswith("@example.com") for spec in PERSONAS)


async def test_seed_is_idempotent(
    contract_env: None, db_session: AsyncSession, personas: dict[str, UUID]
) -> None:
    again = await seed_personas(db_session, get_settings().persona_password)

    assert again == [personas[spec.username] for spec in PERSONAS]
    total = await db_session.scalar(select(func.count()).select_from(Customer))
    assert total == len(PERSONAS)
    rows = (await db_session.scalars(select(Customer))).all()
    assert {row.username for row in rows} == {spec.username for spec in PERSONAS}
    for spec in PERSONAS:
        row = next(row for row in rows if row.username == spec.username)
        assert row.id == personas[spec.username]
        assert (row.display_name, row.email, row.language) == (
            spec.display_name,
            spec.email,
            spec.language,
        )


async def test_seeded_password_is_an_argon2_hash(
    db_session: AsyncSession, personas: dict[str, UUID]
) -> None:
    rows = (await db_session.scalars(select(Customer))).all()

    assert rows
    for row in rows:
        assert row.password_hash.startswith("$argon2")
        assert PERSONA_PASSWORD not in row.password_hash


@pytest.mark.parametrize(
    ("username", "display_name", "language"),
    [("alex", "Alex Morgan", "en"), ("bia", "Bia Souza", "pt")],
)
async def test_personas_log_in_with_the_configured_password(
    client: httpx.AsyncClient,
    personas: dict[str, UUID],
    username: str,
    display_name: str,
    language: str,
) -> None:
    assert get_settings().persona_password == PERSONA_PASSWORD

    response = await client.post(
        "/auth/login", json={"username": username, "password": PERSONA_PASSWORD}
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"customer": {"display_name": display_name, "language": language}}


async def test_personas_do_not_log_in_with_the_default_password(
    client: httpx.AsyncClient, personas: dict[str, UUID]
) -> None:
    response = await client.post("/auth/login", json={"username": "alex", "password": "atlas-demo"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"
