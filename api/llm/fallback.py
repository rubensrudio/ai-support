"""Provider fallback chain (CT-59, RES-05, RES-91..93, LAC-29).

`FallbackLLMClient(chain)`: `chain` = `[(name, client, breaker), ...]` in priority order. Each
call tries the clients in order, skipping those whose breaker does not `allow()`; the first
success wins. A recoverable failure moves to the next client (log `llm_fallback` with `from` and
`to`); a non-recoverable error propagates. All failed or all open -> `AllProvidersFailed` (no
call at all when every breaker is open, RES-92). `stream` switches provider only before the
first `text_delta`; after it the error propagates (RES-93). The chain only wraps model calls:
it never repeats tool calls (RES-91).

The chain only reads its breakers (`allow()`); recording outcomes is the job of the
`BreakerLLMClient` each `client` is expected to be (factory, TASK-121). Every request goes to
the next provider unchanged. `llm_fallback` also carries `error_type`; a provider skipped
because its circuit is open is reported with `error_type="CircuitOpen"`.

Stream boundary: the chain switches provider only while the failed stream has emitted NO event
at all. That is stricter than "before the first `text_delta`": a `tool_call` or `usage` event
already reached the consumer, so replaying the turn on another provider could duplicate a tool
call (RES-91) or mix two providers' answers. Once any event went out, errors propagate as is.
`NotImplementedError` (a provider that cannot stream) also propagates, so
`stream_or_complete` can fall back to `complete` on the whole chain.
"""

from collections.abc import AsyncIterator

from api.llm.circuit_breaker import CircuitBreaker
from api.llm.client import LLMClient, LLMError, LLMRequest, LLMResponse, LLMStreamEvent
from api.observability.logging import log_event

CIRCUIT_OPEN = "CircuitOpen"


class AllProvidersFailed(Exception):
    """Every provider of the chain failed or had its circuit open."""


class FallbackLLMClient:
    def __init__(self, chain: list[tuple[str, LLMClient, CircuitBreaker]]) -> None:
        self.chain = chain

    async def complete(self, request: LLMRequest) -> LLMResponse:
        previous: tuple[str, str] | None = None
        last_error: LLMError | None = None
        for name, client, breaker in self.chain:
            if not breaker.allow():
                previous = (name, CIRCUIT_OPEN)
                continue
            _log_fallback(previous, name)
            try:
                return await client.complete(request)
            except LLMError as exc:
                if not exc.recoverable:
                    raise
                last_error = exc
                previous = (name, type(exc).__name__)
        raise _all_failed(last_error)

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        previous: tuple[str, str] | None = None
        last_error: LLMError | None = None
        for name, client, breaker in self.chain:
            if not breaker.allow():
                previous = (name, CIRCUIT_OPEN)
                continue
            _log_fallback(previous, name)
            emitted = False
            source: AsyncIterator[LLMStreamEvent] | None = None
            try:
                source = client.stream(request)
                async for event in source:
                    emitted = True
                    yield event
                return
            except LLMError as exc:
                if emitted or not exc.recoverable:
                    raise
                last_error = exc
                previous = (name, type(exc).__name__)
            finally:
                aclose = getattr(source, "aclose", None)
                if callable(aclose):
                    await aclose()
        raise _all_failed(last_error)


def _log_fallback(previous: tuple[str, str] | None, to: str) -> None:
    if previous is None:
        return
    source, error_type = previous
    log_event("llm_fallback", **{"from": source, "to": to, "error_type": error_type})


def _all_failed(last_error: LLMError | None) -> AllProvidersFailed:
    if last_error is None:
        error = AllProvidersFailed("Every LLM provider circuit is open")
    else:
        error = AllProvidersFailed(f"Every LLM provider failed; last error: {last_error.message}")
        error.__cause__ = last_error
    return error
