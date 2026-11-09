"""Fixtures for the auth and customer-isolation contract tests (CT-16, CT-17, CT-20, CT-23).

Every test runs against `api.main.create_app()` with `get_db_session` bound to the rolled-back
`db_session` of `tests/conftest.py`, so rows created by one test never leak into another. The
chat service is built with a `FakeLLMClient`, so no test ever reaches a real provider.
"""

from collections.abc import AsyncIterator, Iterator
from typing import Annotated
from uuid import UUID

import httpx
import pytest
from auth_contract_helpers import PERSONA_PASSWORD, LoginFn, session_cookie_from
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from api.chat.service import ChatService, get_chat_service
from api.config import get_settings
from api.db.engine import get_db_session
from api.llm.fake import FakeLLMClient
from api.main import create_app
from api.seed.personas import PERSONAS, seed_personas


@pytest.fixture
def contract_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Deterministic settings for the auth contract: fake LLM, known persona password."""
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "fake")
    monkeypatch.setenv("ATLAS_PERSONA_PASSWORD", PERSONA_PASSWORD)
    monkeypatch.setenv("ATLAS_COOKIE_SECURE", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def personas(contract_env: None, db_session: AsyncSession) -> dict[str, UUID]:
    """Seeded personas (CT-23) keyed by username."""
    ids = await seed_personas(db_session, get_settings().persona_password)
    return {spec.username: customer_id for spec, customer_id in zip(PERSONAS, ids, strict=True)}


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    return FakeLLMClient([])


@pytest.fixture
def app(contract_env: None, db_session: AsyncSession, fake_llm: FakeLLMClient) -> FastAPI:
    application = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def _chat_service(
        session: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> ChatService:
        # CT-15 (phase 02): ChatService(llm, settings, session).
        return ChatService(fake_llm, get_settings(), session)

    application.dependency_overrides[get_db_session] = _db
    application.dependency_overrides[get_chat_service] = _chat_service
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
def login(client: httpx.AsyncClient, personas: dict[str, UUID]) -> LoginFn:
    """Log a persona in and return the clear-text session token from `Set-Cookie`."""

    async def _login(username: str) -> str:
        response = await client.post(
            "/auth/login", json={"username": username, "password": PERSONA_PASSWORD}
        )
        assert response.status_code == 200, response.text
        client.cookies.clear()
        return session_cookie_from(response).value

    return _login
