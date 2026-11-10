import asyncio
import logging
from collections.abc import AsyncIterator, Iterator

import pytest

from api.llm.client import (
    ChatMessage,
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    LLMTimeoutError,
    SystemBlock,
    Usage,
)
from api.llm.fake import FakeLLMClient
from api.llm.retry import RetryingLLMClient
from api.observability.logging import LOGGER_NAME

REQUEST = LLMRequest(
    system=[SystemBlock(text="You are a support agent.")],
    messages=[ChatMessage(role="user", content="Where is my order?")],
)
RESPONSE = LLMResponse(
    text="It ships tomorrow.",
    tool_calls=[],
    usage=Usage(input_tokens=10, output_tokens=4),
    model="fake-model",
    provider="fake",
    stop_reason="end_turn",
)


def _rate_limited() -> LLMError:
    return LLMError("rate limited", recoverable=True, status=429)


class _Sleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def records() -> Iterator[list[logging.LogRecord]]:
    logger = logging.getLogger(LOGGER_NAME)
    handler = _ListHandler()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield handler.records
    logger.removeHandler(handler)
    logger.setLevel(previous_level)


def _retrying(
    inner: object, sleep: _Sleep, *, max_retries: int = 3, timeout_s: float = 5.0
) -> RetryingLLMClient:
    return RetryingLLMClient(
        inner,  # type: ignore[arg-type]
        max_retries=max_retries,
        base_delay_s=0.5,
        timeout_s=timeout_s,
        sleep=sleep,
        rng=lambda: 0.5,
    )


async def _collect(stream: AsyncIterator[LLMStreamEvent]) -> list[LLMStreamEvent]:
    return [event async for event in stream]


async def test_two_rate_limits_then_success_takes_three_attempts_with_growing_delays(
    records: list[logging.LogRecord],
) -> None:
    inner = FakeLLMClient([_rate_limited(), _rate_limited(), RESPONSE])
    sleep = _Sleep()

    response = await _retrying(inner, sleep).complete(REQUEST)

    assert response == RESPONSE
    assert len(inner.requests) == 3
    assert all(request is REQUEST for request in inner.requests)
    # base * 2**n * (0.5 + rng()) with rng() = 0.5 -> 0.5, 1.0
    assert sleep.delays == [0.5, 1.0]
    retries = [r for r in records if getattr(r, "event", None) == "llm_retry"]
    assert [getattr(r, "attempt", None) for r in retries] == [1, 2]
    assert [getattr(r, "status", None) for r in retries] == [429, 429]
    assert [getattr(r, "delay_s", None) for r in retries] == [0.5, 1.0]
    assert {getattr(r, "error_type", None) for r in retries} == {"LLMError"}


async def test_jitter_stays_within_half_and_one_and_a_half_of_the_base() -> None:
    values = iter([0.0, 0.999])
    inner = FakeLLMClient([_rate_limited(), _rate_limited(), RESPONSE])
    sleep = _Sleep()
    client = RetryingLLMClient(
        inner,
        max_retries=3,
        base_delay_s=1.0,
        timeout_s=5.0,
        sleep=sleep,
        rng=lambda: next(values),
    )

    await client.complete(REQUEST)

    assert sleep.delays == pytest.approx([0.5, 2 * 1.499])


async def test_non_recoverable_error_is_not_retried() -> None:
    bad_request = LLMError("bad request", recoverable=False, status=400)
    inner = FakeLLMClient([bad_request, RESPONSE])
    sleep = _Sleep()

    with pytest.raises(LLMError) as raised:
        await _retrying(inner, sleep).complete(REQUEST)

    assert raised.value is bad_request
    assert len(inner.requests) == 1
    assert sleep.delays == []


async def test_exhausted_retries_raise_the_last_error() -> None:
    last = LLMError("overloaded", recoverable=True, status=529)
    inner = FakeLLMClient([_rate_limited(), _rate_limited(), last, RESPONSE])
    sleep = _Sleep()

    with pytest.raises(LLMError) as raised:
        await _retrying(inner, sleep, max_retries=2).complete(REQUEST)

    assert raised.value is last
    assert len(inner.requests) == 3
    assert len(sleep.delays) == 2


class _SlowThenFast:
    """First call hangs past the timeout, the next ones answer at once."""

    def __init__(self) -> None:
        self.calls = 0
        self.cancelled = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        return RESPONSE

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[LLMStreamEvent]:
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(10)
        yield LLMStreamEvent(kind="text_delta", text="hi")
        yield LLMStreamEvent(kind="stop", stop_reason="end_turn")


async def test_timeout_is_treated_as_recoverable_and_retried(
    records: list[logging.LogRecord],
) -> None:
    inner = _SlowThenFast()
    sleep = _Sleep()

    response = await _retrying(inner, sleep, timeout_s=0.01).complete(REQUEST)

    assert response == RESPONSE
    assert inner.calls == 2
    assert inner.cancelled == 1
    retry = next(r for r in records if getattr(r, "event", None) == "llm_retry")
    assert getattr(retry, "error_type", None) == "LLMTimeoutError"


async def test_timeout_on_every_attempt_raises_llm_timeout_error() -> None:
    class _AlwaysSlow:
        async def complete(self, request: LLMRequest) -> LLMResponse:
            await asyncio.sleep(10)
            return RESPONSE

        def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
            raise NotImplementedError

    sleep = _Sleep()

    with pytest.raises(LLMTimeoutError) as raised:
        await _retrying(_AlwaysSlow(), sleep, max_retries=1, timeout_s=0.01).complete(REQUEST)

    assert raised.value.recoverable is True
    assert len(sleep.delays) == 1


async def test_external_cancellation_is_not_turned_into_a_retry() -> None:
    inner = _SlowThenFast()
    sleep = _Sleep()
    task = asyncio.create_task(_retrying(inner, sleep, timeout_s=5.0).complete(REQUEST))
    await asyncio.sleep(0.01)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert inner.calls == 1
    assert sleep.delays == []


async def test_stream_retries_before_the_first_event() -> None:
    inner = FakeLLMClient([_rate_limited(), RESPONSE])
    sleep = _Sleep()

    events = await _collect(_retrying(inner, sleep).stream(REQUEST))

    assert "".join(e.text for e in events if e.kind == "text_delta") == RESPONSE.text
    assert events[-1].kind == "stop"
    assert len(inner.requests) == 2
    assert len(sleep.delays) == 1


async def test_stream_timeout_before_the_first_event_is_retried() -> None:
    inner = _SlowThenFast()
    sleep = _Sleep()

    events = await _collect(_retrying(inner, sleep, timeout_s=0.01).stream(REQUEST))

    assert [e.kind for e in events] == ["text_delta", "stop"]
    assert inner.calls == 2


class _BreaksAfterFirstEvent:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[LLMStreamEvent]:
        self.calls += 1
        yield LLMStreamEvent(kind="text_delta", text="Hel")
        raise LLMError("connection reset", recoverable=True, status=503)


async def test_stream_error_after_the_first_event_propagates_without_retry() -> None:
    inner = _BreaksAfterFirstEvent()
    sleep = _Sleep()
    seen: list[LLMStreamEvent] = []

    with pytest.raises(LLMError, match="connection reset"):
        async for event in _retrying(inner, sleep).stream(REQUEST):
            seen.append(event)

    assert [e.text for e in seen] == ["Hel"]
    assert inner.calls == 1
    assert sleep.delays == []


async def test_stream_non_recoverable_error_is_not_retried() -> None:
    inner = FakeLLMClient([LLMError("unauthorized", recoverable=False, status=401), RESPONSE])
    sleep = _Sleep()

    with pytest.raises(LLMError, match="unauthorized"):
        await _collect(_retrying(inner, sleep).stream(REQUEST))

    assert len(inner.requests) == 1
