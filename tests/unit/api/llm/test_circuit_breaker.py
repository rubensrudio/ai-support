import logging
from collections.abc import AsyncIterator, Iterator

import pytest

from api.llm.circuit_breaker import BreakerLLMClient, CircuitBreaker, CircuitOpen
from api.llm.client import (
    ChatMessage,
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    SystemBlock,
    Usage,
)
from api.llm.fake import FakeLLMClient
from api.observability.logging import LOGGER_NAME

REQUEST = LLMRequest(
    system=[SystemBlock(text="You are a support agent.")],
    messages=[ChatMessage(role="user", content="Hi")],
)
RESPONSE = LLMResponse(
    text="Hello there.",
    tool_calls=[],
    usage=Usage(input_tokens=3, output_tokens=2),
    model="fake-model",
    provider="fake",
    stop_reason="end_turn",
)


def _unavailable() -> LLMError:
    return LLMError("unavailable", recoverable=True, status=503)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


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


def _state(breaker: CircuitBreaker) -> str:
    """Read the state without letting mypy narrow it across clock moves."""
    return breaker.state


def _breaker(clock: _Clock, threshold: int = 3) -> CircuitBreaker:
    return CircuitBreaker("anthropic", failure_threshold=threshold, reset_seconds=30, clock=clock)


async def test_opens_after_threshold_failures_and_never_calls_inner_while_open(
    records: list[logging.LogRecord],
) -> None:
    clock = _Clock()
    breaker = _breaker(clock)
    inner = FakeLLMClient([_unavailable(), _unavailable(), _unavailable(), RESPONSE])
    client = BreakerLLMClient(inner, breaker)

    for _ in range(3):
        with pytest.raises(LLMError, match="unavailable"):
            await client.complete(REQUEST)

    assert _state(breaker) == "open"
    assert breaker.allow() is False
    for _ in range(5):
        with pytest.raises(CircuitOpen) as raised:
            await client.complete(REQUEST)
        assert raised.value.recoverable is True
    assert len(inner.requests) == 3
    transitions = [r for r in records if getattr(r, "event", None) == "llm_breaker"]
    assert [(getattr(r, "provider", None), getattr(r, "state", None)) for r in transitions] == [
        ("anthropic", "open")
    ]


async def test_success_resets_the_consecutive_failure_count() -> None:
    breaker = _breaker(_Clock())
    inner = FakeLLMClient([_unavailable(), _unavailable(), RESPONSE, _unavailable(), RESPONSE])
    client = BreakerLLMClient(inner, breaker)

    for _ in range(2):
        with pytest.raises(LLMError):
            await client.complete(REQUEST)
    await client.complete(REQUEST)
    with pytest.raises(LLMError):
        await client.complete(REQUEST)

    assert _state(breaker) == "closed"
    assert await client.complete(REQUEST) == RESPONSE


async def test_non_recoverable_errors_do_not_count_as_failures() -> None:
    breaker = _breaker(_Clock(), threshold=1)
    inner = FakeLLMClient([LLMError("bad request", recoverable=False, status=400)])

    with pytest.raises(LLMError, match="bad request"):
        await BreakerLLMClient(inner, breaker).complete(REQUEST)

    assert _state(breaker) == "closed"


async def test_half_open_after_reset_and_successful_trial_closes(
    records: list[logging.LogRecord],
) -> None:
    clock = _Clock()
    breaker = _breaker(clock, threshold=1)
    inner = FakeLLMClient([_unavailable(), RESPONSE, RESPONSE])
    client = BreakerLLMClient(inner, breaker)
    with pytest.raises(LLMError):
        await client.complete(REQUEST)

    clock.now += 29.9
    assert _state(breaker) == "open"
    clock.now += 0.1
    assert _state(breaker) == "half_open"
    assert breaker.allow() is True

    assert await client.complete(REQUEST) == RESPONSE
    assert _state(breaker) == "closed"
    assert await client.complete(REQUEST) == RESPONSE
    states = [
        getattr(r, "state", None) for r in records if getattr(r, "event", None) == "llm_breaker"
    ]
    assert states == ["open", "half_open", "closed"]


async def test_half_open_failed_trial_reopens_and_restarts_the_period() -> None:
    clock = _Clock()
    breaker = _breaker(clock, threshold=1)
    inner = FakeLLMClient([_unavailable(), _unavailable(), RESPONSE])
    client = BreakerLLMClient(inner, breaker)
    with pytest.raises(LLMError):
        await client.complete(REQUEST)
    clock.now += 30

    with pytest.raises(LLMError, match="unavailable"):
        await client.complete(REQUEST)

    assert _state(breaker) == "open"
    clock.now += 29
    with pytest.raises(CircuitOpen):
        await client.complete(REQUEST)
    assert len(inner.requests) == 2
    clock.now += 1
    assert _state(breaker) == "half_open"


def test_half_open_lets_one_trial_through_at_a_time() -> None:
    clock = _Clock()
    breaker = _breaker(clock, threshold=1)
    breaker.record_failure()
    clock.now += 30

    assert breaker.acquire() is True
    assert breaker.allow() is False
    assert breaker.acquire() is False

    breaker.release()
    assert breaker.allow() is True


def test_allow_does_not_consume_the_half_open_trial() -> None:
    clock = _Clock()
    breaker = _breaker(clock, threshold=1)
    breaker.record_failure()
    clock.now += 30

    assert breaker.allow() is True
    assert breaker.allow() is True
    assert breaker.acquire() is True


class _ScriptedStream:
    """Streams `events`, then raises `error` if given."""

    def __init__(self, events: list[LLMStreamEvent], error: LLMError | None = None) -> None:
        self.events = events
        self.error = error
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[LLMStreamEvent]:
        self.calls += 1
        for event in self.events:
            yield event
        if self.error is not None:
            raise self.error


async def test_stream_failure_before_the_first_event_counts_as_failure() -> None:
    breaker = _breaker(_Clock(), threshold=1)
    inner = _ScriptedStream([], _unavailable())

    with pytest.raises(LLMError, match="unavailable"):
        async for _ in BreakerLLMClient(inner, breaker).stream(REQUEST):
            pass

    assert _state(breaker) == "open"


async def test_stream_failure_after_events_counts_as_failure() -> None:
    breaker = _breaker(_Clock(), threshold=1)
    inner = _ScriptedStream([LLMStreamEvent(kind="text_delta", text="Hi")], _unavailable())

    with pytest.raises(LLMError):
        async for _ in BreakerLLMClient(inner, breaker).stream(REQUEST):
            pass

    assert _state(breaker) == "open"


async def test_completed_stream_closes_a_half_open_breaker() -> None:
    clock = _Clock()
    breaker = _breaker(clock, threshold=1)
    breaker.record_failure()
    clock.now += 30
    inner = _ScriptedStream(
        [LLMStreamEvent(kind="text_delta", text="Hi"), LLMStreamEvent(kind="stop")]
    )

    events = [e async for e in BreakerLLMClient(inner, breaker).stream(REQUEST)]

    assert len(events) == 2
    assert _state(breaker) == "closed"


async def test_open_stream_raises_circuit_open_without_calling_inner() -> None:
    breaker = _breaker(_Clock(), threshold=1)
    breaker.record_failure()
    inner = _ScriptedStream([LLMStreamEvent(kind="stop")])

    with pytest.raises(CircuitOpen):
        async for _ in BreakerLLMClient(inner, breaker).stream(REQUEST):
            pass

    assert inner.calls == 0


async def test_unsupported_stream_releases_the_half_open_trial() -> None:
    clock = _Clock()
    breaker = _breaker(clock, threshold=1)
    breaker.record_failure()
    clock.now += 30

    class _NoStream:
        async def complete(self, request: LLMRequest) -> LLMResponse:
            return RESPONSE

        def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
            raise NotImplementedError

    client = BreakerLLMClient(_NoStream(), breaker)
    with pytest.raises(NotImplementedError):
        async for _ in client.stream(REQUEST):
            pass

    assert _state(breaker) == "half_open"
    assert await client.complete(REQUEST) == RESPONSE
    assert _state(breaker) == "closed"
