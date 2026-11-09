import json
import logging
from collections.abc import Iterator

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from api.config import Settings
from api.observability import logging as atlas_logging
from api.observability.logging import (
    JsonFormatter,
    SecretRedactionFilter,
    configure_logging,
    get_trace_id,
    log_event,
)
from api.observability.middleware import TraceIdMiddleware


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[dict[str, object]] = []
        self.setFormatter(JsonFormatter())
        self.addFilter(SecretRedactionFilter())

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(self.format(record)))


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Iterator[_ListHandler]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-secret-123")
    configure_logging(Settings(_env_file=None))  # type: ignore[call-arg]
    handler = _ListHandler()
    logger = logging.getLogger(atlas_logging.LOGGER_NAME)
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        monkeypatch.setattr(atlas_logging, "_secret_values", ())


async def _endpoint(request: Request) -> JSONResponse:
    log_event("first")
    log_event("second")
    return JSONResponse({"trace_id": get_trace_id()})


def _client() -> httpx.AsyncClient:
    app = Starlette(routes=[Route("/", _endpoint)])
    app.add_middleware(TraceIdMiddleware)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_logs_in_one_request_share_the_response_trace_id(captured: _ListHandler) -> None:
    async with _client() as client:
        response = await client.get("/")
    header = response.headers["X-Trace-Id"]
    events = [line for line in captured.lines if line["event"] in {"first", "second"}]
    assert len(events) == 2
    assert {line["trace_id"] for line in events} == {header}
    assert response.json()["trace_id"] == header


async def test_two_requests_get_different_trace_ids(captured: _ListHandler) -> None:
    async with _client() as client:
        first = await client.get("/")
        second = await client.get("/")
    assert first.headers["X-Trace-Id"] != second.headers["X-Trace-Id"]
    assert len(first.headers["X-Trace-Id"]) == 32


def test_secret_values_are_redacted(captured: _ListHandler) -> None:
    log_event("x", api_key="sk-ant-test-secret-123", nested={"k": "Bearer sk-ant-test-secret-123"})
    line = captured.lines[-1]
    assert line["api_key"] == "***"
    assert line["nested"] == {"k": "Bearer ***"}
    assert "sk-ant-test-secret-123" not in json.dumps(captured.lines)


def test_log_event_outputs_json_with_event_and_trace_id(captured: _ListHandler) -> None:
    log_event("llm_call", model="m", input_tokens=3)
    line = captured.lines[-1]
    assert line["event"] == "llm_call"
    assert line["model"] == "m"
    assert line["input_tokens"] == 3
    assert isinstance(line["trace_id"], str) and line["trace_id"]


def test_reserved_field_names_do_not_break_logging(captured: _ListHandler) -> None:
    log_event("x", message="hello", name="n")
    line = captured.lines[-1]
    assert line["field_message"] == "hello"
    assert line["field_name"] == "n"
