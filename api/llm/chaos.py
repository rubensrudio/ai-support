"""Chaos failure injection for the primary provider (CT-58, RES-07, DEMO-05).

`ChaosLLMClient(inner, rate, rng)`: before each call, with probability `rate` raises
`LLMError(recoverable=True, status=503)` without calling `inner`. `rate` in [0, 1] (`CHAOS_RATE`,
validated by `Settings`); 0 never fails. Only the primary is wrapped.

In `stream` the inner stream is opened first and the draw happens when the stream is first
iterated, so an injected failure looks like a provider that failed before its first event (the
opened inner stream is closed). A provider whose `stream()` raises `NotImplementedError` (the
subscription CLI adapters) propagates it without a draw, so `stream_or_complete` falling back to
`complete` draws only once per call.
"""

import random
from collections.abc import AsyncIterator

from api.llm.client import LLMClient, LLMError, LLMRequest, LLMResponse, LLMStreamEvent

CHAOS_STATUS = 503


class ChaosLLMClient:
    def __init__(self, inner: LLMClient, rate: float, rng: random.Random | None = None) -> None:
        self.inner = inner
        self.rate = rate
        self.rng = rng or random.Random()  # noqa: S311 - fault injection, not security

    def _maybe_fail(self) -> None:
        # `random()` is in [0, 1): rate 0 never fails and rate 1 always does.
        if self.rng.random() < self.rate:
            raise LLMError("chaos", recoverable=True, status=CHAOS_STATUS)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self._maybe_fail()
        return await self.inner.complete(request)

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        # Open the inner stream before drawing: a provider that cannot stream raises
        # `NotImplementedError` here with no draw, so `stream_or_complete` falling back to
        # `complete` draws exactly once per call (RES-07).
        source = self.inner.stream(request)
        try:
            self._maybe_fail()
            async for event in source:
                yield event
        finally:
            aclose = getattr(source, "aclose", None)
            if callable(aclose):
                await aclose()
