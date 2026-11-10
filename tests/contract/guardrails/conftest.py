"""Fixtures for the guardrail contract tests (Phase 10, AS-3).

Every API test runs against `api.main.create_app()` with `get_db_session` bound to the
rolled-back `db_session` of `tests/conftest.py`, a seeded persona logged in through
`POST /auth/login`, and the chat service built with `RoutingFakeLLM`, so no test ever reaches a
real provider. Service tests build `ChatService` with the same fake and the same settings.
"""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.deps import SESSION_COOKIE, CurrentCustomer
from api.chat.service import ChatService, get_chat_service
from api.config import get_settings
from api.db.engine import get_db_session
from api.main import create_app
from api.messages import Lang
from api.seed.personas import PERSONAS, seed_personas
from api.tools.models import Order, OrderStatus

from .guard_helpers import RoutingFakeLLM, capture_atlas_logs

PERSONA_PASSWORD = "guard-contract-pass-4e8b"  # noqa: S105 - fictitious test credential


@dataclass(frozen=True)
class Persona:
    id: uuid.UUID
    username: str
    email: str
    display_name: str
    language: Lang

    @property
    def principal(self) -> CurrentCustomer:
        return CurrentCustomer(
            id=self.id, email=self.email, display_name=self.display_name, language=self.language
        )


def _set_env(monkeypatch: pytest.MonkeyPatch, *, guardrails: bool) -> None:
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "fake")
    monkeypatch.setenv("ATLAS_PERSONA_PASSWORD", PERSONA_PASSWORD)
    monkeypatch.setenv("ATLAS_COOKIE_SECURE", "false")
    monkeypatch.setenv("ATLAS_AGENT_TOOLS_ENABLED", "true")
    monkeypatch.setenv("ATLAS_GUARDRAILS_ENABLED", "true" if guardrails else "false")
    get_settings.cache_clear()


@pytest.fixture
def guard_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Guardrails on (the default), fake LLM, known persona password."""
    _set_env(monkeypatch, guardrails=True)
    yield
    get_settings.cache_clear()


@pytest.fixture
def guardrails_off(guard_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: `ATLAS_GUARDRAILS_ENABLED=false`."""
    _set_env(monkeypatch, guardrails=False)


@pytest.fixture
async def personas(guard_env: None, db_session: AsyncSession) -> dict[str, Persona]:
    """The seeded personas (alex/en, bia/pt) keyed by username."""
    ids = await seed_personas(db_session, PERSONA_PASSWORD)
    return {
        spec.username: Persona(
            id=customer_id,
            username=spec.username,
            email=spec.email,
            display_name=spec.display_name,
            language=spec.language,
        )
        for spec, customer_id in zip(PERSONAS, ids, strict=True)
    }


OrderFactory = Callable[[Persona, OrderStatus], Awaitable[Order]]


@pytest.fixture
def make_order(db_session: AsyncSession) -> OrderFactory:
    async def _make(owner: Persona, status: OrderStatus) -> Order:
        order = Order(
            customer_id=owner.id,
            status=status.value,
            total_cents=4_990,
            currency="USD",
            items=[{"sku": "box-coffee", "name": "Coffee box", "qty": 1}],
        )
        db_session.add(order)
        await db_session.flush()
        return order

    return _make


@pytest.fixture
def llm() -> RoutingFakeLLM:
    return RoutingFakeLLM()


@pytest.fixture
def service(guard_env: None, db_session: AsyncSession, llm: RoutingFakeLLM) -> ChatService:
    """`ChatService` as the API builds it (no retriever), with the routing fake."""
    return ChatService(llm, get_settings(), db_session)


@pytest.fixture
def app(guard_env: None, db_session: AsyncSession, llm: RoutingFakeLLM) -> FastAPI:
    application = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def _chat_service(
        session: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> ChatService:
        return ChatService(llm, get_settings(), session)

    application.dependency_overrides[get_db_session] = _db
    application.dependency_overrides[get_chat_service] = _chat_service
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


LoginFn = Callable[[str], Awaitable[dict[str, str]]]


@pytest.fixture
def login(client: httpx.AsyncClient, personas: dict[str, Persona]) -> LoginFn:
    """Log a persona in; returns the `Cookie` header of its session."""

    async def _login(username: str) -> dict[str, str]:
        response = await client.post(
            "/auth/login", json={"username": username, "password": PERSONA_PASSWORD}
        )
        assert response.status_code == 200, response.text
        prefix = f"{SESSION_COOKIE}="
        tokens = [
            header.split(";", 1)[0][len(prefix) :]
            for header in response.headers.get_list("set-cookie")
            if header.startswith(prefix)
        ]
        assert len(tokens) == 1 and tokens[0], "login did not set the session cookie"
        token = tokens[0]
        client.cookies.clear()
        return {"Cookie": f"{SESSION_COOKIE}={token}"}

    return _login


@pytest.fixture
def atlas_logs(app: FastAPI) -> Iterator[list[str]]:
    """JSON log lines of the `atlas` logger during the test (after `create_app` configured it)."""
    with capture_atlas_logs() as lines:
        yield lines
