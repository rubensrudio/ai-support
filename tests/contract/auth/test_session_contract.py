"""Session lifecycle contract: `/auth/me`, `/auth/logout`, expiry and token storage.

CT-16, CT-17, CT-18, DA-10 (opaque token stored only as SHA-256), AS-7.
"""

import hashlib
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from auth_contract_helpers import LoginFn, cookie_header
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import CustomerSession
from api.auth.service import hash_token

pytestmark = pytest.mark.integration

HEX_64 = re.compile(r"^[0-9a-f]{64}$")


def _assert_auth_required(response: httpx.Response) -> None:
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "AUTH_REQUIRED"
    assert error["trace_id"] == response.headers["x-trace-id"]


async def test_me_returns_the_logged_in_customer(client: httpx.AsyncClient, login: LoginFn) -> None:
    alex = await login("alex")
    bia = await login("bia")

    alex_me = await client.get("/auth/me", headers=cookie_header(alex))
    bia_me = await client.get("/auth/me", headers=cookie_header(bia))

    assert alex_me.status_code == 200
    assert alex_me.json() == {"display_name": "Alex Morgan", "language": "en"}
    assert bia_me.status_code == 200
    assert bia_me.json() == {"display_name": "Bia Souza", "language": "pt"}


async def test_me_without_cookie_is_auth_required(
    client: httpx.AsyncClient, personas: dict[str, UUID]
) -> None:
    _assert_auth_required(await client.get("/auth/me"))


async def test_me_with_tampered_cookie_is_auth_required(
    client: httpx.AsyncClient, login: LoginFn
) -> None:
    token = await login("alex")
    flipped = token[:-1] + ("A" if token[-1] != "A" else "B")

    for forged in (flipped, token + "x", token[:-4], "not-a-real-token", ""):
        _assert_auth_required(await client.get("/auth/me", headers=cookie_header(forged)))


async def test_logout_revokes_the_session_server_side(
    client: httpx.AsyncClient, login: LoginFn
) -> None:
    token = await login("alex")
    assert (await client.get("/auth/me", headers=cookie_header(token))).status_code == 200

    logout = await client.post("/auth/logout", headers=cookie_header(token))

    assert logout.status_code == 204
    _assert_auth_required(await client.get("/auth/me", headers=cookie_header(token)))


async def test_logout_does_not_revoke_other_sessions(
    client: httpx.AsyncClient, login: LoginFn
) -> None:
    alex = await login("alex")
    bia = await login("bia")

    await client.post("/auth/logout", headers=cookie_header(alex))

    assert (await client.get("/auth/me", headers=cookie_header(bia))).status_code == 200


async def test_expired_session_is_auth_required(
    client: httpx.AsyncClient, login: LoginFn, db_session: AsyncSession
) -> None:
    token = await login("alex")
    assert (await client.get("/auth/me", headers=cookie_header(token))).status_code == 200

    result = await db_session.execute(
        update(CustomerSession)
        .where(CustomerSession.token_hash == hashlib.sha256(token.encode("utf-8")).hexdigest())
        .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await db_session.flush()
    assert result.rowcount == 1  # type: ignore[attr-defined]

    _assert_auth_required(await client.get("/auth/me", headers=cookie_header(token)))


async def test_session_table_stores_only_a_sha256_of_the_token(
    client: httpx.AsyncClient,
    login: LoginFn,
    personas: dict[str, UUID],
    db_session: AsyncSession,
) -> None:
    token = await login("alex")

    rows = (
        await db_session.scalars(
            select(CustomerSession).where(CustomerSession.customer_id == personas["alex"])
        )
    ).all()

    assert len(rows) == 1
    row = rows[0]
    expected = hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert HEX_64.fullmatch(row.token_hash)
    assert row.token_hash == expected
    assert hash_token(token) == expected
    stored_values = [str(getattr(row, column.key)) for column in CustomerSession.__table__.columns]
    assert all(token not in value for value in stored_values)
    assert row.expires_at > datetime.now(UTC)
