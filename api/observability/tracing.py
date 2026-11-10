"""OpenTelemetry tracing exported to Langfuse over OTLP HTTP (CT-62, DA-25, OBS-01, OBS-91, OBS-93).

Contract (orchestrator skeleton; TASK-132 implements it without changing signatures):

- `init_tracing(settings)`: builds the module tracer provider. With `LANGFUSE_HOST`,
  `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` all set, it adds a `BatchSpanProcessor` with an
  OTLP HTTP exporter to `{LANGFUSE_HOST}/api/public/otel/v1/traces` (Basic auth
  `public:secret`). Without any of them, export is off and spans are still created (tests and
  local dev). Idempotent: a second call does not add a second exporter.
- `start_span(name, **attributes)`: context manager that starts a span, makes it current and
  ends it on exit (an exception is recorded on the span and re-raised). With no active span the
  new span is parented to a synthetic remote context whose trace id is `int(get_trace_id(), 16)`,
  so the OTel trace id in hex is always the request trace id of the logs (CT-2). String
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

import contextvars
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from opentelemetry.trace import Span

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export import SpanExporter

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

_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "atlas_session_id", default=None
)


def init_tracing(settings: Settings) -> None:
    """Skeleton no-op so `create_app` keeps working until TASK-132 lands."""
    return None


@contextmanager
def start_span(name: str, **attributes: object) -> Iterator[Span]:
    raise NotImplementedError("TASK-132")
    yield  # pragma: no cover


def bind_session_id(session_id: str) -> contextvars.Token[str | None]:
    raise NotImplementedError("TASK-132")


def reset_session_id(token: contextvars.Token[str | None]) -> None:
    _session_id.reset(token)


def add_span_exporter(exporter: SpanExporter) -> Callable[[], None]:
    raise NotImplementedError("TASK-132")
