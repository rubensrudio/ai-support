"""Login, logout and me endpoints (TASK-026, CT-17, DA-10, AS-1)."""

import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.deps import SESSION_COOKIE
from api.auth.models import Customer, CustomerSession
from api.auth.routes import auth_router
from api.auth.service import hash_password, hash_token
from api.config import get_settings
from api.db.engine import get_db_session
from api.errors import install_error_handlers
from api.observability.middleware import TraceIdMiddleware

pytestmark = pytest.mark.integration

PASSWORD = "s3cret-for-tests"


def _build_app(db_session: AsyncSession) -> FastAPI:
    app = FastAPI()
    app.add_middleware(TraceIdMiddleware)
    install_error_handlers(app)
    app.include_router(auth_router)

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _session
    return app


@pytest.fixture
async def customer(db_session: AsyncSession) -> Customer:
    row = Customer(
        id=uuid.uuid4(),
        username="dana",
        display_name="Dana Lee",
        email="dana@example.com",
        password_hash=hash_password(PASSWORD),
        language="en",
    )
    db_session.add(row)
    await db_session.flush()
    return row


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=_build_app(db_session), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
def secure_cookie(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ATLAS_COOKIE_SECURE", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("ATLAS_COOKIE_SECURE", raising=False)
    get_settings.cache_clear()


def _set_cookie(response: httpx.Response) -> str:
    return response.headers["set-cookie"]


async def _login(client: httpx.AsyncClient, username: str, password: str) -> httpx.Response:
    return await client.post("/auth/login", json={"username": username, "password": password})


async def test_login_returns_the_customer_and_a_hardened_session_cookie(
    client: httpx.AsyncClient, customer: Customer
) -> None:
    response = await _login(client, "dana", PASSWORD)

    assert response.status_code == 200
    assert response.json() == {"customer": {"display_name": "Dana Lee", "language": "en"}}
    cookie = _set_cookie(response)
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie or "SameSite=Strict" in cookie
    assert "Path=/" in cookie
    assert f"Max-Age={get_settings().session_ttl_hours * 3600}" in cookie
    assert "Secure" not in cookie


async def test_login_cookie_is_secure_when_configured(
    client: httpx.AsyncClient, customer: Customer, secure_cookie: None
) -> None:
    response = await _login(client, "dana", PASSWORD)

    assert response.status_code == 200
    assert "Secure" in _set_cookie(response)


async def test_login_stores_only_the_token_hash(
    client: httpx.AsyncClient, customer: Customer, db_session: AsyncSession
) -> None:
    response = await _login(client, "dana", PASSWORD)
    token = response.cookies[SESSION_COOKIE]

    stored = (await db_session.execute(select(CustomerSession.token_hash))).scalars().all()

    assert stored == [hash_token(token)]


async def test_invalid_credentials_have_an_identical_body_for_unknown_user_and_wrong_password(
    client: httpx.AsyncClient, customer: Customer, db_session: AsyncSession
) -> None:
    wrong_password = await _login(client, "dana", "wrong")
    unknown_user = await _login(client, "nobody", "wrong")

    bodies = []
    for response in (wrong_password, unknown_user):
        assert response.status_code == 401
        assert "set-cookie" not in response.headers
        body = response.json()
        assert body["error"].pop("trace_id")
        bodies.append(body)
    assert bodies[0] == bodies[1]
    assert bodies[0] == {
        "error": {"code": "INVALID_CREDENTIALS", "message": "Invalid username or password."}
    }
    count = await db_session.scalar(select(func.count()).select_from(CustomerSession))
    assert count == 0


async def test_me_with_cookie_then_401_after_logout(
    client: httpx.AsyncClient, customer: Customer, db_session: AsyncSession
) -> None:
    await _login(client, "dana", PASSWORD)

    me = await client.get("/auth/me")
    assert me.status_code == 200
    assert me.json() == {"display_name": "Dana Lee", "language": "en"}

    logout = await client.post("/auth/logout")
    assert logout.status_code == 204
    assert f'{SESSION_COOKIE}=""' in _set_cookie(logout) or "Max-Age=0" in _set_cookie(logout)
    count = await db_session.scalar(select(func.count()).select_from(CustomerSession))
    assert count == 0

    after = await client.get("/auth/me")
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "AUTH_REQUIRED"


async def test_revoked_token_is_refused_even_if_the_client_keeps_it(
    client: httpx.AsyncClient, customer: Customer
) -> None:
    token = (await _login(client, "dana", PASSWORD)).cookies[SESSION_COOKIE]
    await client.post("/auth/logout")
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, token)

    response = await client.get("/auth/me")

    assert response.status_code == 401


async def test_me_without_cookie_is_auth_required(client: httpx.AsyncClient) -> None:
    response = await client.get("/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"
    assert response.json()["error"]["message"] == "Please sign in with a test account."


async def test_logout_without_session_is_still_204(client: httpx.AsyncClient) -> None:
    response = await client.post("/auth/logout")

    assert response.status_code == 204


async def test_login_rejects_a_malformed_body(client: httpx.AsyncClient) -> None:
    response = await client.post("/auth/login", json={"username": "dana"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
