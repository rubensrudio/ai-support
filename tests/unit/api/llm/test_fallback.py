import logging
from collections.abc import AsyncIterator, Iterator

import pytest

from api.llm.circuit_breaker import BreakerLLMClient, CircuitBreaker
from api.llm.client import (
    ChatMessage,
    LLMClient,
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    SystemBlock,
    ToolCall,
    Usage,
)
from api.llm.fake import FakeLLMClient
from api.llm.fallback import AllProvidersFailed, FallbackLLMClient
from api.observability.logging import LOGGER_NAME

REQUEST = LLMRequest(
    system=[SystemBlock(text="You are a support agent.")],
    messages=[ChatMessage(role="user", content="Cancel order 42")],
)


def _response(provider: str) -> LLMResponse:
    return LLMResponse(
        text=f"Answer from {provider}.",
        tool_calls=[],
        usage=Usage(input_tokens=5, output_tokens=3),
        model=f"{provider}-model",
        provider=provider,
        stop_reason="end_turn",
    )


def _unavailable() -> LLMError:
    return LLMError("unavailable", recoverable=True, status=503)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

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


def _breaker(name: str, clock: _Clock | None = None, threshold: int = 3) -> CircuitBreaker:
    return CircuitBreaker(
        name, failure_threshold=threshold, reset_seconds=30, clock=clock or _Clock()
    )


def _chain(
    primary: LLMClient, secondary: LLMClient, *, threshold: int = 3
) -> tuple[FallbackLLMClient, CircuitBreaker, CircuitBreaker]:
    first = _breaker("anthropic", threshold=threshold)
    second = _breaker("openai", threshold=threshold)
    chain: list[tuple[str, LLMClient, CircuitBreaker]] = [
        ("anthropic", BreakerLLMClient(primary, first), first),
        ("openai", BreakerLLMClient(secondary, second), second),
    ]
    return FallbackLLMClient(chain), first, second


async def test_primary_success_does_not_touch_the_secondary() -> None:
    primary = FakeLLMClient([_response("anthropic")])
    secondary = FakeLLMClient([_response("openai")])
    client, _, _ = _chain(primary, secondary)

    response = await client.complete(REQUEST)

    assert response.provider == "anthropic"
    assert secondary.requests == []


async def test_recoverable_primary_failure_is_served_by_secondary_with_the_same_request(
    records: list[logging.LogRecord],
) -> None:
    primary = FakeLLMClient([_unavailable()])
    secondary = FakeLLMClient([_response("openai")])
    client, _, _ = _chain(primary, secondary)

    response = await client.complete(REQUEST)

    assert response.provider == "openai"
    assert primary.requests == [REQUEST]
    assert len(secondary.requests) == 1
    assert secondary.requests[0] is REQUEST
    fallbacks = [r for r in records if getattr(r, "event", None) == "llm_fallback"]
    assert len(fallbacks) == 1
    assert getattr(fallbacks[0], "from") == "anthropic"
    assert getattr(fallbacks[0], "to", None) == "openai"
    assert getattr(fallbacks[0], "error_type", None) == "LLMError"


async def test_non_recoverable_primary_error_propagates_without_fallback() -> None:
    primary = FakeLLMClient([LLMError("bad request", recoverable=False, status=400)])
    secondary = FakeLLMClient([_response("openai")])
    client, _, _ = _chain(primary, secondary)

    with pytest.raises(LLMError, match="bad request"):
        await client.complete(REQUEST)

    assert secondary.requests == []


async def test_all_providers_failing_raises_all_providers_failed() -> None:
    primary = FakeLLMClient([_unavailable()])
    secondary = FakeLLMClient([LLMError("overloaded", recoverable=True, status=529)])
    client, _, _ = _chain(primary, secondary)

    with pytest.raises(AllProvidersFailed) as raised:
        await client.complete(REQUEST)

    assert isinstance(raised.value.__cause__, LLMError)
    assert raised.value.__cause__.status == 529
    assert len(primary.requests) == 1
    assert len(secondary.requests) == 1


async def test_open_primary_is_skipped_without_being_called(
    records: list[logging.LogRecord],
) -> None:
    primary = FakeLLMClient([_response("anthropic")])
    secondary = FakeLLMClient([_response("openai")])
    client, first, _ = _chain(primary, secondary, threshold=1)
    first.record_failure()

    response = await client.complete(REQUEST)

    assert response.provider == "openai"
    assert primary.requests == []
    fallback = next(r for r in records if getattr(r, "event", None) == "llm_fallback")
    assert getattr(fallback, "error_type", None) == "CircuitOpen"


async def test_all_circuits_open_raises_without_any_call() -> None:
    primary = FakeLLMClient([_response("anthropic")])
    secondary = FakeLLMClient([_response("openai")])
    client, first, second = _chain(primary, secondary, threshold=1)
    first.record_failure()
    second.record_failure()

    with pytest.raises(AllProvidersFailed):
        await client.complete(REQUEST)
    with pytest.raises(AllProvidersFailed):
        async for _ in client.stream(REQUEST):
            pass

    assert primary.requests == []
    assert secondary.requests == []


async def test_half_open_primary_gets_its_trial_call_through_the_chain() -> None:
    clock = _Clock()
    first = _breaker("anthropic", clock, threshold=1)
    second = _breaker("openai", clock, threshold=1)
    primary = FakeLLMClient([_response("anthropic")])
    secondary = FakeLLMClient([_response("openai")])
    client = FallbackLLMClient(
        [
            ("anthropic", BreakerLLMClient(primary, first), first),
            ("openai", BreakerLLMClient(secondary, second), second),
        ]
    )
    first.record_failure()
    clock.now += 30

    response = await client.complete(REQUEST)

    assert response.provider == "anthropic"
    assert first.state == "closed"
    assert secondary.requests == []


async def test_repeated_primary_failures_open_its_circuit_and_route_to_secondary() -> None:
    primary = FakeLLMClient([_unavailable(), _unavailable()])
    secondary = FakeLLMClient([_response("openai") for _ in range(4)])
    client, first, _ = _chain(primary, secondary, threshold=2)

    for _ in range(4):
        assert (await client.complete(REQUEST)).provider == "openai"

    assert first.state == "open"
    assert len(primary.requests) == 2
    assert len(secondary.requests) == 4


class _ScriptedStream:
    """Streams `events`, then raises `error` if given; counts calls."""

    def __init__(self, events: list[LLMStreamEvent], error: LLMError | None = None) -> None:
        self.events = events
        self.error = error
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        self.requests.append(request)
        return self._stream()

    async def _stream(self) -> AsyncIterator[LLMStreamEvent]:
        for event in self.events:
            yield event
        if self.error is not None:
            raise self.error


def _text(events: list[LLMStreamEvent]) -> str:
    return "".join(e.text for e in events if e.kind == "text_delta")


async def test_stream_failure_before_any_event_switches_to_secondary() -> None:
    primary = _ScriptedStream([], _unavailable())
    secondary = FakeLLMClient([_response("openai")])
    client, _, _ = _chain(primary, secondary)

    events = [e async for e in client.stream(REQUEST)]

    assert _text(events) == "Answer from openai."
    assert secondary.requests == [REQUEST]


async def test_stream_failure_after_the_first_token_propagates_without_secondary() -> None:
    primary = _ScriptedStream(
        [LLMStreamEvent(kind="text_delta", text="Your order")], _unavailable()
    )
    secondary = FakeLLMClient([_response("openai")])
    client, _, _ = _chain(primary, secondary)
    seen: list[LLMStreamEvent] = []

    with pytest.raises(LLMError, match="unavailable"):
        async for event in client.stream(REQUEST):
            seen.append(event)

    assert _text(seen) == "Your order"
    assert secondary.requests == []


async def test_stream_failure_after_a_tool_call_event_does_not_switch_provider() -> None:
    call = ToolCall(id="t1", name="cancel_order", arguments={"order_id": 42})
    primary = _ScriptedStream([LLMStreamEvent(kind="tool_call", tool_call=call)], _unavailable())
    secondary = FakeLLMClient([_response("openai")])
    client, _, _ = _chain(primary, secondary)

    with pytest.raises(LLMError, match="unavailable"):
        async for _ in client.stream(REQUEST):
            pass

    assert secondary.requests == []


async def test_stream_all_failing_before_first_event_raises_all_providers_failed() -> None:
    primary = _ScriptedStream([], _unavailable())
    secondary = _ScriptedStream([], _unavailable())
    client, _, _ = _chain(primary, secondary)

    with pytest.raises(AllProvidersFailed):
        async for _ in client.stream(REQUEST):
            pass

    assert len(primary.requests) == 1
    assert len(secondary.requests) == 1


async def test_stream_non_recoverable_error_propagates_without_fallback() -> None:
    primary = _ScriptedStream([], LLMError("unauthorized", recoverable=False, status=401))
    secondary = FakeLLMClient([_response("openai")])
    client, _, _ = _chain(primary, secondary)

    with pytest.raises(LLMError, match="unauthorized"):
        async for _ in client.stream(REQUEST):
            pass

    assert secondary.requests == []


async def test_empty_chain_raises_all_providers_failed() -> None:
    with pytest.raises(AllProvidersFailed):
        await FallbackLLMClient([]).complete(REQUEST)
