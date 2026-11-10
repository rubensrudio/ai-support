"""Instrumented LLM client (CT-9, FND-04, CHAT-05).

Decorates any `LLMClient` and emits exactly one `llm_call` event per call (success, error, a
cancelled task, or a stream the consumer stopped early) with trace id, provider, model,
estimated context tokens, provider token usage, estimated cost and latency.

Cost is never silently zero (AS-2):
- a model missing from the pricing table keeps the response and logs `cost_usd=None` with
  `pricing_error="unknown_model"` plus a `pricing_missing` event;
- a subscription CLI provider (`claude_cli`, `codex_cli`, LAC-43) has no per-token price: the
  pricing table is not consulted and a call logs `cost_usd=None` with
  `pricing_error="subscription"`;
- a failed call logs `cost_usd=0` only when the provider rejected the request (non-recoverable
  4xx); any other failure (timeout, connection, 5xx, unknown) may have been billed, so it logs
  `cost_usd=None` with `pricing_error="cost_unknown"`.

Phase 14 (TASK-133, OBS-01): every call is also one `llm` span (CT-62) with the model, the
provider, input/output/cache tokens, the estimated cost (`ATTR_COST` and `gen_ai.usage.cost`, as a
float; no attribute when the cost is unknown), the latency, the outcome and the prompt (system
blocks and messages, masked as a whole and then cut at `PROMPT_ATTRIBUTE_MAX_CHARS`). The span is
opened in a copy of the current context (`_DetachedSpan`), so it never becomes the current span
of the caller across the `yield`s of a stream and closing it from another task or context cannot
fail with `Failed to detach context`.
"""

import asyncio
import contextvars
import json
import time
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager
from dataclasses import replace
from decimal import Decimal

from opentelemetry.trace import Span, Status, StatusCode

from api.config import SUBSCRIPTION_PROVIDERS
from api.guardrails.pii import mask_pii
from api.llm.client import (
    LLMClient,
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    Usage,
)
from api.llm.pricing import UnknownModelPricing, estimate_cost
from api.llm.tokens import count_messages
from api.observability.logging import get_trace_id, log_event
from api.observability.tracing import (
    ATTR_COST,
    ATTR_INPUT_TOKENS,
    ATTR_MODEL,
    ATTR_OUTPUT_TOKENS,
    start_span,
)

OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"
OUTCOME_CANCELLED = "cancelled"
COST_UNKNOWN = "cost_unknown"
SUBSCRIPTION = "subscription"

SPAN_NAME = "llm"
PROMPT_ATTRIBUTE_MAX_CHARS = 8000
ATTR_SYSTEM = "gen_ai.system"
ATTR_PROMPT = "gen_ai.prompt"
ATTR_PURPOSE = "atlas.purpose"
ATTR_CACHE_READ_TOKENS = "gen_ai.usage.cache_read_input_tokens"
ATTR_CACHE_WRITE_TOKENS = "gen_ai.usage.cache_creation_input_tokens"
ATTR_USAGE_COST = "gen_ai.usage.cost"
ATTR_LATENCY_MS = "atlas.latency_ms"
ATTR_OUTCOME = "atlas.outcome"
ATTR_PRICING_ERROR = "atlas.pricing_error"
ATTR_ERROR_TYPE = "error.type"


def _prompt(request: LLMRequest) -> str:
    """System blocks and messages as JSON, masked, then cut to a bounded size."""
    payload = {
        "system": [block.text for block in request.system],
        "messages": [
            {
                "role": message.role,
                "content": message.content,
                **(
                    {"tool_calls": [call.name for call in message.tool_calls]}
                    if message.tool_calls
                    else {}
                ),
            }
            for message in request.messages
        ],
    }
    text = json.dumps(payload, ensure_ascii=False, default=str)
    # Mask the whole text before cutting: PII split by the cut would no longer match (OBS-91).
    return mask_pii(text).text[:PROMPT_ATTRIBUTE_MAX_CHARS]


class _DetachedSpan:
    """A `start_span` entered and exited inside its own copy of the current context.

    The parent is the span current when it opens (the request span), but the caller's context
    never changes: attach and detach both happen in the copied context, whatever task ends it.
    """

    def __init__(self, name: str, **attributes: object) -> None:
        # Bind the trace id in the caller's context first: outside a request `get_trace_id()`
        # binds a fresh id, which must be the one of the `llm_call` log, not one of the copy.
        get_trace_id()
        self._context = contextvars.copy_context()
        self._manager: AbstractContextManager[Span] = start_span(name, **attributes)
        self.span: Span = self._context.run(self._manager.__enter__)
        self._closed = False

    def close(self) -> None:
        """End the span. Errors are recorded by `_record` (type only): the exception itself is
        never handed to the span, so a provider message cannot reach the trace unmasked."""
        if self._closed:
            return
        self._closed = True
        self._context.run(self._manager.__exit__, None, None, None)


def _is_rejection(exc: BaseException) -> bool:
    """The provider refused the request (4xx, not retryable), so nothing was billed."""
    return (
        isinstance(exc, LLMError)
        and not exc.recoverable
        and exc.status is not None
        and 400 <= exc.status < 500
    )


class InstrumentedLLMClient:
    def __init__(self, inner: LLMClient, model: str, provider: str) -> None:
        self.inner = inner
        self.model = model
        self.provider = provider

    def _open_span(self, request: LLMRequest) -> _DetachedSpan:
        return _DetachedSpan(
            SPAN_NAME,
            **{
                ATTR_MODEL: self.model,
                ATTR_SYSTEM: self.provider,
                ATTR_PURPOSE: request.purpose,
                ATTR_PROMPT: _prompt(request),
            },
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        context_tokens = count_messages(request.system, request.messages)
        started = time.perf_counter()
        scope = self._open_span(request)
        try:
            response = await self.inner.complete(request)
        except asyncio.CancelledError:
            # The call was in flight: the provider may bill it, so the cost is unknown.
            self._log_cancelled(request, context_tokens, started, None, scope.span)
            scope.close()
            raise
        except Exception as exc:
            self._log_error(request, context_tokens, started, exc, scope.span)
            scope.close()
            raise
        self._log_success(request, context_tokens, started, response.usage, scope.span)
        scope.close()
        return response

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        context_tokens = count_messages(request.system, request.messages)
        started = time.perf_counter()
        usage: Usage | None = None
        logged = False
        inner = self.inner.stream(request)
        # After `stream()`: a provider that cannot stream raises there, before any span opens.
        scope = self._open_span(request)
        try:
            async for event in inner:
                if event.kind == "usage" and event.usage is not None:
                    usage = event.usage
                    if event.model is None or event.provider is None:
                        event = replace(event, model=self.model, provider=self.provider)
                yield event
            logged = True
            self._log_success(
                request,
                context_tokens,
                started,
                usage or Usage(input_tokens=0, output_tokens=0),
                scope.span,
            )
            scope.close()
        except Exception as exc:
            if not logged:
                logged = True
                self._log_error(request, context_tokens, started, exc, scope.span)
            scope.close()
            raise
        finally:
            # Reached without a log on early stop: `break`/`aclose()` (GeneratorExit) or task
            # cancellation (CancelledError). Close the inner stream so the provider call ends.
            if not logged:
                logged = True
                self._log_cancelled(request, context_tokens, started, usage, scope.span)
                scope.close()
                aclose = getattr(inner, "aclose", None)
                if aclose is not None:
                    await aclose()
            scope.close()

    def _cost(self, usage: Usage) -> tuple[Decimal | None, str | None]:
        if self.provider in SUBSCRIPTION_PROVIDERS:
            return None, SUBSCRIPTION
        try:
            return estimate_cost(self.model, usage), None
        except UnknownModelPricing:
            log_event("pricing_missing", model=self.model, provider=self.provider)
            return None, "unknown_model"
        except Exception as exc:
            # A broken pricing table must not discard a response the provider already billed.
            log_event(
                "pricing_error",
                model=self.model,
                provider=self.provider,
                error_type=type(exc).__name__,
            )
            return None, "pricing_unavailable"

    def _log_success(
        self,
        request: LLMRequest,
        context_tokens: int,
        started: float,
        usage: Usage,
        span: Span | None = None,
    ) -> None:
        cost, pricing_error = self._cost(usage)
        extra: dict[str, object] = {}
        if pricing_error is not None:
            extra["pricing_error"] = pricing_error
        self._emit(request, context_tokens, started, usage, cost, OUTCOME_OK, extra, span)

    def _log_cancelled(
        self,
        request: LLMRequest,
        context_tokens: int,
        started: float,
        usage: Usage | None,
        span: Span | None = None,
    ) -> None:
        extra: dict[str, object] = {}
        cost: Decimal | None
        pricing_error: str | None
        if usage is None:
            # Stopped before the provider reported usage: tokens may have been billed anyway.
            usage = Usage(input_tokens=0, output_tokens=0)
            cost, pricing_error = None, COST_UNKNOWN
        else:
            cost, pricing_error = self._cost(usage)
        if pricing_error is not None:
            extra["pricing_error"] = pricing_error
        self._emit(request, context_tokens, started, usage, cost, OUTCOME_CANCELLED, extra, span)

    def _log_error(
        self,
        request: LLMRequest,
        context_tokens: int,
        started: float,
        exc: Exception,
        span: Span | None = None,
    ) -> None:
        extra: dict[str, object] = {"error_type": type(exc).__name__}
        if isinstance(exc, LLMError):
            extra["status"] = exc.status
            extra["recoverable"] = exc.recoverable
        cost: Decimal | None = Decimal("0")
        if not _is_rejection(exc):
            cost = None
            extra["pricing_error"] = COST_UNKNOWN
        # A failed call returns no usage, so token counts are 0.
        usage = Usage(input_tokens=0, output_tokens=0)
        self._emit(request, context_tokens, started, usage, cost, OUTCOME_ERROR, extra, span)

    def _emit(
        self,
        request: LLMRequest,
        context_tokens: int,
        started: float,
        usage: Usage,
        cost: Decimal | None,
        outcome: str,
        extra: dict[str, object],
        span: Span | None = None,
    ) -> None:
        # `log_event` adds the current trace id.
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        if span is not None:
            _record(span, usage, cost, latency_ms, outcome, extra)
        log_event(
            "llm_call",
            provider=self.provider,
            model=self.model,
            purpose=request.purpose,
            context_tokens=context_tokens,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            outcome=outcome,
            **extra,
        )


def _record(
    span: Span,
    usage: Usage,
    cost: Decimal | None,
    latency_ms: float,
    outcome: str,
    extra: dict[str, object],
) -> None:
    """Usage, cost, latency and outcome on the `llm` span (never an error message or payload)."""
    span.set_attribute(ATTR_INPUT_TOKENS, usage.input_tokens)
    span.set_attribute(ATTR_OUTPUT_TOKENS, usage.output_tokens)
    span.set_attribute(ATTR_CACHE_READ_TOKENS, usage.cache_read_tokens)
    span.set_attribute(ATTR_CACHE_WRITE_TOKENS, usage.cache_write_tokens)
    if cost is not None:
        span.set_attribute(ATTR_COST, float(cost))
        span.set_attribute(ATTR_USAGE_COST, float(cost))
    span.set_attribute(ATTR_LATENCY_MS, latency_ms)
    span.set_attribute(ATTR_OUTCOME, outcome)
    pricing_error = extra.get("pricing_error")
    if isinstance(pricing_error, str):
        span.set_attribute(ATTR_PRICING_ERROR, pricing_error)
    error_type = extra.get("error_type")
    if isinstance(error_type, str):
        span.set_attribute(ATTR_ERROR_TYPE, error_type)
        span.set_status(Status(StatusCode.ERROR, error_type))
