"""Per-provider circuit breaker (CT-57, RES-04, DA-24: state per process, never in the DB).

`CircuitBreaker`: `closed` -> `open` after `failure_threshold` consecutive failures; `open` ->
`half_open` once `reset_seconds` have passed (`clock` injectable); in `half_open` one trial call
is allowed: success -> `closed`, failure -> `open` again. `BreakerLLMClient(inner, breaker)`:
when `allow()` is false raises `CircuitOpen` (recoverable) without calling `inner`; records
success/failure of each call (only recoverable `LLMError`s count as failures).
"""

import time
from collections.abc import AsyncIterator, Callable
from typing import Literal

from api.llm.client import LLMClient, LLMError, LLMRequest, LLMResponse, LLMStreamEvent

BreakerState = Literal["closed", "open", "half_open"]


class CircuitOpen(LLMError):
    def __init__(self, name: str) -> None:
        super().__init__(f"Circuit open for provider {name}", recoverable=True)
        self.name = name


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int,
        reset_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self.clock = clock

    @property
    def state(self) -> BreakerState:
        raise NotImplementedError("TASK-116")

    def allow(self) -> bool:
        raise NotImplementedError("TASK-116")

    def record_success(self) -> None:
        raise NotImplementedError("TASK-116")

    def record_failure(self) -> None:
        raise NotImplementedError("TASK-116")


class BreakerLLMClient:
    def __init__(self, inner: LLMClient, breaker: CircuitBreaker) -> None:
        self.inner = inner
        self.breaker = breaker

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError("TASK-116")

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        raise NotImplementedError("TASK-116")
