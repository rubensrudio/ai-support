"""Chaos failure injection for the primary provider (CT-58, RES-07, DEMO-05).

`ChaosLLMClient(inner, rate, rng)`: before each call, with probability `rate` raises
`LLMError(recoverable=True, status=503)` without calling `inner`. `rate` in [0, 1] (`CHAOS_RATE`,
validated by `Settings`); 0 never fails. Only the primary is wrapped.
"""

import random
from collections.abc import AsyncIterator

from api.llm.client import LLMClient, LLMRequest, LLMResponse, LLMStreamEvent


class ChaosLLMClient:
    def __init__(self, inner: LLMClient, rate: float, rng: random.Random | None = None) -> None:
        self.inner = inner
        self.rate = rate
        self.rng = rng or random.Random()  # noqa: S311 - fault injection, not security

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError("TASK-117")

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        raise NotImplementedError("TASK-117")
