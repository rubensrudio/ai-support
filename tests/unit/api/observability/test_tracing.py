import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator, Iterator, Sequence

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from api.config import Settings
from api.observability import logging as atlas_logging
from api.observability import tracing
from api.observability.logging import (
    JsonFormatter,
    bind_trace_id,
    get_trace_id,
    new_trace_id,
    reset_trace_id,
)
from api.observability.middleware import TRACE_HEADER, TraceIdMiddleware
from api.observability.tracing import (
    ATTR_MODEL,
    ATTR_SESSION_ID,
    OTLP_TRACES_PATH,
    TELEMETRY_DROPPED_EVENT,
    add_span_exporter,
    bind_session_id,
    init_tracing,
    reset_session_id,
    start_span,
)

CARD = "4111 1111 1111 1111"


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[dict[str, object]] = []
        self.setFormatter(JsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        line = json.loads(self.format(record))
        line["_level"] = record.levelname
        self.lines.append(line)


class _RaisingExporter(SpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        raise ConnectionError("collector unreachable")

    def shutdown(self) -> None:
        return None


class _FailingExporter(SpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        return None


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    memory = InMemorySpanExporter()
    detach = add_span_exporter(memory)
    token = bind_trace_id(new_trace_id())
    try:
        yield memory
    finally:
        reset_trace_id(token)
        detach()


@pytest.fixture
def logs() -> Iterator[_ListHandler]:
    handler = _ListHandler()
    logger = logging.getLogger(atlas_logging.LOGGER_NAME)
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_start_span_sets_attributes_and_uses_request_trace_id(
    exporter: InMemorySpanExporter,
) -> None:
    with start_span("llm", **{ATTR_MODEL: "fake-model", "tokens": 12, "skipped": None}):
        pass

    (span,) = exporter.get_finished_spans()
    assert span.name == "llm"
    assert span.attributes is not None
    assert span.attributes[ATTR_MODEL] == "fake-model"
    assert span.attributes["tokens"] == 12
    assert "skipped" not in span.attributes
    assert format(span.context.trace_id, "032x") == get_trace_id()


def test_child_span_inherits_parent_trace(exporter: InMemorySpanExporter) -> None:
    with start_span("request") as parent, start_span("retrieval") as child:
        assert child.get_span_context().trace_id == parent.get_span_context().trace_id

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert spans["retrieval"].parent is not None
    assert spans["retrieval"].parent.span_id == spans["request"].context.span_id
    assert format(spans["retrieval"].context.trace_id, "032x") == get_trace_id()


def test_string_attributes_are_pii_masked(exporter: InMemorySpanExporter) -> None:
    with start_span(
        "tool",
        prompt=f"my card is {CARD}",
        chunks=[f"card {CARD}", "plain"],
        payload={"card": CARD},
    ):
        pass

    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert span.attributes["prompt"] == "my card is [CARD]"
    assert tuple(span.attributes["chunks"]) == ("card [CARD]", "plain")  # type: ignore[arg-type]
    assert CARD not in str(span.attributes["payload"])
    assert "[CARD]" in str(span.attributes["payload"])


def test_exception_is_recorded_and_reraised(exporter: InMemorySpanExporter) -> None:
    with pytest.raises(ValueError, match="boom"), start_span("llm"):
        raise ValueError("boom")

    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert any(event.name == "exception" for event in span.events)


def test_session_id_is_added_to_spans(exporter: InMemorySpanExporter) -> None:
    with start_span("request") as root:
        token = bind_session_id("conv-123")
        try:
            with start_span("llm"):
                pass
        finally:
            reset_session_id(token)
        assert root.is_recording()
    with start_span("after"):
        pass

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert spans["request"].attributes is not None
    assert spans["request"].attributes[ATTR_SESSION_ID] == "conv-123"
    assert spans["llm"].attributes is not None
    assert spans["llm"].attributes[ATTR_SESSION_ID] == "conv-123"
    assert spans["after"].attributes is not None
    assert ATTR_SESSION_ID not in spans["after"].attributes


def test_detached_exporter_receives_nothing(exporter: InMemorySpanExporter) -> None:
    other = InMemorySpanExporter()
    detach = add_span_exporter(other)
    detach()
    with start_span("llm"):
        pass
    assert other.get_finished_spans() == ()
    assert len(exporter.get_finished_spans()) == 1


def test_raising_exporter_never_propagates_and_logs_dropped(
    exporter: InMemorySpanExporter, logs: _ListHandler
) -> None:
    detach = add_span_exporter(_RaisingExporter())
    try:
        with start_span("llm"):
            pass
    finally:
        detach()

    assert len(exporter.get_finished_spans()) == 1
    dropped = [line for line in logs.lines if line["event"] == TELEMETRY_DROPPED_EVENT]
    assert dropped
    assert dropped[0]["_level"] == "WARNING"
    assert "collector unreachable" not in json.dumps(dropped)


def test_failed_export_result_logs_dropped(logs: _ListHandler) -> None:
    safe = tracing._SafeExporter(_FailingExporter())
    assert safe.export([]) is SpanExportResult.FAILURE
    assert any(line["event"] == TELEMETRY_DROPPED_EVENT for line in logs.lines)


@pytest.fixture
def fresh_tracing(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, str]]]:
    built: list[tuple[str, dict[str, str]]] = []

    def fake_build(endpoint: str, headers: dict[str, str]) -> SpanExporter:
        built.append((endpoint, headers))
        return InMemorySpanExporter()

    monkeypatch.setattr(tracing, "_build_otlp_exporter", fake_build)
    monkeypatch.setattr(tracing, "_remote_processor", None)
    return built


def test_init_without_keys_installs_no_remote_exporter(
    fresh_tracing: list[tuple[str, dict[str, str]]],
) -> None:
    init_tracing(_settings(langfuse_host="https://lf.example.com"))
    assert fresh_tracing == []
    assert tracing._remote_processor is None


def test_init_with_keys_targets_langfuse_otlp_with_basic_auth(
    fresh_tracing: list[tuple[str, dict[str, str]]],
) -> None:
    settings = _settings(
        langfuse_host="https://lf.example.com/",
        langfuse_public_key=SecretStr("pk-lf-1"),
        langfuse_secret_key=SecretStr("sk-lf-2"),
    )
    try:
        init_tracing(settings)
        init_tracing(settings)
        assert len(fresh_tracing) == 1
        endpoint, headers = fresh_tracing[0]
        assert endpoint == "https://lf.example.com" + OTLP_TRACES_PATH
        expected = base64.b64encode(b"pk-lf-1:sk-lf-2").decode()
        assert headers == {"Authorization": f"Basic {expected}"}
        assert tracing._remote_processor is not None
    finally:
        tracing._shutdown_remote()


def test_real_otlp_exporter_is_built_without_network() -> None:
    built = tracing._build_otlp_exporter(
        "https://lf.example.com" + OTLP_TRACES_PATH, {"Authorization": "Basic eA=="}
    )
    try:
        assert isinstance(built, SpanExporter)
    finally:
        built.shutdown()


def test_tracer_provider_is_not_global() -> None:
    assert trace.get_tracer_provider() is not tracing._provider
    resource = tracing._provider.resource.attributes
    assert resource["service.name"] == "atlas-api"


async def _ok(request: Request) -> JSONResponse:
    return JSONResponse({"trace_id": get_trace_id()})


async def _stream(request: Request) -> StreamingResponse:
    async def body() -> AsyncIterator[bytes]:
        for chunk in (b"data: a\n\n", b"data: b\n\n"):
            with start_span("chunk"):
                await asyncio.sleep(0)
            yield chunk

    return StreamingResponse(body(), media_type="text/event-stream")


async def _fail(request: Request) -> JSONResponse:
    raise RuntimeError("route failed")


def _app() -> TraceIdMiddleware:
    app = Starlette(routes=[Route("/ok", _ok), Route("/stream", _stream), Route("/fail", _fail)])
    return TraceIdMiddleware(app)


async def test_middleware_opens_root_request_span(exporter: InMemorySpanExporter) -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ok")

    assert response.status_code == 200
    header_trace = response.headers[TRACE_HEADER]
    assert response.json()["trace_id"] == header_trace
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "request"]
    assert format(span.context.trace_id, "032x") == header_trace
    assert span.parent is None
    assert span.attributes is not None
    assert span.attributes["http.method"] == "GET"
    assert span.attributes["url.path"] == "/ok"
    assert span.attributes["http.status_code"] == 200


async def test_middleware_root_span_covers_streaming_response(
    exporter: InMemorySpanExporter, logs: _ListHandler, caplog: pytest.LogCaptureFixture
) -> None:
    transport = httpx.ASGITransport(app=_app())
    with caplog.at_level(logging.ERROR):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/stream")

    assert response.status_code == 200
    assert response.text == "data: a\n\ndata: b\n\n"
    header_trace = response.headers[TRACE_HEADER]
    spans = exporter.get_finished_spans()
    (root,) = [s for s in spans if s.name == "request"]
    chunks = [s for s in spans if s.name == "chunk"]
    assert len(chunks) == 2
    for chunk in chunks:
        assert chunk.parent is not None
        assert chunk.parent.span_id == root.context.span_id
        assert format(chunk.context.trace_id, "032x") == header_trace
    assert root.attributes is not None
    assert root.attributes["http.status_code"] == 200
    assert "Failed to detach context" not in caplog.text


async def test_middleware_records_route_exception(exporter: InMemorySpanExporter) -> None:
    transport = httpx.ASGITransport(app=_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/fail")

    assert response.status_code == 500
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "request"]
    assert span.status.status_code is StatusCode.ERROR


def test_root_span_has_no_parent_and_children_share_request_trace(
    exporter: InMemorySpanExporter,
) -> None:
    with start_span("request"), start_span("llm"), start_span("tool"):
        pass

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert spans["request"].parent is None
    expected = int(get_trace_id(), 16)
    assert {s.context.trace_id for s in spans.values()} == {expected}
    assert spans["llm"].parent is not None
    assert spans["llm"].parent.span_id == spans["request"].context.span_id
    assert spans["tool"].parent is not None
    assert spans["tool"].parent.span_id == spans["llm"].context.span_id


def test_invalid_request_trace_id_falls_back_to_random(
    exporter: InMemorySpanExporter,
) -> None:
    token = bind_trace_id("not-hex")
    try:
        with start_span("request"):
            pass
    finally:
        reset_trace_id(token)

    (span,) = exporter.get_finished_spans()
    assert span.parent is None
    assert span.context.trace_id != 0


def test_exception_event_is_masked_and_has_no_stacktrace(
    exporter: InMemorySpanExporter,
) -> None:
    def fail() -> None:
        try:
            raise KeyError(f"SELECT * FROM cards WHERE number = '{CARD}'")  # noqa: S608
        except KeyError as cause:
            raise ValueError(f"payment failed for card {CARD}") from cause

    with pytest.raises(ValueError, match="payment failed"), start_span("llm"):
        fail()

    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "ValueError"
    (event,) = span.events
    assert event.name == "exception"
    assert event.attributes is not None
    assert dict(event.attributes) == {
        "exception.type": "ValueError",
        "exception.message": "payment failed for card [CARD]",
    }
    dumped = repr(
        [dict(span.attributes or {}), [(e.name, dict(e.attributes or {})) for e in span.events]]
    )
    assert CARD not in dumped
    assert CARD.replace(" ", "") not in dumped
    assert "stacktrace" not in dumped
    assert "SELECT" not in dumped
