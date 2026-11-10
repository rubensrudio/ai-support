"""Fixtures for the `POST /messages/{message_id}/feedback` contract tests (CT-64, CT-65, AS-1).

Every test runs against `api.main.create_app()` with `get_db_session` bound to the rolled-back
`db_session` of `tests/conftest.py`. Langfuse is replaced by `FakeLangfuse` through the
`get_langfuse_api` dependency, so no test reaches the network. Conversations and messages are
created directly in the database for the two personas `alex` and `bia`.

Shared types live in `feedback_contract_helpers.py`, a unique module name, so the bare import
never collides with another test folder.
"""

import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from fastapi import FastAPI
from feedback_contract_helpers import (
    PERSONA_PASSWORD,
    FakeLangfuse,
    MessageFactory,
    SeededMessage,
)
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.deps import SESSION_COOKIE
from api.auth.service import create_session
from api.chat.models import Conversation, Message
from api.config import get_settings
from api.db.engine import get_db_session
from api.feedback.routes import get_langfuse_api
from api.main import create_app
from api.seed.personas import PERSONAS, seed_personas


@pytest.fixture
def feedback_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "fake")
    monkeypatch.setenv("ATLAS_PERSONA_PASSWORD", PERSONA_PASSWORD)
    monkeypatch.setenv("ATLAS_COOKIE_SECURE", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_langfuse() -> FakeLangfuse:
    return FakeLangfuse()


@pytest.fixture
def app(feedback_env: None, db_session: AsyncSession, fake_langfuse: FakeLangfuse) -> FastAPI:
    application = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db_session] = _db
    application.dependency_overrides[get_langfuse_api] = lambda: fake_langfuse
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
async def personas(feedback_env: None, db_session: AsyncSession) -> dict[str, uuid.UUID]:
    ids = await seed_personas(db_session, get_settings().persona_password)
    return {spec.username: customer_id for spec, customer_id in zip(PERSONAS, ids, strict=True)}


@pytest.fixture
async def cookies(
    personas: dict[str, uuid.UUID], db_session: AsyncSession
) -> dict[str, dict[str, str]]:
    """Explicit `Cookie` header per persona, so tests never depend on the client cookie jar."""
    headers: dict[str, dict[str, str]] = {}
    for username in ("alex", "bia"):
        token = await create_session(db_session, personas[username])
        headers[username] = {"Cookie": f"{SESSION_COOKIE}={token}"}
    return headers


@pytest.fixture
def make_message(db_session: AsyncSession, personas: dict[str, uuid.UUID]) -> MessageFactory:
    """Create a conversation of `owner` with one message of `role`; return its id and trace."""

    async def _make(owner: str, role: str = "assistant") -> SeededMessage:
        customer_id = personas[owner]
        conversation = Conversation(customer_id=customer_id)
        db_session.add(conversation)
        await db_session.flush()
        trace_id = uuid.uuid4().hex
        message = Message(
            conversation_id=conversation.id,
            role=role,
            content=f"{role} message of {owner}",
            status="complete",
            language="en",
            trace_id=trace_id,
        )
        db_session.add(message)
        await db_session.flush()
        return SeededMessage(id=message.id, trace_id=trace_id, customer_id=customer_id)

    return _make
