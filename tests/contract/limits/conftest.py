"""Fixtures for the public demo limit contract tests (CT-66..CT-70, DEMO-02, DEMO-03, AS-2).

Every test runs against `api.main.create_app()` with `get_db_session` bound to the rolled-back
`db_session` of `tests/conftest.py`. The chat service and the triage service are built with
`CountingFakeLLM`, which answers every call and counts it, so no test reaches a real provider and
every test can assert that a refused request never reached the LLM.

The rate limit and the spend cap read their settings from the environment (`limits_env`): the
tests set `ATLAS_RATE_LIMIT_PER_HOUR` and `ATLAS_DAILY_SPEND_CAP_USD` explicitly, overriding the
high limit that `tests/conftest.py` sets for the other suites.

This folder has no `__init__.py`; shared types live in `limits_contract_helpers.py`, a unique
module name, so nothing collides with `tests/integration/api/limits` (a package named
`limits`) in the same pytest run.
"""

import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI
from limits_contract_helpers import (
    CHAT_ANSWER,
    DAILY_CAP_USD,
    OPERATOR_KEY,
    PERSONA_PASSWORD,
    RATE_LIMIT_PER_HOUR,
    VALID_TRIAGE,
    CountFn,
    CountingFakeLLM,
    FakeLLMs,
    Persona,
    SpendFn,
    start_of_utc_day,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.deps import OPERATOR_HEADER, SESSION_COOKIE
from api.auth.service import create_session
from api.chat.models import Conversation, Message
from api.chat.service import ChatService, get_chat_service
from api.config import get_settings
from api.db.engine import get_db_session
from api.limits import deps as limit_deps
from api.limits.models import LlmCall, RateLimitEvent
from api.main import create_app
from api.seed.personas import PERSONAS, seed_personas
from api.triage.routes import get_triage_service
from api.triage.service import TriageService


def _set_limits_env(
    monkeypatch: pytest.MonkeyPatch, *, rate_limit: int, cap: Decimal | None
) -> None:
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "fake")
    monkeypatch.setenv("ATLAS_PERSONA_PASSWORD", PERSONA_PASSWORD)
    monkeypatch.setenv("ATLAS_COOKIE_SECURE", "false")
    monkeypatch.setenv("ATLAS_OPERATOR_API_KEY", OPERATOR_KEY)
    monkeypatch.setenv("ATLAS_GUARDRAILS_ENABLED", "false")
    monkeypatch.setenv("ATLAS_AGENT_TOOLS_ENABLED", "false")
    monkeypatch.setenv("ATLAS_TRIAGE_MODE", "schema")
    monkeypatch.setenv("ATLAS_RATE_LIMIT_PER_HOUR", str(rate_limit))
    if cap is not None:
        monkeypatch.setenv("ATLAS_DAILY_SPEND_CAP_USD", str(cap))
    get_settings.cache_clear()


@pytest.fixture
def limits_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Rate limit of 3 messages per hour and a daily cap of 2.00 USD, fake LLM, operator key."""
    _set_limits_env(monkeypatch, rate_limit=RATE_LIMIT_PER_HOUR, cap=DAILY_CAP_USD)
    yield
    get_settings.cache_clear()


@pytest.fixture
def rate_limit() -> int:
    return RATE_LIMIT_PER_HOUR


@pytest.fixture
def unlimited_rate(limits_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spend cap tests: a rate limit high enough never to interfere."""
    _set_limits_env(monkeypatch, rate_limit=1_000_000, cap=DAILY_CAP_USD)


@pytest.fixture
def fake_llms() -> FakeLLMs:
    return FakeLLMs(
        chat=CountingFakeLLM(CHAT_ANSWER), triage=CountingFakeLLM(json.dumps(VALID_TRIAGE))
    )


@pytest.fixture
def app(limits_env: None, db_session: AsyncSession, fake_llms: FakeLLMs) -> FastAPI:
    application = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def _chat_service(
        session: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> ChatService:
        return ChatService(fake_llms.chat, get_settings(), session)

    def _triage_service() -> TriageService:
        return TriageService(fake_llms.triage, get_settings())

    application.dependency_overrides[get_db_session] = _db
    application.dependency_overrides[get_chat_service] = _chat_service
    application.dependency_overrides[get_triage_service] = _triage_service
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
async def personas(limits_env: None, db_session: AsyncSession) -> dict[str, Persona]:
    """The seeded personas (alex/en, bia/pt), each with an explicit session `Cookie` header."""
    ids = await seed_personas(db_session, PERSONA_PASSWORD)
    result: dict[str, Persona] = {}
    for spec, customer_id in zip(PERSONAS, ids, strict=True):
        token = await create_session(db_session, customer_id)
        result[spec.username] = Persona(
            id=customer_id,
            username=spec.username,
            language=spec.language,
            headers={"Cookie": f"{SESSION_COOKIE}={token}"},
        )
    return result


@pytest.fixture
def operator_headers() -> dict[str, str]:
    return {OPERATOR_HEADER: OPERATOR_KEY}


@pytest.fixture
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """`utc_now()` of the limit dependencies pinned to midday UTC of today."""
    now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(limit_deps, "utc_now", lambda: now)
    return now


@pytest.fixture
def spend(db_session: AsyncSession) -> SpendFn:
    """Record an LLM call of `cost_usd` at `created_at` in the ledger."""

    async def _spend(cost_usd: Decimal, created_at: datetime) -> None:
        db_session.add(
            LlmCall(
                trace_id=uuid.uuid4().hex,
                provider="anthropic",
                model="claude-sonnet-5-5",
                cost_usd=cost_usd,
                created_at=created_at,
            )
        )
        await db_session.flush()

    return _spend


@pytest.fixture
def cap_reached(spend: SpendFn, fixed_now: datetime) -> Callable[[], Awaitable[None]]:
    """Fill today's ledger up to exactly the daily cap (two calls of 1.00 USD)."""

    async def _reach() -> None:
        today = start_of_utc_day(fixed_now)
        await spend(DAILY_CAP_USD / 2, today + timedelta(minutes=5))
        await spend(DAILY_CAP_USD / 2, fixed_now - timedelta(minutes=1))

    return _reach


@pytest.fixture
def count_rate_events(db_session: AsyncSession) -> CountFn:
    async def _count(customer_id: uuid.UUID) -> int:
        value = await db_session.scalar(
            select(func.count())
            .select_from(RateLimitEvent)
            .where(RateLimitEvent.customer_id == customer_id)
        )
        return int(value or 0)

    return _count


@pytest.fixture
def count_conversations(db_session: AsyncSession) -> CountFn:
    async def _count(customer_id: uuid.UUID) -> int:
        value = await db_session.scalar(
            select(func.count())
            .select_from(Conversation)
            .where(Conversation.customer_id == customer_id)
        )
        return int(value or 0)

    return _count


@pytest.fixture
def count_messages(db_session: AsyncSession) -> CountFn:
    async def _count(customer_id: uuid.UUID) -> int:
        value = await db_session.scalar(
            select(func.count())
            .select_from(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Conversation.customer_id == customer_id)
        )
        return int(value or 0)

    return _count
