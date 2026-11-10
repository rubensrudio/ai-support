"""OpenTelemetry tracing exported to Langfuse over OTLP HTTP (CT-62, DA-25, OBS-01, OBS-91, OBS-93).

Contract (orchestrator skeleton; TASK-132 implements it without changing signatures):

- `init_tracing(settings)`: builds the module tracer provider. With `LANGFUSE_HOST`,
  `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` all set, it adds a `BatchSpanProcessor` with an
  OTLP HTTP exporter to `{LANGFUSE_HOST}/api/public/otel/v1/traces` (Basic auth
  `public:secret`). Without any of them, export is off and spans are still created (tests and
  local dev). Idempotent: a second call does not add a second exporter.
- `start_span(name, **attributes)`: context manager that starts a span, makes it current and
  ends it on exit (an exception is recorded on the span and re-raised). With no active span the
  new span is a true root span (no parent span id) whose trace id is `int(get_trace_id(), 16)`
  (custom `IdGenerator`), so the OTel trace id in hex is always the request trace id of the logs
  (CT-2) and Langfuse sees the request span as the trace root. String
  attribute values pass through `mask_pii` before being set (OBS-91); `None` values are skipped.
  The bound session id (see `bind_session_id`) is added as `ATTR_SESSION_ID` to every span.
- `bind_session_id(session_id)` / `reset_session_id(token)`: bind the conversation id for the
  rest of the request; binding also sets `ATTR_SESSION_ID` on the current span when it records.
- `add_span_exporter(exporter)`: attaches a `SimpleSpanProcessor(exporter)` to the module
  provider (tests use `InMemorySpanExporter`) and returns a callable that detaches it.
- Export never blocks the request and never raises: an export failure only logs the warning
  event `telemetry_dropped` (OBS-93).

The module owns its `TracerProvider`; it never calls `trace.set_tracer_provider`, so tests can
create and discard exporters freely.
"""

from __future__ import annotations

import base64
import contextvars
import logging
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
from opentelemetry.trace import Span, Status, StatusCode
from opentelemetry.util.types import AttributeValue

from api.guardrails.pii import mask_pii
from api.observability.logging import LOGGER_NAME, get_trace_id

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import Span as SdkSpan

    from api.config import Settings

ATTR_SESSION_ID = "langfuse.session.id"
ATTR_MODEL = "gen_ai.request.model"
ATTR_INPUT_TOKENS = "gen_ai.usage.input_tokens"
ATTR_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
ATTR_COST = "atlas.cost_usd"
ATTR_FALLBACK = "atlas.fallback"

TRACER_NAME = "atlas"
OTLP_TRACES_PATH = "/api/public/otel/v1/traces"
TELEMETRY_DROPPED_EVENT = "telemetry_dropped"
SERVICE_NAME = "atlas-api"

_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "atlas_session_id", default=None
)


def _log_dropped(reason: str, spans: int) -> None:
    """Warning `telemetry_dropped`: only the failure class and span count, never payloads."""
    logging.getLogger(LOGGER_NAME).warning(
        TELEMETRY_DROPPED_EVENT,
        extra={"event": TELEMETRY_DROPPED_EVENT, "reason": reason, "spans": spans},
    )


class _SafeExporter(SpanExporter):
    """Wraps an exporter so a failed or raising export only logs `telemetry_dropped` (OBS-93)."""

    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            result = self._inner.export(spans)
        except Exception as exc:  # export must never propagate (OBS-93)
            _log_dropped(type(exc).__name__, len(spans))
            return SpanExportResult.FAILURE
        if result is not SpanExportResult.SUCCESS:
            _log_dropped("export_failed", len(spans))
        return result

    def shutdown(self) -> None:
        try:
            self._inner.shutdown()
        except Exception as exc:
            _log_dropped(type(exc).__name__, 0)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            return self._inner.force_flush(timeout_millis)
        except Exception as exc:
            _log_dropped(type(exc).__name__, 0)
            return False


class _MultiplexProcessor(SpanProcessor):
    """Processor whose children can be detached (the SDK provider cannot remove processors)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processors: tuple[SpanProcessor, ...] = ()

    def add(self, processor: SpanProcessor) -> None:
        with self._lock:
            self._processors = (*self._processors, processor)

    def remove(self, processor: SpanProcessor) -> bool:
        with self._lock:
            if processor not in self._processors:
                return False
            self._processors = tuple(p for p in self._processors if p is not processor)
            return True

    def on_start(self, span: SdkSpan, parent_context: otel_context.Context | None = None) -> None:
        for processor in self._processors:
            processor.on_start(span, parent_context=parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        for processor in self._processors:
            processor.on_end(span)

    def shutdown(self) -> None:
        for processor in self._processors:
            processor.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return all(p.force_flush(timeout_millis) for p in self._processors)


class _RequestTraceIdGenerator(RandomIdGenerator):
    """Root spans take the request trace id (CT-2); children inherit it from their parent.

    The SDK only asks for a trace id when the new span has no valid parent, so the request span
    is a true root (no parent span id) whose hex trace id equals `get_trace_id()`. A trace id
    that is not valid 128-bit hex falls back to a random one.
    """

    def generate_trace_id(self) -> int:
        try:
            trace_id = int(get_trace_id(), 16)
        except ValueError:
            return super().generate_trace_id()
        if not 0 < trace_id < 2**128:
            return super().generate_trace_id()
        return trace_id


_provider = TracerProvider(
    resource=Resource.create({"service.name": SERVICE_NAME}),
    id_generator=_RequestTraceIdGenerator(),
)
_processors = _MultiplexProcessor()
_provider.add_span_processor(_processors)
_tracer = _provider.get_tracer(TRACER_NAME)
_init_lock = threading.Lock()
_remote_processor: SpanProcessor | None = None


def _build_otlp_exporter(endpoint: str, headers: dict[str, str]) -> SpanExporter:
    """OTLP/HTTP protobuf exporter (no network until the first export)."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(endpoint=endpoint, headers=headers, timeout=5)


def _shutdown_remote() -> None:
    """Detach and shut down the Langfuse exporter (tests and process shutdown)."""
    global _remote_processor
    with _init_lock:
        processor, _remote_processor = _remote_processor, None
    if processor is not None and _processors.remove(processor):
        processor.shutdown()


SHUTDOWN_TIMEOUT_SECONDS = 3.0


def shutdown_tracing(timeout_seconds: float = SHUTDOWN_TIMEOUT_SECONDS) -> None:
    """Detach the Langfuse exporter and export what is queued, waiting at most `timeout_seconds`.

    `BatchSpanProcessor.shutdown` exports the queue and joins its worker thread without a usable
    deadline, so it runs in a daemon thread: when Langfuse is down this returns after the timeout
    and the process can still exit within the container grace period (OBS-93).
    """
    worker = threading.Thread(target=_shutdown_remote, name="atlas-tracing-shutdown", daemon=True)
    worker.start()
    worker.join(timeout_seconds)


def init_tracing(settings: Settings) -> None:
    """Install the Langfuse OTLP exporter when host and both keys are set (idempotent)."""
    global _remote_processor
    host = (settings.langfuse_host or "").strip()
    public = settings.langfuse_public_key.get_secret_value() if settings.langfuse_public_key else ""
    secret = settings.langfuse_secret_key.get_secret_value() if settings.langfuse_secret_key else ""
    if not (host and public and secret):
        return
    with _init_lock:
        if _remote_processor is not None:
            return
        token = base64.b64encode(f"{public}:{secret}".encode()).decode()
        exporter = _build_otlp_exporter(
            host.rstrip("/") + OTLP_TRACES_PATH, {"Authorization": f"Basic {token}"}
        )
        _remote_processor = BatchSpanProcessor(_SafeExporter(exporter))
        _processors.add(_remote_processor)


def _mask(value: str) -> str:
    return mask_pii(value).text


def _attribute(value: object) -> AttributeValue | None:
    if value is None:
        return None
    if isinstance(value, str):
        return _mask(value)
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, list | tuple):
        items = [v for v in value if v is not None]
        if all(isinstance(v, str) for v in items):
            return [_mask(str(v)) for v in items]
        for kind in (bool, int, float):
            if all(type(v) is kind for v in items):
                return list(items)
        return [_mask(str(v)) for v in items]
    return _mask(str(value))


def _attributes(raw: Mapping[str, object]) -> dict[str, AttributeValue]:
    attributes: dict[str, AttributeValue] = {}
    for key, value in raw.items():
        converted = _attribute(value)
        if converted is not None:
            attributes[key] = converted
    session_id = _session_id.get()
    if session_id is not None:
        attributes[ATTR_SESSION_ID] = session_id
    return attributes


def _record_masked_exception(span: Span, exc: BaseException) -> None:
    """Record only the exception type and its PII-masked message (OBS-91).

    The SDK's `record_exception` stores the raw message and the full stacktrace (including the
    `__cause__` chain: SQL, provider text, user data) unmasked, so it is never used here.
    """
    type_name = type(exc).__name__
    span.add_event(
        "exception",
        attributes={"exception.type": type_name, "exception.message": _mask(str(exc))},
    )
    span.set_status(Status(StatusCode.ERROR, type_name))


@contextmanager
def start_span(name: str, **attributes: object) -> Iterator[Span]:
    """Start a current span; masks string attributes and records masked exceptions (re-raised)."""
    with _tracer.start_as_current_span(
        name,
        attributes=_attributes(attributes),
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except Exception as exc:
            _record_masked_exception(span, exc)
            raise


def bind_session_id(session_id: str) -> contextvars.Token[str | None]:
    token = _session_id.set(session_id)
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute(ATTR_SESSION_ID, session_id)
    return token


def reset_session_id(token: contextvars.Token[str | None]) -> None:
    _session_id.reset(token)


def add_span_exporter(exporter: SpanExporter) -> Callable[[], None]:
    """Attach `SimpleSpanProcessor(exporter)`; the returned callable detaches it."""
    processor = SimpleSpanProcessor(_SafeExporter(exporter))
    _processors.add(processor)

    def detach() -> None:
        _processors.remove(processor)

    return detach
