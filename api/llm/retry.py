"""Retry with exponential backoff, jitter and a per-attempt timeout (CT-56, RES-01..03).

Each attempt runs under `asyncio.timeout(timeout_s)`; a timeout becomes `LLMTimeoutError`
(recoverable). Only `LLMError.recoverable` errors are retried, up to `max_retries` retries
(total attempts = 1 + max_retries), sleeping `base_delay_s * 2**n * uniform(0.5, 1.5)` before
retry n (n = 0, 1, ...), with `sleep` and `rng` injectable. `stream` retries only before its
first event; after it, errors propagate. Implements `LLMClient` (CT-6).

Every retry logs `llm_retry` (attempt that failed, delay, status, error type). When the retries
run out the last error is raised as is. A cancellation of the caller (`CancelledError`) is never
turned into an LLM error nor retried. In `stream` the timeout covers the wait for the first
event only: once the provider has started answering, the consumer's own pace drives the stream.
"""

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable

from api.llm.client import (
    LLMClient,
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    LLMTimeoutError,
)
from api.observability.logging import log_event


class RetryingLLMClient:
    def __init__(
        self,
        inner: LLMClient,
        *,
        max_retries: int,
        base_delay_s: float,
        timeout_s: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.inner = inner
        self.max_retries = max_retries
        self.base_delay_s = base_delay_s
        self.timeout_s = timeout_s
        self.sleep = sleep
        self.rng = rng

    async def complete(self, request: LLMRequest) -> LLMResponse:
        retry = 0
        while True:
            try:
                async with asyncio.timeout(self.timeout_s):
                    return await self.inner.complete(request)
            except TimeoutError as exc:
                error: LLMError = LLMTimeoutError(f"LLM call timed out after {self.timeout_s}s")
                error.__cause__ = exc
            except LLMError as exc:
                error = exc
            if not await self._backoff(error, retry):
                raise error
            retry += 1

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        retry = 0
        while True:
            source: AsyncIterator[LLMStreamEvent] | None = None
            try:
                source = self.inner.stream(request)
                async with asyncio.timeout(self.timeout_s):
                    first = await anext(source)
            except StopAsyncIteration:
                return
            except TimeoutError as exc:
                await _close(source)
                error: LLMError = LLMTimeoutError(
                    f"LLM stream produced no event within {self.timeout_s}s"
                )
                error.__cause__ = exc
            except LLMError as exc:
                await _close(source)
                error = exc
            except BaseException:
                await _close(source)
                raise
            else:
                break
            if not await self._backoff(error, retry):
                raise error
            retry += 1
        assert source is not None  # noqa: S101 - set before the first event was read
        try:
            yield first
            async for event in source:
                yield event
        finally:
            await _close(source)

    async def _backoff(self, error: LLMError, retry: int) -> bool:
        """Sleep before retry number `retry` and return True, or False when out of retries."""
        if not error.recoverable or retry >= self.max_retries:
            return False
        delay = self.base_delay_s * 2**retry * (0.5 + self.rng())
        log_event(
            "llm_retry",
            attempt=retry + 1,
            delay_s=delay,
            status=error.status,
            error_type=type(error).__name__,
        )
        await self.sleep(delay)
        return True


async def _close(source: AsyncIterator[LLMStreamEvent] | None) -> None:
    """Close a provider stream so its call ends now instead of at garbage collection."""
    if source is None:
        return
    aclose = getattr(source, "aclose", None)
    if callable(aclose):
        await aclose()
