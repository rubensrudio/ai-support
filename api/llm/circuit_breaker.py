"""Per-provider circuit breaker (CT-57, RES-04, DA-24: state per process, never in the DB).

`CircuitBreaker`: `closed` -> `open` after `failure_threshold` consecutive failures; `open` ->
`half_open` once `reset_seconds` have passed (`clock` injectable); in `half_open` one trial call
is allowed: success -> `closed`, failure -> `open` again. `BreakerLLMClient(inner, breaker)`:
when `allow()` is false raises `CircuitOpen` (recoverable) without calling `inner`; records
success/failure of each call (only recoverable `LLMError`s count as failures).

`allow()` only asks; it never takes the half-open trial, so a caller such as the fallback chain
can check a breaker and then call the `BreakerLLMClient` that guards it. `acquire()` asks and,
in `half_open`, takes the single trial slot; `record_success()`, `record_failure()` and
`release()` give it back. `BreakerLLMClient` acquires before each call and releases on a
neutral outcome (non-recoverable error, any other exception, cancellation): those say nothing
about the provider's health. A stream counts as a success when it completes or when the
consumer stops it after at least one event; a recoverable error at any point is a failure.
Every state transition logs `llm_breaker` with the breaker's name as `provider` (`name` is a
reserved `LogRecord` attribute) and the new `state`.
"""

import time
from collections.abc import AsyncIterator, Callable
from typing import Literal

from api.llm.client import LLMClient, LLMError, LLMRequest, LLMResponse, LLMStreamEvent
from api.observability.logging import log_event

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
        self._state: BreakerState = "closed"
        self._failures = 0
        self._opened_at = 0.0
        self._trial_in_flight = False

    @property
    def state(self) -> BreakerState:
        if self._state == "open" and self.clock() - self._opened_at >= self.reset_seconds:
            self._transition("half_open")
        return self._state

    def allow(self) -> bool:
        state = self.state
        if state == "closed":
            return True
        if state == "half_open":
            return not self._trial_in_flight
        return False

    def acquire(self) -> bool:
        """`allow()` that also takes the half-open trial slot when it lets the call through."""
        if not self.allow():
            return False
        if self._state == "half_open":
            self._trial_in_flight = True
        return True

    def release(self) -> None:
        """Give the trial slot back after a call whose outcome says nothing about health."""
        self._trial_in_flight = False

    def record_success(self) -> None:
        self._trial_in_flight = False
        state = self.state
        if state == "open":
            # A call started before the circuit opened: the open period still runs.
            return
        self._failures = 0
        if state == "half_open":
            self._transition("closed")

    def record_failure(self) -> None:
        self._trial_in_flight = False
        state = self.state
        if state == "open":
            return
        if state == "half_open":
            self._open()
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._open()

    def _open(self) -> None:
        self._opened_at = self.clock()
        self._failures = 0
        self._transition("open")

    def _transition(self, state: BreakerState) -> None:
        self._state = state
        log_event("llm_breaker", provider=self.name, state=state)


class BreakerLLMClient:
    def __init__(self, inner: LLMClient, breaker: CircuitBreaker) -> None:
        self.inner = inner
        self.breaker = breaker

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if not self.breaker.acquire():
            raise CircuitOpen(self.breaker.name)
        settled = False
        try:
            response = await self.inner.complete(request)
        except LLMError as exc:
            if exc.recoverable:
                settled = True
                self.breaker.record_failure()
            raise
        else:
            settled = True
            self.breaker.record_success()
            return response
        finally:
            if not settled:
                self.breaker.release()

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        if not self.breaker.acquire():
            raise CircuitOpen(self.breaker.name)
        settled = False
        received = False
        source: AsyncIterator[LLMStreamEvent] | None = None
        try:
            source = self.inner.stream(request)
            async for event in source:
                received = True
                yield event
            settled = True
            self.breaker.record_success()
        except LLMError as exc:
            if exc.recoverable and not settled:
                settled = True
                self.breaker.record_failure()
            raise
        finally:
            if not settled:
                # Consumer stopped early, cancellation or a neutral error.
                if received:
                    self.breaker.record_success()
                else:
                    self.breaker.release()
            aclose = getattr(source, "aclose", None)
            if callable(aclose):
                await aclose()
