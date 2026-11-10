"""Provider fallback chain (CT-59, RES-05, RES-91..93, LAC-29).

`FallbackLLMClient(chain)`: `chain` = `[(name, client, breaker), ...]` in priority order. Each
call tries the clients in order, skipping those whose breaker does not `allow()`; the first
success wins. A recoverable failure moves to the next client (log `llm_fallback` with `from` and
`to`); a non-recoverable error propagates. All failed or all open -> `AllProvidersFailed` (no
call at all when every breaker is open, RES-92). `stream` switches provider only before the
first `text_delta`; after it the error propagates (RES-93). The chain only wraps model calls:
it never repeats tool calls (RES-91).
"""

from collections.abc import AsyncIterator

from api.llm.circuit_breaker import CircuitBreaker
from api.llm.client import LLMClient, LLMRequest, LLMResponse, LLMStreamEvent


class AllProvidersFailed(Exception):
    """Every provider of the chain failed or had its circuit open."""


class FallbackLLMClient:
    def __init__(self, chain: list[tuple[str, LLMClient, CircuitBreaker]]) -> None:
        self.chain = chain

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError("TASK-118")

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        raise NotImplementedError("TASK-118")
