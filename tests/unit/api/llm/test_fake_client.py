import pytest

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
from api.llm.fake import FAKE_MODEL, FAKE_PROVIDER, FakeLLMClient


def _request(text: str) -> LLMRequest:
    return LLMRequest(
        system=[SystemBlock("You are a test.")],
        messages=[ChatMessage(role="user", content=text)],
    )


def _response(text: str, tool_calls: list[ToolCall] | None = None) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=tool_calls or [],
        usage=Usage(input_tokens=10, output_tokens=5),
        model=FAKE_MODEL,
        provider=FAKE_PROVIDER,
        stop_reason="end_turn",
    )


def test_fake_client_implements_protocol() -> None:
    assert isinstance(FakeLLMClient(script=[]), LLMClient)


async def test_complete_returns_script_in_order_and_records_requests() -> None:
    first, second = _response("first"), _response("second")
    client = FakeLLMClient(script=[first, second])
    req_a, req_b = _request("a"), _request("b")

    assert await client.complete(req_a) == first
    assert await client.complete(req_b) == second
    assert client.requests == [req_a, req_b]


async def test_scripted_recoverable_error_is_raised_on_its_call() -> None:
    error = LLMError("rate limited", recoverable=True, status=429)
    client = FakeLLMClient(script=[_response("ok"), error, _response("after")])

    assert (await client.complete(_request("1"))).text == "ok"
    with pytest.raises(LLMError) as caught:
        await client.complete(_request("2"))
    assert caught.value is error
    assert caught.value.recoverable is True
    assert (await client.complete(_request("3"))).text == "after"
    assert len(client.requests) == 3


async def test_exhausted_script_raises_explicit_error() -> None:
    client = FakeLLMClient(script=[])
    with pytest.raises(LLMError, match="fake script exhausted") as caught:
        await client.complete(_request("x"))
    assert caught.value.recoverable is False


async def test_stream_emits_deltas_then_usage_and_stop() -> None:
    text = "Hello there, this is  a scripted\nanswer."
    call = ToolCall(id="t1", name="lookup_order", arguments={"order_id": "42"})
    client = FakeLLMClient(script=[_response(text, [call])])
    request = _request("stream me")

    events: list[LLMStreamEvent] = [event async for event in client.stream(request)]

    deltas = [e for e in events if e.kind == "text_delta"]
    assert len(deltas) > 1
    assert "".join(e.text for e in deltas) == text
    assert [e.tool_call for e in events if e.kind == "tool_call"] == [call]
    assert [e.kind for e in events[-2:]] == ["usage", "stop"]
    assert events[-2].usage == Usage(input_tokens=10, output_tokens=5)
    assert events[-1].stop_reason == "end_turn"
    assert client.requests == [request]


async def test_stream_raises_scripted_error() -> None:
    client = FakeLLMClient(script=[LLMError("boom", recoverable=True, status=503)])
    with pytest.raises(LLMError, match="boom"):
        async for _ in client.stream(_request("x")):
            pass
