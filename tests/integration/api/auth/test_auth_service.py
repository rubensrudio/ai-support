"""Session authentication service and dependencies (TASK-025, CT-16, DA-10, AS-1, AS-7)."""

import hashlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from api.auth import service
from api.auth.deps import (
    SESSION_COOKIE,
    CurrentCustomer,
    OperatorPrincipal,
    require_customer,
    require_customer_or_operator,
)
from api.auth.models import Customer, CustomerSession
from api.config import get_settings
from api.errors import AtlasError, ErrorCode

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"


async def _add_customer(
    session: AsyncSession, username: str = "carol", language: str = "pt"
) -> Customer:
    customer = Customer(
        id=uuid.uuid4(),
        username=username,
        display_name=username.title(),
        email=f"{username}@example.com",
        password_hash=service.hash_password(PASSWORD),
        language=language,
    )
    session.add(customer)
    await session.flush()
    return customer


def _request(cookie: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if cookie is not None:
        headers.append((b"cookie", f"{SESSION_COOKIE}={cookie}".encode()))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


@pytest.fixture
def operator_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    key = "operator-key-for-tests"
    monkeypatch.setenv("ATLAS_OPERATOR_API_KEY", key)
    get_settings.cache_clear()
    yield key
    monkeypatch.delenv("ATLAS_OPERATOR_API_KEY", raising=False)
    get_settings.cache_clear()


@pytest.fixture
def no_operator_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ATLAS_OPERATOR_API_KEY", "")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("ATLAS_OPERATOR_API_KEY", raising=False)
    get_settings.cache_clear()


def test_hash_password_is_argon2_and_not_the_raw_password() -> None:
    hashed = service.hash_password(PASSWORD)

    assert hashed.startswith("$argon2")
    assert PASSWORD not in hashed
    assert service.hash_password(PASSWORD) != hashed  # random salt


def test_hash_token_is_sha256_hex() -> None:
    assert service.hash_token("abc") == hashlib.sha256(b"abc").hexdigest()
    assert len(service.hash_token("abc")) == 64


async def test_valid_login_yields_a_resolvable_session(db_session: AsyncSession) -> None:
    customer = await _add_customer(db_session)

    current = await service.authenticate(db_session, "carol", PASSWORD)
    token = await service.create_session(db_session, current.id)
    resolved = await service.resolve_session(db_session, token)

    assert current == CurrentCustomer(
        id=customer.id, email="carol@example.com", display_name="Carol", language="pt"
    )
    assert resolved == current


async def test_wrong_password_raises_invalid_credentials(db_session: AsyncSession) -> None:
    await _add_customer(db_session)

    with pytest.raises(AtlasError) as error:
        await service.authenticate(db_session, "carol", "wrong")

    assert error.value.code is ErrorCode.INVALID_CREDENTIALS
    assert error.value.http_status == 401


async def test_unknown_user_fails_like_wrong_password_with_the_same_hash_work(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _add_customer(db_session)
    verified: list[str] = []
    original_verify = service._verify

    def spy(hashed: str, password: str) -> bool:
        verified.append(hashed)
        return original_verify(hashed, password)

    monkeypatch.setattr(service, "_verify", spy)

    errors: list[AtlasError] = []
    for username, password in (("carol", "wrong"), ("nobody", "wrong")):
        with pytest.raises(AtlasError) as error:
            await service.authenticate(db_session, username, password)
        errors.append(error.value)

    assert len(verified) == 2  # one argon2 verify for each case
    assert verified[1].startswith("$argon2")
    assert [(e.code, e.http_status, e.params) for e in errors] == [
        (ErrorCode.INVALID_CREDENTIALS, 401, {}),
        (ErrorCode.INVALID_CREDENTIALS, 401, {}),
    ]


async def test_session_expires_after_the_configured_ttl(db_session: AsyncSession) -> None:
    customer = await _add_customer(db_session)
    before = datetime.now(UTC)

    token = await service.create_session(db_session, customer.id)

    row = (await db_session.execute(select(CustomerSession))).scalar_one()
    ttl = timedelta(hours=get_settings().session_ttl_hours)
    assert before + ttl <= row.expires_at <= datetime.now(UTC) + ttl
    assert len(token) >= 43  # token_urlsafe(32)


async def test_expired_token_resolves_to_none(db_session: AsyncSession) -> None:
    customer = await _add_customer(db_session)
    token = await service.create_session(db_session, customer.id)
    await db_session.execute(
        update(CustomerSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )

    assert await service.resolve_session(db_session, token) is None


async def test_unknown_token_resolves_to_none(db_session: AsyncSession) -> None:
    await _add_customer(db_session)

    assert await service.resolve_session(db_session, "not-a-token") is None


async def test_table_stores_only_the_token_hash(db_session: AsyncSession) -> None:
    customer = await _add_customer(db_session)
    token = await service.create_session(db_session, customer.id)

    rows = (await db_session.execute(text("SELECT * FROM customer_sessions"))).all()

    assert len(rows) == 1
    columns = [str(value) for value in rows[0]]
    assert service.hash_token(token) in columns
    assert all(token not in value for value in columns)


async def test_revoke_session_deletes_the_row(db_session: AsyncSession) -> None:
    customer = await _add_customer(db_session)
    token = await service.create_session(db_session, customer.id)

    await service.revoke_session(db_session, token)

    assert await service.resolve_session(db_session, token) is None
    assert (await db_session.execute(select(CustomerSession))).first() is None


async def test_require_customer_resolves_the_session_cookie(db_session: AsyncSession) -> None:
    customer = await _add_customer(db_session)
    token = await service.create_session(db_session, customer.id)

    current = await require_customer(_request(token), db_session)

    assert current.id == customer.id


@pytest.mark.parametrize("cookie", [None, "", "unknown-token"])
async def test_require_customer_without_valid_cookie_is_auth_required(
    db_session: AsyncSession, cookie: str | None
) -> None:
    with pytest.raises(AtlasError) as error:
        await require_customer(_request(cookie), db_session)

    assert (error.value.code, error.value.http_status) == (ErrorCode.AUTH_REQUIRED, 401)


async def test_require_customer_with_expired_cookie_is_auth_required(
    db_session: AsyncSession,
) -> None:
    customer = await _add_customer(db_session)
    token = await service.create_session(db_session, customer.id)
    await db_session.execute(
        update(CustomerSession).values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )

    with pytest.raises(AtlasError) as error:
        await require_customer(_request(token), db_session)

    assert error.value.code is ErrorCode.AUTH_REQUIRED


async def test_operator_path_accepts_the_correct_key(
    db_session: AsyncSession, operator_key: str
) -> None:
    principal = await require_customer_or_operator(_request(), db_session, operator_key)

    assert principal == OperatorPrincipal()


async def test_operator_path_refuses_a_wrong_key(
    db_session: AsyncSession, operator_key: str
) -> None:
    with pytest.raises(AtlasError) as error:
        await require_customer_or_operator(_request(), db_session, operator_key + "x")

    assert (error.value.code, error.value.http_status) == (ErrorCode.AUTH_REQUIRED, 401)


@pytest.mark.parametrize("key", ["", "anything", "operator-key-for-tests"])
async def test_operator_path_is_disabled_without_a_configured_key(
    db_session: AsyncSession, no_operator_key: None, key: str
) -> None:
    with pytest.raises(AtlasError) as error:
        await require_customer_or_operator(_request(), db_session, key)

    assert error.value.code is ErrorCode.AUTH_REQUIRED


async def test_customer_or_operator_prefers_a_valid_customer_cookie(
    db_session: AsyncSession, operator_key: str
) -> None:
    customer = await _add_customer(db_session)
    token = await service.create_session(db_session, customer.id)

    principal = await require_customer_or_operator(_request(token), db_session, None)

    assert isinstance(principal, CurrentCustomer)
    assert principal.id == customer.id


async def test_customer_or_operator_without_cookie_or_key_is_auth_required(
    db_session: AsyncSession, operator_key: str
) -> None:
    with pytest.raises(AtlasError) as error:
        await require_customer_or_operator(_request("unknown-token"), db_session, None)

    assert error.value.code is ErrorCode.AUTH_REQUIRED
