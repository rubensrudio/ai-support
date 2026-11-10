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
"""

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from decimal import Decimal

from api.config import SUBSCRIPTION_PROVIDERS
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
from api.observability.logging import log_event

OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"
OUTCOME_CANCELLED = "cancelled"
COST_UNKNOWN = "cost_unknown"
SUBSCRIPTION = "subscription"


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

    async def complete(self, request: LLMRequest) -> LLMResponse:
        context_tokens = count_messages(request.system, request.messages)
        started = time.perf_counter()
        try:
            response = await self.inner.complete(request)
        except asyncio.CancelledError:
            # The call was in flight: the provider may bill it, so the cost is unknown.
            self._log_cancelled(request, context_tokens, started, None)
            raise
        except Exception as exc:
            self._log_error(request, context_tokens, started, exc)
            raise
        self._log_success(request, context_tokens, started, response.usage)
        return response

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        context_tokens = count_messages(request.system, request.messages)
        started = time.perf_counter()
        usage: Usage | None = None
        logged = False
        inner = self.inner.stream(request)
        try:
            async for event in inner:
                if event.kind == "usage" and event.usage is not None:
                    usage = event.usage
                    if event.model is None or event.provider is None:
                        event = replace(event, model=self.model, provider=self.provider)
                yield event
            logged = True
            self._log_success(
                request, context_tokens, started, usage or Usage(input_tokens=0, output_tokens=0)
            )
        except Exception as exc:
            if not logged:
                logged = True
                self._log_error(request, context_tokens, started, exc)
            raise
        finally:
            # Reached without a log on early stop: `break`/`aclose()` (GeneratorExit) or task
            # cancellation (CancelledError). Close the inner stream so the provider call ends.
            if not logged:
                logged = True
                self._log_cancelled(request, context_tokens, started, usage)
                aclose = getattr(inner, "aclose", None)
                if aclose is not None:
                    await aclose()

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
        self, request: LLMRequest, context_tokens: int, started: float, usage: Usage
    ) -> None:
        cost, pricing_error = self._cost(usage)
        extra: dict[str, object] = {}
        if pricing_error is not None:
            extra["pricing_error"] = pricing_error
        self._emit(request, context_tokens, started, usage, cost, OUTCOME_OK, extra)

    def _log_cancelled(
        self, request: LLMRequest, context_tokens: int, started: float, usage: Usage | None
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
        self._emit(request, context_tokens, started, usage, cost, OUTCOME_CANCELLED, extra)

    def _log_error(
        self, request: LLMRequest, context_tokens: int, started: float, exc: Exception
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
        self._emit(request, context_tokens, started, usage, cost, OUTCOME_ERROR, extra)

    def _emit(
        self,
        request: LLMRequest,
        context_tokens: int,
        started: float,
        usage: Usage,
        cost: Decimal | None,
        outcome: str,
        extra: dict[str, object],
    ) -> None:
        # `log_event` adds the current trace id.
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
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
