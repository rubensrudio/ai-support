"""Error catalog, error envelope and bilingual user messages (TASK-003, CT-3, CT-4)."""

from typing import get_args

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from api import errors
from api.errors import AtlasError, ErrorCode, install_error_handlers
from api.messages import Lang, MessageKey, user_message
from api.observability.logging import get_trace_id
from api.observability.middleware import TraceIdMiddleware

CATALOG_HTTP = {
    "VALIDATION_ERROR": 422,
    "MESSAGE_TOO_LONG": 422,
    "AUTH_REQUIRED": 401,
    "INVALID_CREDENTIALS": 401,
    "CONVERSATION_NOT_FOUND": 404,
    "CONFIRMATION_NOT_FOUND": 404,
    "CONFIRMATION_NOT_PENDING": 409,
    "MESSAGE_NOT_FOUND": 404,
    "RATE_LIMITED": 429,
    "DAILY_CAP_REACHED": 429,
    "LLM_UNAVAILABLE": 502,
    "TRIAGE_UNAVAILABLE": 503,
    "INTERNAL_ERROR": 500,
}


class _Body(BaseModel):
    message: str


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(TraceIdMiddleware)
    install_error_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret-internal-detail at /srv/app.py line 42")

    @app.get("/llm-down")
    async def llm_down() -> dict[str, str]:
        raise AtlasError(ErrorCode.LLM_UNAVAILABLE, 502)

    @app.get("/too-long")
    async def too_long() -> None:
        raise AtlasError(ErrorCode.MESSAGE_TOO_LONG, 422, {"max_chars": 4000})

    @app.get("/missing-param")
    async def missing_param() -> None:
        raise AtlasError(ErrorCode.MESSAGE_TOO_LONG, 422)

    @app.get("/trace")
    async def trace() -> dict[str, str]:
        return {"trace_id": get_trace_id()}

    @app.post("/echo")
    async def echo(body: _Body) -> dict[str, str]:
        return {"message": body.message}

    return app


@pytest.fixture
def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=_build_app(), raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _assert_envelope(response: httpx.Response, code: str) -> dict[str, str]:
    body = response.json()
    assert set(body) == {"error"}
    error = body["error"]
    assert set(error) == {"code", "message", "trace_id"}
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    assert error["trace_id"] == response.headers["X-Trace-Id"]
    return error  # type: ignore[no-any-return]


def test_error_code_catalog_matches_plan() -> None:
    assert {code.value for code in ErrorCode} >= {*CATALOG_HTTP, "STREAM_ERROR"}
    for name, status in CATALOG_HTTP.items():
        assert ErrorCode(name).http_status == status
    assert ErrorCode.STREAM_ERROR.http_status is None


async def test_unhandled_exception_becomes_500_without_traceback(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(errors, "log_event", lambda event, **f: events.append((event, f)))

    async with client:
        response = await client.get("/boom")

    assert response.status_code == 500
    error = _assert_envelope(response, "INTERNAL_ERROR")
    assert "Traceback" not in response.text
    assert "secret-internal-detail" not in response.text
    assert error["message"] == user_message(MessageKey.INTERNAL_ERROR, "en")
    assert ("unhandled_error", {"exc_type": "RuntimeError"}) in events


async def test_atlas_error_returns_status_and_current_trace_id(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        response = await client.get("/llm-down")

    assert response.status_code == 502
    error = _assert_envelope(response, "LLM_UNAVAILABLE")
    assert error["message"] == user_message(MessageKey.LLM_UNAVAILABLE, "en")


async def test_atlas_error_params_interpolate_message(client: httpx.AsyncClient) -> None:
    async with client:
        response = await client.get("/too-long")

    assert response.status_code == 422
    error = _assert_envelope(response, "MESSAGE_TOO_LONG")
    assert "4000" in error["message"]


async def test_missing_message_param_still_returns_envelope(client: httpx.AsyncClient) -> None:
    async with client:
        response = await client.get("/missing-param")

    assert response.status_code == 422
    _assert_envelope(response, "MESSAGE_TOO_LONG")


async def test_request_validation_error_becomes_validation_error(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        response = await client.post("/echo", json={"wrong": 1})

    assert response.status_code == 422
    _assert_envelope(response, "VALIDATION_ERROR")
    assert "wrong" not in response.json()["error"]["message"]


async def test_unknown_route_and_method_use_envelope(client: httpx.AsyncClient) -> None:
    async with client:
        not_found = await client.get("/does-not-exist")
        not_allowed = await client.delete("/trace")

    assert not_found.status_code == 404
    _assert_envelope(not_found, "NOT_FOUND")
    assert not_allowed.status_code == 405
    _assert_envelope(not_allowed, "METHOD_NOT_ALLOWED")


async def test_success_response_keeps_trace_header(client: httpx.AsyncClient) -> None:
    async with client:
        response = await client.get("/trace")

    assert response.json()["trace_id"] == response.headers["X-Trace-Id"]


@pytest.mark.parametrize("key", list(MessageKey))
@pytest.mark.parametrize("lang", get_args(Lang))
def test_every_message_key_has_text_in_both_languages(key: MessageKey, lang: Lang) -> None:
    from api.messages import message_template

    assert message_template(key, lang).strip()


def test_every_error_code_has_a_user_message() -> None:
    for code in ErrorCode:
        assert MessageKey(code.value)


def test_messages_differ_between_languages() -> None:
    assert user_message(MessageKey.OUT_OF_CORPUS, "pt") != user_message(
        MessageKey.OUT_OF_CORPUS, "en"
    )


def test_unsupported_language_message_is_always_english() -> None:
    en = user_message(MessageKey.UNSUPPORTED_LANGUAGE, "en")
    assert user_message(MessageKey.UNSUPPORTED_LANGUAGE, "pt") == en
    assert "Portuguese" in en and "English" in en


def test_user_message_interpolates_params() -> None:
    assert "4000" in user_message(MessageKey.MESSAGE_TOO_LONG, "pt", max_chars=4000)
    confirm = user_message(MessageKey.CONFIRM_ACTION, "en", action="cancel", target="order 7")
    assert "cancel" in confirm and "order 7" in confirm


def test_user_message_requires_declared_params() -> None:
    with pytest.raises(KeyError):
        user_message(MessageKey.MESSAGE_TOO_LONG, "en")
