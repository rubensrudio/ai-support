import json
from collections.abc import Callable
from typing import Any

import httpx2 as httpx
import pytest
from pydantic import SecretStr

from api.config import Settings
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
from api.llm.openai_adapter import PROVIDER, OpenAIAdapter

API_HOST = "api.openai.com"
FAKE_KEY = "sk-openai-test-0123456789-secret"
MODEL = "gpt-test-model"

Handler = Callable[[httpx.Request], httpx.Response]

ORDER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"order_id": {"type": "string"}, "verbose": {"type": "boolean"}},
    "required": ["order_id", "verbose"],
    "additionalProperties": False,
}


def _settings(key: str | None = FAKE_KEY, model: str | None = MODEL) -> Settings:
    return Settings(
        _env_file=None,
        llm_provider="anthropic",
        openai_api_key=SecretStr(key) if key is not None else None,
        secondary_model=model,
        llm_timeout_seconds=5.0,
    )


def _adapter(
    handler: Handler,
    captured: list[httpx.Request] | None = None,
    settings: Settings | None = None,
) -> OpenAIAdapter:
    sink = captured if captured is not None else []

    def guarded(request: httpx.Request) -> httpx.Response:
        if request.url.host != API_HOST:
            raise AssertionError(f"unexpected network call to {request.url.host}")
        sink.append(request)
        return handler(request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(guarded))
    return OpenAIAdapter(settings or _settings(), http_client=http_client)


def _request(**overrides: Any) -> LLMRequest:
    base: dict[str, Any] = {
        "system": [SystemBlock("You are a support agent.")],
        "messages": [ChatMessage(role="user", content="Where is order 42?")],
    }
    base.update(overrides)
    return LLMRequest(**base)


def _usage_body(prompt: int = 120, completion: int = 30, cached: int | None = 0) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    if cached is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    return usage


def _completion_body(
    message: dict[str, Any],
    finish_reason: str = "stop",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", **message},
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage if usage is not None else _usage_body(),
    }


def _ok(body: dict[str, Any]) -> Handler:
    return lambda _: httpx.Response(200, json=body)


def _sent(request: httpx.Request) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(request.content)
    return payload


# --- complete(): request conversion -------------------------------------------------------


async def test_tools_become_function_tools_with_input_schema_as_parameters() -> None:
    captured: list[httpx.Request] = []
    adapter = _adapter(_ok(_completion_body({"content": "ok"})), captured)
    tool = ToolSpec(name="get_order", description="Look up an order", input_schema=ORDER_SCHEMA)

    await adapter.complete(_request(tools=[tool], max_tokens=256))

    sent = _sent(captured[0])
    assert captured[0].url.path == "/v1/chat/completions"
    assert captured[0].headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert sent["model"] == MODEL
    assert sent["max_completion_tokens"] == 256
    assert sent["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_order",
                "description": "Look up an order",
                "parameters": ORDER_SCHEMA,
            },
        }
    ]
    assert sent["tools"][0]["function"]["parameters"] == tool.input_schema
    assert "stream" not in sent or sent["stream"] is False


async def test_without_tools_or_schema_the_fields_are_not_sent() -> None:
    captured: list[httpx.Request] = []
    adapter = _adapter(_ok(_completion_body({"content": "ok"})), captured)

    await adapter.complete(_request())

    sent = _sent(captured[0])
    assert "tools" not in sent
    assert "response_format" not in sent


async def test_system_blocks_are_joined_into_one_system_message() -> None:
    captured: list[httpx.Request] = []
    adapter = _adapter(_ok(_completion_body({"content": "ok"})), captured)
    system = [
        SystemBlock("Policy block.", cache_breakpoint=True),
        SystemBlock(""),
        SystemBlock("Persona block."),
    ]

    await adapter.complete(_request(system=system))

    messages = _sent(captured[0])["messages"]
    assert messages[0] == {"role": "system", "content": "Policy block.\n\nPersona block."}
    assert messages[1] == {"role": "user", "content": "Where is order 42?"}


async def test_empty_system_sends_no_system_message() -> None:
    captured: list[httpx.Request] = []
    adapter = _adapter(_ok(_completion_body({"content": "ok"})), captured)

    await adapter.complete(_request(system=[SystemBlock("")]))

    assert [m["role"] for m in _sent(captured[0])["messages"]] == ["user"]


async def test_assistant_tool_calls_and_tool_results_use_openai_message_shapes() -> None:
    captured: list[httpx.Request] = []
    adapter = _adapter(_ok(_completion_body({"content": "Shipped."})), captured)
    call = ToolCall(id="call_1", name="get_order", arguments={"order_id": "42"})
    messages = [
        ChatMessage(role="user", content="Where is order 42?"),
        ChatMessage(role="assistant", content="", tool_calls=[call]),
        ChatMessage(role="tool", content='{"status": "shipped"}', tool_call_id="call_1"),
        ChatMessage(role="assistant", content="Let me also say hi."),
    ]

    await adapter.complete(_request(messages=messages))

    sent = _sent(captured[0])["messages"]
    assert sent[2] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_order", "arguments": '{"order_id": "42"}'},
            }
        ],
    }
    assert json.loads(sent[2]["tool_calls"][0]["function"]["arguments"]) == call.arguments
    assert sent[3] == {"role": "tool", "tool_call_id": "call_1", "content": '{"status": "shipped"}'}
    assert sent[4] == {"role": "assistant", "content": "Let me also say hi."}


async def test_response_schema_becomes_strict_json_schema_response_format() -> None:
    captured: list[httpx.Request] = []
    body = _completion_body({"content": '{"order_id": "42", "verbose": false}'})
    adapter = _adapter(_ok(body), captured)

    response = await adapter.complete(_request(response_schema=ORDER_SCHEMA, purpose="triage"))

    assert _sent(captured[0])["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "triage", "schema": ORDER_SCHEMA, "strict": True},
    }
    assert json.loads(response.text) == {"order_id": "42", "verbose": False}


async def test_response_format_name_is_sanitized_from_the_purpose() -> None:
    captured: list[httpx.Request] = []
    adapter = _adapter(_ok(_completion_body({"content": "{}"})), captured)

    await adapter.complete(_request(response_schema=ORDER_SCHEMA, purpose="output guard/topic"))

    assert _sent(captured[0])["response_format"]["json_schema"]["name"] == "output_guard_topic"


# --- complete(): response conversion -------------------------------------------------------


async def test_tool_calls_response_becomes_tool_calls_with_dict_arguments() -> None:
    body = _completion_body(
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "get_order",
                        "arguments": '{"order_id": "42", "verbose": true}',
                    },
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "list_orders", "arguments": ""},
                },
            ],
        },
        finish_reason="tool_calls",
    )
    adapter = _adapter(_ok(body))

    response = await adapter.complete(_request())

    assert response.tool_calls == [
        ToolCall(id="call_1", name="get_order", arguments={"order_id": "42", "verbose": True}),
        ToolCall(id="call_2", name="list_orders", arguments={}),
    ]
    assert isinstance(response.tool_calls[0].arguments, dict)
    assert response.text == ""
    assert response.stop_reason == "tool_use"
    assert response.provider == PROVIDER == "openai"
    assert response.model == MODEL


@pytest.mark.parametrize("arguments", ["{not json", "[1, 2]"])
async def test_tool_call_arguments_that_are_not_a_json_object_are_not_recoverable(
    arguments: str,
) -> None:
    body = _completion_body(
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_order", "arguments": arguments},
                }
            ],
        },
        finish_reason="tool_calls",
    )
    adapter = _adapter(_ok(body))

    with pytest.raises(LLMError) as info:
        await adapter.complete(_request())

    assert info.value.recoverable is False
    assert arguments not in str(info.value)


async def test_text_response_and_finish_reason_mapping() -> None:
    adapter = _adapter(_ok(_completion_body({"content": "Order 42 shipped."})))

    response = await adapter.complete(_request())

    assert response.text == "Order 42 shipped."
    assert response.tool_calls == []
    assert response.stop_reason == "end_turn"


async def test_length_finish_reason_maps_to_max_tokens() -> None:
    adapter = _adapter(_ok(_completion_body({"content": "Order"}, finish_reason="length")))

    response = await adapter.complete(_request())

    assert response.stop_reason == "max_tokens"


async def test_refusal_maps_to_refusal_stop_reason_with_empty_text() -> None:
    body = _completion_body({"content": None, "refusal": "I can't help with that."})
    adapter = _adapter(_ok(body))

    response = await adapter.complete(_request(response_schema=ORDER_SCHEMA))

    assert response.text == ""
    assert response.stop_reason == "refusal"


async def test_cached_tokens_are_read_and_subtracted_from_input_tokens() -> None:
    body = _completion_body({"content": "ok"}, usage=_usage_body(1200, 40, cached=1024))
    adapter = _adapter(_ok(body))

    response = await adapter.complete(_request())

    assert response.usage == Usage(
        input_tokens=176, output_tokens=40, cache_read_tokens=1024, cache_write_tokens=0
    )


async def test_missing_prompt_tokens_details_means_zero_cache_tokens() -> None:
    body = _completion_body({"content": "ok"}, usage=_usage_body(120, 30, cached=None))
    adapter = _adapter(_ok(body))

    response = await adapter.complete(_request())

    assert response.usage == Usage(input_tokens=120, output_tokens=30)


async def test_response_without_choices_is_not_recoverable() -> None:
    body = _completion_body({"content": "ok"})
    body["choices"] = []
    adapter = _adapter(_ok(body))

    with pytest.raises(LLMError) as info:
        await adapter.complete(_request())

    assert info.value.recoverable is False


# --- errors --------------------------------------------------------------------------------


def _error(status: int) -> Handler:
    body = {"error": {"message": f"echo {FAKE_KEY} body-secret", "type": "x", "code": "y"}}
    return lambda _: httpx.Response(status, json=body)


@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_rate_limit_and_server_errors_are_recoverable(status: int) -> None:
    adapter = _adapter(_error(status))

    with pytest.raises(LLMError) as info:
        await adapter.complete(_request())

    assert info.value.recoverable is True
    assert info.value.status == status
    assert FAKE_KEY not in str(info.value)
    assert "body-secret" not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_client_errors_are_not_recoverable(status: int) -> None:
    adapter = _adapter(_error(status))

    with pytest.raises(LLMError) as info:
        await adapter.complete(_request())

    assert info.value.recoverable is False
    assert info.value.status == status
    assert "body-secret" not in str(info.value)


async def test_timeout_becomes_recoverable_timeout_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    adapter = _adapter(handler)

    with pytest.raises(LLMTimeoutError) as info:
        await adapter.complete(_request())

    assert info.value.recoverable is True


async def test_connection_error_is_recoverable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    adapter = _adapter(handler)

    with pytest.raises(LLMError) as info:
        await adapter.complete(_request())

    assert info.value.recoverable is True


async def test_invalid_json_body_is_not_recoverable() -> None:
    adapter = _adapter(
        lambda _: httpx.Response(
            200, headers={"content-type": "application/json"}, content=b"{oops"
        )
    )

    with pytest.raises(LLMError) as info:
        await adapter.complete(_request())

    assert info.value.recoverable is False


@pytest.mark.parametrize(
    ("key", "model", "missing"),
    [(None, MODEL, "OPENAI_API_KEY"), ("", MODEL, "OPENAI_API_KEY"), (FAKE_KEY, None, "MODEL")],
)
async def test_missing_key_or_model_fails_on_call_not_on_construction(
    key: str | None, model: str | None, missing: str
) -> None:
    captured: list[httpx.Request] = []
    adapter = _adapter(_ok(_completion_body({"content": "ok"})), captured, _settings(key, model))

    with pytest.raises(LLMError) as info:
        await adapter.complete(_request())
    assert info.value.recoverable is False
    assert missing in str(info.value)

    with pytest.raises(LLMError) as stream_info:
        _ = [event async for event in adapter.stream(_request())]
    assert stream_info.value.recoverable is False
    assert captured == []


def test_sdk_runs_without_its_own_retries_and_with_the_configured_timeout() -> None:
    adapter = OpenAIAdapter(_settings())

    assert adapter.max_retries == 0
    assert adapter.timeout_seconds == 5.0
    assert adapter.model == MODEL


# --- stream() --------------------------------------------------------------------------------


def _chunk(
    delta: dict[str, Any] | None = None,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    choices = (
        [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish_reason}]
    )
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": MODEL,
        "choices": choices,
        "usage": usage,
    }


def _sse_response(chunks: list[dict[str, Any]], done: bool = True) -> httpx.Response:
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    if done:
        lines.append("data: [DONE]\n\n")
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content="".join(lines).encode()
    )


def _text_stream() -> list[dict[str, Any]]:
    return [
        _chunk({"role": "assistant", "content": ""}),
        _chunk({"content": "Order 42 "}),
        _chunk({"content": "has shipped."}),
        _chunk({}, finish_reason="stop"),
        _chunk(usage=_usage_body(1200, 40, cached=1024)),
    ]


def _tool_stream() -> list[dict[str, Any]]:
    def tool_delta(index: int, **fields: Any) -> dict[str, Any]:
        return {"tool_calls": [{"index": index, **fields}]}

    return [
        _chunk({"role": "assistant", "content": "Checking."}),
        _chunk(
            tool_delta(
                0, id="call_1", type="function", function={"name": "get_order", "arguments": ""}
            )
        ),
        _chunk(tool_delta(0, function={"arguments": '{"order_'})),
        _chunk(
            tool_delta(
                1, id="call_2", type="function", function={"name": "list_orders", "arguments": ""}
            )
        ),
        _chunk(tool_delta(0, function={"arguments": 'id": "42"}'})),
        _chunk({}, finish_reason="tool_calls"),
        _chunk(usage=_usage_body(120, 30, cached=0)),
    ]


async def _collect(adapter: OpenAIAdapter, request: LLMRequest) -> list[LLMStreamEvent]:
    return [event async for event in adapter.stream(request)]


async def test_text_stream_yields_text_deltas_then_usage_and_stop() -> None:
    captured: list[httpx.Request] = []
    adapter = _adapter(lambda _: _sse_response(_text_stream()), captured)

    events = await _collect(adapter, _request())

    assert [event.kind for event in events] == ["text_delta", "text_delta", "usage", "stop"]
    assert [event.text for event in events[:2]] == ["Order 42 ", "has shipped."]
    assert events[2].usage == Usage(input_tokens=176, output_tokens=40, cache_read_tokens=1024)
    assert events[3].stop_reason == "end_turn"
    sent = _sent(captured[0])
    assert sent["stream"] is True
    assert sent["stream_options"] == {"include_usage": True}


async def test_stream_accumulates_tool_calls_by_index() -> None:
    adapter = _adapter(lambda _: _sse_response(_tool_stream()))

    events = await _collect(adapter, _request())

    assert [event.kind for event in events] == [
        "text_delta",
        "tool_call",
        "tool_call",
        "usage",
        "stop",
    ]
    assert events[1].tool_call == ToolCall(
        id="call_1", name="get_order", arguments={"order_id": "42"}
    )
    assert events[2].tool_call == ToolCall(id="call_2", name="list_orders", arguments={})
    assert events[3].usage == Usage(input_tokens=120, output_tokens=30)
    assert events[4].stop_reason == "tool_use"


async def test_stream_uses_the_same_parameters_as_complete() -> None:
    captured: list[httpx.Request] = []
    adapter = _adapter(lambda _: _sse_response(_text_stream()), captured)
    tool = ToolSpec(name="get_order", description="Look up an order", input_schema=ORDER_SCHEMA)
    request = _request(tools=[tool], response_schema=ORDER_SCHEMA)

    await _collect(adapter, request)

    sent = _sent(captured[0])
    assert sent["tools"][0]["function"]["parameters"] == ORDER_SCHEMA
    assert sent["response_format"]["json_schema"]["strict"] is True
    assert sent["messages"][0]["role"] == "system"


async def test_stream_with_invalid_tool_arguments_is_not_recoverable() -> None:
    chunks = [
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_order", "arguments": "{broken"},
                    }
                ]
            }
        ),
        _chunk({}, finish_reason="tool_calls"),
        _chunk(usage=_usage_body()),
    ]
    adapter = _adapter(lambda _: _sse_response(chunks))

    with pytest.raises(LLMError) as info:
        await _collect(adapter, _request())

    assert info.value.recoverable is False


@pytest.mark.parametrize(("status", "recoverable"), [(429, True), (503, True), (401, False)])
async def test_stream_http_errors_follow_the_same_rule(status: int, recoverable: bool) -> None:
    adapter = _adapter(_error(status))

    with pytest.raises(LLMError) as info:
        await _collect(adapter, _request())

    assert info.value.recoverable is recoverable
    assert info.value.status == status
    assert "body-secret" not in str(info.value)


async def test_stream_error_event_after_first_delta_raises_recoverable_error() -> None:
    error = {"error": {"message": "body-secret overloaded", "type": "server_error"}}
    lines = [
        f"data: {json.dumps(_chunk({'content': 'Order '}))}\n\n",
        f"data: {json.dumps(error)}\n\n",
    ]
    response = httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content="".join(lines).encode()
    )
    adapter = _adapter(lambda _: response)
    seen: list[LLMStreamEvent] = []

    with pytest.raises(LLMError) as info:
        async for event in adapter.stream(_request()):
            seen.append(event)

    assert [event.kind for event in seen] == ["text_delta"]
    assert info.value.recoverable is True
    assert "body-secret" not in str(info.value)


async def test_stream_invalid_request_error_event_is_not_recoverable() -> None:
    error = {"error": {"message": "bad", "type": "invalid_request_error"}}
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=f"data: {json.dumps(error)}\n\n".encode(),
    )
    adapter = _adapter(lambda _: response)

    with pytest.raises(LLMError) as info:
        await _collect(adapter, _request())

    assert info.value.recoverable is False


async def test_stream_ending_without_finish_reason_raises_recoverable_error() -> None:
    chunks = [_chunk({"content": "Order "})]
    adapter = _adapter(lambda _: _sse_response(chunks, done=False))

    with pytest.raises(LLMError) as info:
        await _collect(adapter, _request())

    assert info.value.recoverable is True


async def test_stream_timeout_becomes_timeout_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    adapter = _adapter(handler)

    with pytest.raises(LLMTimeoutError):
        await _collect(adapter, _request())
