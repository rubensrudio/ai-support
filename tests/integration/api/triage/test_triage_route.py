"""`POST /triage` with persona session or operator key (TASK-089, CT-49, TRI-01, TRI-05,
TRI-07, TRI-90, LAC-35, AS-1)."""

import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.deps import OPERATOR_HEADER, SESSION_COOKIE
from api.auth.models import Customer
from api.auth.service import create_session
from api.config import Settings, get_settings
from api.db.engine import get_db_session
from api.errors import AtlasError, ErrorCode
from api.llm.fake import FakeLLMClient
from api.main import create_app
from api.triage.routes import get_triage_service
from api.triage.schemas import TriageResult
from api.triage.service import TriageMode, TriageService

pytestmark = pytest.mark.integration

OPERATOR_KEY = "operator-key-for-tests"
RESULT = TriageResult(
    categoria="reembolso",
    prioridade="alta",
    sentimento="negativo",
    precisa_humano=True,
    resumo="Customer wants a refund for a damaged order.",
)


class RecordingTriageService(TriageService):
    """Test double: records every `classify` call and answers `RESULT` or raises `error`."""

    def __init__(self) -> None:
        super().__init__(FakeLLMClient([]), Settings(llm_provider="fake"))
        self.calls: list[str] = []
        self.error: AtlasError | None = None

    async def classify(self, text: str, mode: TriageMode | None = None) -> TriageResult:
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        return RESULT


@pytest.fixture
def service() -> RecordingTriageService:
    return RecordingTriageService()


@pytest.fixture
def operator_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("ATLAS_OPERATOR_API_KEY", OPERATOR_KEY)
    get_settings.cache_clear()
    yield OPERATOR_KEY
    monkeypatch.delenv("ATLAS_OPERATOR_API_KEY", raising=False)
    get_settings.cache_clear()


@pytest.fixture
def no_operator_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ATLAS_OPERATOR_API_KEY", "")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("ATLAS_OPERATOR_API_KEY", raising=False)
    get_settings.cache_clear()


@pytest.fixture
async def client(
    service: RecordingTriageService, db_session: AsyncSession
) -> AsyncIterator[httpx.AsyncClient]:
    """The real `require_customer_or_operator`; the session dependency shares the test
    transaction and the triage service is the recording double."""
    app = create_app()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_triage_service] = lambda: service
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def _login_persona(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    suffix = uuid.uuid4().hex[:12]
    customer = Customer(
        username=f"persona-{suffix}",
        display_name=f"Persona {suffix}",
        email=f"persona-{suffix}@example.test",
        password_hash="not-a-real-hash",  # noqa: S106 - fixture value, never verified
        language="en",
    )
    db_session.add(customer)
    await db_session.flush()
    client.cookies.set(SESSION_COOKIE, await create_session(db_session, customer.id))


def _assert_auth_required(response: httpx.Response) -> None:
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "AUTH_REQUIRED"
    assert error["trace_id"] == response.headers["x-trace-id"]


# --- access: persona session OR operator key (LAC-35, AS-1) --------------------------------


async def test_persona_session_gets_the_triage_result(
    client: httpx.AsyncClient, service: RecordingTriageService, db_session: AsyncSession
) -> None:
    await _login_persona(client, db_session)

    response = await client.post("/triage", json={"text": "My order arrived broken, refund!"})

    assert response.status_code == 200
    assert response.json() == RESULT.model_dump()
    assert service.calls == ["My order arrived broken, refund!"]


async def test_correct_operator_key_gets_the_triage_result(
    client: httpx.AsyncClient, service: RecordingTriageService, operator_key: str
) -> None:
    response = await client.post(
        "/triage",
        json={"text": "Meu pedido chegou quebrado."},
        headers={OPERATOR_HEADER: operator_key},
    )

    assert response.status_code == 200
    assert TriageResult.model_validate(response.json()) == RESULT
    assert service.calls == ["Meu pedido chegou quebrado."]


async def test_without_credentials_is_401_before_any_llm_call(
    client: httpx.AsyncClient, service: RecordingTriageService, operator_key: str
) -> None:
    response = await client.post("/triage", json={"text": "Where is my order?"})

    _assert_auth_required(response)
    assert service.calls == []


async def test_wrong_operator_key_is_401_before_any_llm_call(
    client: httpx.AsyncClient, service: RecordingTriageService, operator_key: str
) -> None:
    response = await client.post(
        "/triage",
        json={"text": "Where is my order?"},
        headers={OPERATOR_HEADER: operator_key + "-wrong"},
    )

    _assert_auth_required(response)
    assert service.calls == []


async def test_operator_key_is_rejected_when_no_key_is_configured(
    client: httpx.AsyncClient, service: RecordingTriageService, no_operator_key: None
) -> None:
    for candidate in ("", OPERATOR_KEY):
        response = await client.post(
            "/triage", json={"text": "Where is my order?"}, headers={OPERATOR_HEADER: candidate}
        )
        _assert_auth_required(response)
    assert service.calls == []


async def test_unknown_session_cookie_is_401(
    client: httpx.AsyncClient, service: RecordingTriageService
) -> None:
    client.cookies.set(SESSION_COOKIE, "not-a-session-token")

    response = await client.post("/triage", json={"text": "Where is my order?"})

    _assert_auth_required(response)
    assert service.calls == []


# --- validation (TRI-05, TRI-90) -----------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"text": ""},
        {"text": "   \n\t "},
        {"conversation_id": "3f0e4b6c-0000-4000-8000-000000000000"},
        {"text": "Where is my order?", "conversation_id": "3f0e4b6c-0000-4000-8000-000000000000"},
        {},
    ],
    ids=["empty", "blank", "conversation_id_only", "extra_conversation_id", "missing_text"],
)
async def test_invalid_body_is_422_without_calling_the_llm(
    client: httpx.AsyncClient,
    service: RecordingTriageService,
    db_session: AsyncSession,
    body: dict[str, str],
) -> None:
    await _login_persona(client, db_session)

    response = await client.post("/triage", json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert service.calls == []


async def test_text_longer_than_the_limit_is_422_without_calling_the_llm(
    client: httpx.AsyncClient, service: RecordingTriageService, db_session: AsyncSession
) -> None:
    await _login_persona(client, db_session)
    too_long = "x" * (get_settings().max_message_chars + 1)

    response = await client.post("/triage", json={"text": too_long})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert service.calls == []


# --- unavailable (TRI-07) ------------------------------------------------------------------


async def test_unavailable_triage_is_503_with_the_error_envelope_only(
    client: httpx.AsyncClient, service: RecordingTriageService, db_session: AsyncSession
) -> None:
    await _login_persona(client, db_session)
    service.error = AtlasError(ErrorCode.TRIAGE_UNAVAILABLE, 503)

    response = await client.post("/triage", json={"text": "Where is my order?"})

    assert response.status_code == 503
    body = response.json()
    assert list(body) == ["error"]
    assert body["error"]["code"] == "TRIAGE_UNAVAILABLE"
    assert body["error"]["trace_id"] == response.headers["x-trace-id"]
    assert "categoria" not in response.text
    assert service.calls == ["Where is my order?"]
