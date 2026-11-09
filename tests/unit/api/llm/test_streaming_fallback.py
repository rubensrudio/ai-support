from collections.abc import AsyncIterator

import pytest

from api.llm.client import LLMRequest, LLMResponse, LLMStreamEvent, SystemBlock, ToolCall, Usage
from api.llm.fake import FakeLLMClient
from api.llm.streaming import events_from_response, stream_or_complete

REQUEST = LLMRequest(system=[SystemBlock("s")], messages=[])
RESPONSE = LLMResponse(
    text="Hello there",
    tool_calls=[ToolCall(id="c1", name="get_order", arguments={"order_id": 1})],
    usage=Usage(input_tokens=3, output_tokens=2),
    model="m",
    provider="p",
    stop_reason="end_turn",
)


class _NoStream:
    """Like the CLI adapters: `stream()` raises before returning an iterator."""

    def __init__(self) -> None:
        self.completed = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.completed += 1
        return RESPONSE

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        raise NotImplementedError("no streaming")


class _LazyNoStream(_NoStream):
    """Like a wrapper whose async generator raises on the first `anext`."""

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        async def gen() -> AsyncIterator[LLMStreamEvent]:
            raise NotImplementedError("no streaming")
            yield  # pragma: no cover

        return gen()


class _BreaksMidStream(_NoStream):
    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        async def gen() -> AsyncIterator[LLMStreamEvent]:
            yield LLMStreamEvent(kind="text_delta", text="Hel")
            raise NotImplementedError("late")

        return gen()


async def _collect(llm: object) -> list[LLMStreamEvent]:
    return [event async for event in stream_or_complete(llm, REQUEST)]  # type: ignore[arg-type]


def test_events_from_response_follow_the_stream_order() -> None:
    kinds = [event.kind for event in events_from_response(RESPONSE)]
    assert kinds == ["text_delta", "tool_call", "usage", "stop"]


@pytest.mark.parametrize("client", [_NoStream, _LazyNoStream])
async def test_non_streaming_provider_falls_back_to_complete(client: type[_NoStream]) -> None:
    llm = client()
    events = await _collect(llm)
    assert llm.completed == 1
    assert [e.kind for e in events] == ["text_delta", "tool_call", "usage", "stop"]
    assert events[0].text == "Hello there"
    assert events[1].tool_call == RESPONSE.tool_calls[0]


async def test_streaming_provider_is_passed_through() -> None:
    llm = FakeLLMClient([RESPONSE])
    events = await _collect(llm)
    assert "".join(e.text for e in events if e.kind == "text_delta") == "Hello there"
    assert events[-1].kind == "stop"


async def test_not_implemented_after_the_first_event_propagates() -> None:
    llm = _BreaksMidStream()
    with pytest.raises(NotImplementedError):
        await _collect(llm)
    assert llm.completed == 0
