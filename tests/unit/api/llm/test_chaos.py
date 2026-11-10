import random
from collections.abc import AsyncIterator

import pytest

from api.llm.chaos import ChaosLLMClient
from api.llm.client import (
    ChatMessage,
    LLMClient,
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    SystemBlock,
    Usage,
)
from api.llm.streaming import stream_or_complete

REQUEST = LLMRequest(
    system=[SystemBlock(text="You are a support agent.")],
    messages=[ChatMessage(role="user", content="Hi")],
)
RESPONSE = LLMResponse(
    text="Hello.",
    tool_calls=[],
    usage=Usage(input_tokens=3, output_tokens=1),
    model="fake-model",
    provider="fake",
    stop_reason="end_turn",
)


class _Source:
    """Inner stream that records whether it was iterated and closed."""

    def __init__(self) -> None:
        self.events = [
            LLMStreamEvent(kind="text_delta", text="Hello."),
            LLMStreamEvent(kind="stop", stop_reason="end_turn"),
        ]
        self.started = False
        self.closed = False

    def __aiter__(self) -> "_Source":
        return self

    async def __anext__(self) -> LLMStreamEvent:
        self.started = True
        if not self.events:
            raise StopAsyncIteration
        return self.events.pop(0)

    async def aclose(self) -> None:
        self.closed = True


class _Counting:
    def __init__(self) -> None:
        self.complete_calls = 0
        self.sources: list[_Source] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.complete_calls += 1
        return RESPONSE

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        source = _Source()
        self.sources.append(source)
        return source


class _NoStream:
    """A subscription-CLI-like provider: `stream()` raises at once."""

    def __init__(self) -> None:
        self.complete_calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.complete_calls += 1
        return RESPONSE

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        raise NotImplementedError("streaming not supported")


class _CountingRandom(random.Random):
    def __init__(self, seed: int) -> None:
        super().__init__(seed)
        self.draws = 0

    def random(self) -> float:
        self.draws += 1
        return super().random()


async def _failures(client: ChaosLLMClient, calls: int) -> int:
    failures = 0
    for _ in range(calls):
        try:
            await client.complete(REQUEST)
        except LLMError as exc:
            assert exc.recoverable is True
            assert exc.status == 503
            failures += 1
    return failures


async def test_rate_point_three_fails_about_thirty_percent_of_calls() -> None:
    inner = _Counting()
    client = ChaosLLMClient(inner, rate=0.3, rng=random.Random(12))  # noqa: S311

    failures = await _failures(client, 1000)

    assert 250 <= failures <= 350
    assert inner.complete_calls == 1000 - failures


async def test_rate_zero_never_fails() -> None:
    inner = _Counting()
    client = ChaosLLMClient(inner, rate=0.0, rng=random.Random(12))  # noqa: S311

    assert await _failures(client, 1000) == 0
    assert inner.complete_calls == 1000


async def test_rate_one_always_fails_without_calling_inner() -> None:
    inner = _Counting()
    client = ChaosLLMClient(inner, rate=1.0)

    assert await _failures(client, 50) == 50
    assert inner.complete_calls == 0


async def test_stream_injects_failure_before_iterating_inner_and_closes_it() -> None:
    inner = _Counting()
    client = ChaosLLMClient(inner, rate=1.0)

    with pytest.raises(LLMError, match="chaos") as raised:
        async for _ in client.stream(REQUEST):
            pass

    assert raised.value.recoverable is True
    assert raised.value.status == 503
    assert len(inner.sources) == 1
    assert inner.sources[0].started is False
    assert inner.sources[0].closed is True


async def test_stream_passes_events_through_when_no_failure_is_drawn() -> None:
    inner = _Counting()
    client = ChaosLLMClient(inner, rate=0.0)

    events = [e async for e in client.stream(REQUEST)]

    assert [e.kind for e in events] == ["text_delta", "stop"]
    assert len(inner.sources) == 1
    assert inner.sources[0].closed is True


async def test_provider_without_stream_draws_once_per_call_via_stream_or_complete() -> None:
    inner = _NoStream()
    rng = _CountingRandom(12)
    client = ChaosLLMClient(inner, rate=0.0, rng=rng)

    events = [e async for e in stream_or_complete(client, REQUEST)]

    assert events[0].text == RESPONSE.text
    assert rng.draws == 1
    assert inner.complete_calls == 1


async def _stream_failures(client: LLMClient, calls: int) -> int:
    failures = 0
    for _ in range(calls):
        try:
            async for _event in stream_or_complete(client, REQUEST):
                pass
        except LLMError:
            failures += 1
    return failures


async def test_provider_without_stream_keeps_the_configured_rate_via_stream_or_complete() -> None:
    inner = _NoStream()
    rng = _CountingRandom(12)
    client = ChaosLLMClient(inner, rate=0.3, rng=rng)

    failures = await _stream_failures(client, 1000)

    assert 250 <= failures <= 350
    assert rng.draws == 1000
