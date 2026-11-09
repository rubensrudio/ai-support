"""Fixtures for the `POST /triage` contract tests (CT-16, CT-48, CT-49, CT-74, LAC-35).

Every test runs against `api.main.create_app()` with `get_db_session` bound to the rolled-back
`db_session` of `tests/conftest.py`. The triage service is built with a scripted
`FakeLLMClient` (`fake_llm`), so no test reaches a real provider and every test can count how
many LLM calls a request made (`fake_llm.requests`).

This folder has no `__init__.py` and no helper module on purpose: everything the tests share is
a fixture here, so nothing is imported by a bare module name that could collide with another
test folder named `triage` in the same pytest run.
"""

import json
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.deps import OPERATOR_HEADER, SESSION_COOKIE
from api.auth.service import create_session
from api.config import get_settings
from api.db.engine import get_db_session
from api.llm.client import LLMResponse, Usage
from api.llm.fake import FakeLLMClient
from api.main import create_app
from api.seed.personas import PERSONAS, seed_personas
from api.triage.routes import get_triage_service
from api.triage.service import TriageService

OPERATOR_KEY = "triage-contract-operator-key-4e7b"
PERSONA_PASSWORD = "triage-contract-persona-pass-2d81"  # noqa: S105 - fictitious credential
MAX_MESSAGE_CHARS = 300
MAX_REPAIRS = 2

AnswerFn = Callable[[dict[str, Any] | str], LLMResponse]


@pytest.fixture
def triage_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Deterministic settings: fake LLM, schema mode, known operator key, limits and repairs."""
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "fake")
    monkeypatch.setenv("ATLAS_PERSONA_PASSWORD", PERSONA_PASSWORD)
    monkeypatch.setenv("ATLAS_COOKIE_SECURE", "false")
    monkeypatch.setenv("ATLAS_OPERATOR_API_KEY", OPERATOR_KEY)
    monkeypatch.setenv("ATLAS_MAX_MESSAGE_CHARS", str(MAX_MESSAGE_CHARS))
    monkeypatch.setenv("ATLAS_TRIAGE_MODE", "schema")
    monkeypatch.setenv("ATLAS_TRIAGE_MAX_REPAIRS", str(MAX_REPAIRS))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def operator_disabled(monkeypatch: pytest.MonkeyPatch, triage_env: None) -> None:
    """`ATLAS_OPERATOR_API_KEY` empty: the operator path is off (LAC-35)."""
    monkeypatch.setenv("ATLAS_OPERATOR_API_KEY", "")
    get_settings.cache_clear()
    configured = get_settings().operator_api_key
    assert configured is None or configured.get_secret_value() == ""


@pytest.fixture
def operator_key() -> str:
    return OPERATOR_KEY


@pytest.fixture
def operator_headers() -> dict[str, str]:
    return {OPERATOR_HEADER: OPERATOR_KEY}


@pytest.fixture
def max_chars() -> int:
    return MAX_MESSAGE_CHARS


@pytest.fixture
def max_repairs() -> int:
    return MAX_REPAIRS


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    return FakeLLMClient([])


@pytest.fixture
def app(
    triage_env: None,
    db_session: AsyncSession,
    fake_llm: FakeLLMClient,
) -> FastAPI:
    application = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db_session] = _db

    # CT-49: the route exposes `get_triage_service` for the tests to override (CT-74).
    def _triage_service() -> TriageService:
        return TriageService(fake_llm, get_settings())

    application.dependency_overrides[get_triage_service] = _triage_service
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
async def persona_token(triage_env: None, db_session: AsyncSession) -> str:
    """Clear-text session token of the seeded persona `alex`."""
    ids = await seed_personas(db_session, get_settings().persona_password)
    by_username = {
        spec.username: customer_id for spec, customer_id in zip(PERSONAS, ids, strict=True)
    }
    return await create_session(db_session, by_username["alex"])


@pytest.fixture
def persona_headers(persona_token: str) -> dict[str, str]:
    """Explicit `Cookie` header, so tests do not depend on the client cookie jar."""
    return {"Cookie": f"{SESSION_COOKIE}={persona_token}"}


@pytest.fixture
def answer() -> AnswerFn:
    """Fake LLM answer whose text is `payload` (a dict is serialized to JSON)."""

    def _answer(payload: dict[str, Any] | str) -> LLMResponse:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return LLMResponse(
            text=text,
            tool_calls=[],
            usage=Usage(input_tokens=40, output_tokens=30),
            model="fake-model",
            provider="fake",
            stop_reason="end_turn",
        )

    return _answer


@pytest.fixture
def valid_triage() -> dict[str, Any]:
    return {
        "categoria": "reembolso",
        "prioridade": "alta",
        "sentimento": "negativo",
        "precisa_humano": True,
        "resumo": "Customer asks for a refund of a damaged order.",
    }
