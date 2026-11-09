import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx2 as httpx
import pytest
from pydantic import SecretStr

from api.config import Settings
from api.llm.anthropic_adapter import AnthropicAdapter
from api.llm.client import (
    ChatMessage,
    LLMError,
    LLMRequest,
    LLMStreamEvent,
    LLMTimeoutError,
    SystemBlock,
    ToolCall,
    ToolSpec,
    Usage,
)

API_HOST = "api.anthropic.com"
FAKE_KEY = "sk-ant-test-0123456789-secret"
MODEL = "claude-test-model"

Handler = Callable[[httpx.Request], httpx.Response]


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        llm_provider="anthropic",
        anthropic_api_key=SecretStr(FAKE_KEY),
        primary_model=MODEL,
        llm_timeout_seconds=5.0,
    )


def _adapter(handler: Handler, captured: list[httpx.Request] | None = None) -> AnthropicAdapter:
    sink = captured if captured is not None else []

    def guarded(request: httpx.Request) -> httpx.Response:
        if request.url.host != API_HOST:
            raise AssertionError(f"unexpected network call to {request.url.host}")
        sink.append(request)
        return handler(request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(guarded))
    return AnthropicAdapter(_settings(), http_client=http_client)


def _request(**overrides: Any) -> LLMRequest:
    base: dict[str, Any] = {
        "system": [SystemBlock("You are a support agent.")],
        "messages": [ChatMessage(role="user", content="Where is order 42?")],
    }
    base.update(overrides)
    return LLMRequest(**base)


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _message_start() -> str:
    return _sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_stream",
                "type": "message",
                "role": "assistant",
                "model": MODEL,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 7,
                    "cache_creation_input_tokens": 3,
                },
            },
        },
    )


def _text_block(index: int, chunks: list[str]) -> list[str]:
    events = [
        _sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "text", "text": ""},
            },
        )
    ]
    events.extend(
        _sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "text_delta", "text": chunk},
            },
        )
        for chunk in chunks
    )
    events.append(_sse("content_block_stop", {"type": "content_block_stop", "index": index}))
    return events


def _tool_block(index: int, tool_id: str, name: str, json_chunks: list[str]) -> list[str]:
    events = [
        _sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}},
            },
        )
    ]
    events.extend(
        _sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "input_json_delta", "partial_json": chunk},
            },
        )
        for chunk in json_chunks
    )
    events.append(_sse("content_block_stop", {"type": "content_block_stop", "index": index}))
    return events


def _message_end(stop_reason: str = "tool_use", output_tokens: int = 30) -> list[str]:
    return [
        _sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": output_tokens},
            },
        ),
        _sse("message_stop", {"type": "message_stop"}),
    ]


def _recorded_stream() -> list[str]:
    """A stream as the Messages API sends it: text, a tool_use block with split JSON, the end."""
    return [
        _message_start(),
        _sse("ping", {"type": "ping"}),
        *_text_block(0, ["Let me ", "check that ", "order."]),
        *_tool_block(1, "toolu_1", "get_order", ['{"order_', 'id": "4', '2", "verbose": true}']),
        *_message_end(),
    ]


def _sse_response(events: list[str]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content="".join(events).encode(),
    )


async def _collect(adapter: AnthropicAdapter, request: LLMRequest) -> list[LLMStreamEvent]:
    return [event async for event in adapter.stream(request)]


async def test_recorded_stream_yields_text_deltas_then_tool_call_usage_and_stop() -> None:
    adapter = _adapter(lambda _: _sse_response(_recorded_stream()))

    events = await _collect(adapter, _request())

    assert [event.kind for event in events] == [
        "text_delta",
        "text_delta",
        "text_delta",
        "tool_call",
        "usage",
        "stop",
    ]
    assert "".join(event.text for event in events if event.kind == "text_delta") == (
        "Let me check that order."
    )
    assert events[3].tool_call == ToolCall(
        id="toolu_1", name="get_order", arguments={"order_id": "42", "verbose": True}
    )
    assert events[4].usage == Usage(
        input_tokens=120, output_tokens=30, cache_read_tokens=7, cache_write_tokens=3
    )
    assert events[5].stop_reason == "tool_use"


async def test_stream_request_uses_the_same_parameters_as_complete() -> None:
    captured: list[httpx.Request] = []
    adapter = _adapter(lambda _: _sse_response(_recorded_stream()), captured)
    tool = ToolSpec(
        name="get_order",
        description="Fetch an order",
        input_schema={"type": "object", "properties": {"order_id": {"type": "string"}}},
    )
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}

    await _collect(adapter, _request(tools=[tool], response_schema=schema, max_tokens=256))

    payload = json.loads(captured[0].content)
    assert payload["stream"] is True
    assert payload["model"] == MODEL
    assert payload["max_tokens"] == 256
    assert payload["system"] == [{"type": "text", "text": "You are a support agent."}]
    assert payload["tools"][0]["name"] == "get_order"
    assert payload["output_config"] == {"format": {"type": "json_schema", "schema": schema}}
    assert payload["messages"] == [{"role": "user", "content": "Where is order 42?"}]


async def test_tool_without_input_deltas_has_empty_arguments() -> None:
    events_sse = [
        _message_start(),
        *_tool_block(0, "toolu_2", "list_orders", []),
        *_message_end(),
    ]
    adapter = _adapter(lambda _: _sse_response(events_sse))

    events = await _collect(adapter, _request())

    assert events[0].tool_call == ToolCall(id="toolu_2", name="list_orders", arguments={})


async def test_text_only_stream_ends_with_usage_and_end_turn() -> None:
    events_sse = [_message_start(), *_text_block(0, ["Hi"]), *_message_end("end_turn", 2)]
    adapter = _adapter(lambda _: _sse_response(events_sse))

    events = await _collect(adapter, _request())

    assert [event.kind for event in events] == ["text_delta", "usage", "stop"]
    assert events[1].usage is not None and events[1].usage.output_tokens == 2
    assert events[2].stop_reason == "end_turn"


@pytest.mark.parametrize("status", [429, 500, 529])
async def test_retryable_http_error_before_first_event_is_recoverable(status: int) -> None:
    body = {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
    adapter = _adapter(lambda _: httpx.Response(status, json=body))

    with pytest.raises(LLMError) as caught:
        await _collect(adapter, _request())

    assert caught.value.recoverable is True
    assert caught.value.status == status
    assert FAKE_KEY not in str(caught.value)


@pytest.mark.parametrize("status", [400, 401, 404])
async def test_client_http_error_before_first_event_is_not_recoverable(status: int) -> None:
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}
    adapter = _adapter(lambda _: httpx.Response(status, json=body))

    with pytest.raises(LLMError) as caught:
        await _collect(adapter, _request())

    assert caught.value.recoverable is False
    assert caught.value.status == status
    assert caught.value.__cause__ is None


async def test_timeout_before_first_event_becomes_timeout_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(LLMTimeoutError):
        await _collect(_adapter(handler), _request())


async def test_connection_error_before_first_event_is_recoverable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(LLMError) as caught:
        await _collect(_adapter(handler), _request())

    assert caught.value.recoverable is True
    assert not isinstance(caught.value, LLMTimeoutError)


async def test_error_event_after_first_delta_raises_recoverable_error() -> None:
    error = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    events_sse = [_message_start(), *_text_block(0, ["Partial"])[:2], _sse("error", error)]
    adapter = _adapter(lambda _: _sse_response(events_sse))
    received: list[LLMStreamEvent] = []

    with pytest.raises(LLMError) as caught:
        async for event in adapter.stream(_request()):
            received.append(event)

    assert [event.text for event in received] == ["Partial"]
    assert caught.value.recoverable is True
    assert caught.value.status == 529


async def test_invalid_request_error_event_mid_stream_is_not_recoverable() -> None:
    error = {"type": "error", "error": {"type": "invalid_request_error", "message": "no"}}
    adapter = _adapter(lambda _: _sse_response([_message_start(), _sse("error", error)]))

    with pytest.raises(LLMError) as caught:
        await _collect(adapter, _request())

    assert caught.value.recoverable is False


class _BrokenBody(httpx.AsyncByteStream):
    def __init__(self, first: bytes, request: httpx.Request) -> None:
        self._first = first
        self._request = request

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._first
        raise httpx.ReadError("connection reset", request=self._request)


async def test_connection_drop_after_first_event_raises_recoverable_error() -> None:
    head = "".join([_message_start(), *_text_block(0, ["Hel"])[:2]]).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_BrokenBody(head, request),
        )

    received: list[LLMStreamEvent] = []
    with pytest.raises(LLMError) as caught:
        async for event in _adapter(handler).stream(_request()):
            received.append(event)

    assert [event.text for event in received] == ["Hel"]
    assert caught.value.recoverable is True


async def test_stream_ending_without_message_stop_raises_error() -> None:
    truncated = [_message_start(), *_text_block(0, ["Hello"])]
    adapter = _adapter(lambda _: _sse_response(truncated))

    with pytest.raises(LLMError) as caught:
        await _collect(adapter, _request())

    assert caught.value.recoverable is True


async def test_invalid_tool_json_raises_non_recoverable_error() -> None:
    events_sse = [
        _message_start(),
        *_tool_block(0, "toolu_3", "get_order", ['{"order_']),
        *_message_end(),
    ]
    adapter = _adapter(lambda _: _sse_response(events_sse))

    with pytest.raises(LLMError) as caught:
        await _collect(adapter, _request())

    assert caught.value.recoverable is False
    assert "order_" not in str(caught.value)
