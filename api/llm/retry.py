"""Retry with exponential backoff, jitter and a per-attempt timeout (CT-56, RES-01..03).

Each attempt runs under `asyncio.timeout(timeout_s)`; a timeout becomes `LLMTimeoutError`
(recoverable). Only `LLMError.recoverable` errors are retried, up to `max_retries` retries
(total attempts = 1 + max_retries), sleeping `base_delay_s * 2**n * uniform(0.5, 1.5)` before
retry n (n = 0, 1, ...), with `sleep` and `rng` injectable. `stream` retries only before its
first event; after it, errors propagate. Implements `LLMClient` (CT-6).
"""

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable

from api.llm.client import LLMClient, LLMRequest, LLMResponse, LLMStreamEvent


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
        raise NotImplementedError("TASK-115")

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        raise NotImplementedError("TASK-115")
