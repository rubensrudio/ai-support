import json
from collections.abc import Callable
from typing import Any

import httpx2 as httpx
import pytest
from pydantic import SecretStr

from api.config import MissingConfigError, Settings
from api.llm.anthropic_adapter import AnthropicAdapter
from api.llm.client import (
    ChatMessage,
    LLMError,
    LLMRequest,
    LLMTimeoutError,
    SystemBlock,
    ToolCall,
    ToolSpec,
)

API_HOST = "api.anthropic.com"
FAKE_KEY = "sk-ant-test-0123456789-secret"
MODEL = "claude-test-model"

Handler = Callable[[httpx.Request], httpx.Response]


def _settings(key: str | None = FAKE_KEY, model: str | None = MODEL) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        llm_provider="anthropic",
        anthropic_api_key=SecretStr(key) if key is not None else None,
        primary_model=model,
        llm_timeout_seconds=5.0,
    )


def _guarded(handler: Handler, captured: list[httpx.Request]) -> Handler:
    def wrapper(request: httpx.Request) -> httpx.Response:
        if request.url.host != API_HOST:
            raise AssertionError(f"unexpected network call to {request.url.host}")
        captured.append(request)
        return handler(request)

    return wrapper


def _adapter(handler: Handler, captured: list[httpx.Request] | None = None) -> AnthropicAdapter:
    sink = captured if captured is not None else []
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_guarded(handler, sink)))
    return AnthropicAdapter(_settings(), http_client=http_client)


def _message_body(content: list[dict[str, Any]], stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 30,
            "cache_read_input_tokens": 7,
            "cache_creation_input_tokens": 3,
        },
    }


def _request(**overrides: Any) -> LLMRequest:
    base: dict[str, Any] = {
        "system": [SystemBlock("You are a support agent.")],
        "messages": [ChatMessage(role="user", content="Where is order 42?")],
    }
    base.update(overrides)
    return LLMRequest(**base)


def _payload(request: httpx.Request) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(request.content)
    return data


async def test_tool_use_response_becomes_tool_calls() -> None:
    captured: list[httpx.Request] = []
    body = _message_body(
        [
            {"type": "text", "text": "Let me check."},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "get_order",
                "input": {"order_id": "42"},
            },
        ],
        stop_reason="tool_use",
    )
    adapter = _adapter(lambda _: httpx.Response(200, json=body), captured)
    tool = ToolSpec(
        name="get_order",
        description="Fetch an order",
        input_schema={"type": "object", "properties": {"order_id": {"type": "string"}}},
    )

    response = await adapter.complete(_request(tools=[tool]))

    assert response.tool_calls == [
        ToolCall(id="toolu_1", name="get_order", arguments={"order_id": "42"})
    ]
    assert response.text == "Let me check."
    assert response.stop_reason == "tool_use"
    assert response.provider == "anthropic"
    assert response.model == MODEL
    assert response.usage.input_tokens == 120
    assert response.usage.output_tokens == 30
    assert response.usage.cache_read_tokens == 7
    assert response.usage.cache_write_tokens == 3

    sent = _payload(captured[0])
    assert sent["model"] == MODEL
    assert sent["system"] == [{"type": "text", "text": "You are a support agent."}]
    assert sent["tools"][0]["name"] == "get_order"
    assert sent["tools"][0]["input_schema"] == tool.input_schema
    assert "cache_control" not in json.dumps(sent)
    headers = captured[0].headers
    assert FAKE_KEY in headers.get("x-api-key", headers.get("authorization", ""))
    assert headers["x-stainless-retry-count"] == "0"


async def test_conversation_with_tool_results_is_converted() -> None:
    captured: list[httpx.Request] = []
    body = _message_body([{"type": "text", "text": "Shipped."}])
    adapter = _adapter(lambda _: httpx.Response(200, json=body), captured)
    call_a = ToolCall(id="t1", name="get_order", arguments={"order_id": "1"})
    call_b = ToolCall(id="t2", name="get_order", arguments={"order_id": "2"})
    messages = [
        ChatMessage(role="user", content="Orders 1 and 2?"),
        ChatMessage(role="assistant", content="", tool_calls=[call_a, call_b]),
        ChatMessage(role="tool", content='{"status": "shipped"}', tool_call_id="t1"),
        ChatMessage(role="tool", content='{"status": "pending"}', tool_call_id="t2"),
    ]

    await adapter.complete(_request(messages=messages))

    sent = _payload(captured[0])["messages"]
    assert sent[0] == {"role": "user", "content": "Orders 1 and 2?"}
    assert sent[1]["role"] == "assistant"
    assert [b["type"] for b in sent[1]["content"]] == ["tool_use", "tool_use"]
    assert sent[1]["content"][0]["input"] == {"order_id": "1"}
    assert sent[2]["role"] == "user"
    assert [b["tool_use_id"] for b in sent[2]["content"]] == ["t1", "t2"]
    assert len(sent) == 3


@pytest.mark.parametrize("status", [429, 500, 502, 503, 529])
async def test_retryable_statuses_are_recoverable(status: int) -> None:
    error = {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
    adapter = _adapter(lambda _: httpx.Response(status, json=error))

    with pytest.raises(LLMError) as caught:
        await adapter.complete(_request())

    assert caught.value.recoverable is True
    assert caught.value.status == status
    assert FAKE_KEY not in str(caught.value)


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_client_errors_are_not_recoverable(status: int) -> None:
    error = {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": f"bad key {FAKE_KEY}"},
    }
    adapter = _adapter(lambda _: httpx.Response(status, json=error))

    with pytest.raises(LLMError) as caught:
        await adapter.complete(_request())

    assert caught.value.recoverable is False
    assert caught.value.status == status
    assert FAKE_KEY not in str(caught.value)
    assert caught.value.__cause__ is None


async def test_timeout_becomes_recoverable_timeout_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    adapter = _adapter(handler)

    with pytest.raises(LLMTimeoutError) as caught:
        await adapter.complete(_request())
    assert caught.value.recoverable is True


async def test_connection_error_is_recoverable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    adapter = _adapter(handler)

    with pytest.raises(LLMError) as caught:
        await adapter.complete(_request())
    assert caught.value.recoverable is True
    assert caught.value.status is None


async def test_response_schema_sends_mandatory_json_schema() -> None:
    captured: list[httpx.Request] = []
    schema = {
        "type": "object",
        "properties": {"category": {"type": "string"}},
        "required": ["category"],
        "additionalProperties": False,
    }
    body = _message_body([{"type": "text", "text": '{"category": "billing"}'}])
    adapter = _adapter(lambda _: httpx.Response(200, json=body), captured)

    response = await adapter.complete(_request(response_schema=schema, purpose="triage"))

    sent = _payload(captured[0])
    assert sent["output_config"] == {"format": {"type": "json_schema", "schema": schema}}
    assert json.loads(response.text) == {"category": "billing"}


async def test_request_without_schema_has_no_output_config() -> None:
    captured: list[httpx.Request] = []
    body = _message_body([{"type": "text", "text": "hi"}])
    adapter = _adapter(lambda _: httpx.Response(200, json=body), captured)

    await adapter.complete(_request(max_tokens=256))

    sent = _payload(captured[0])
    assert "output_config" not in sent
    assert "tools" not in sent
    assert sent["max_tokens"] == 256


def test_missing_api_key_raises_missing_config_error() -> None:
    with pytest.raises(MissingConfigError, match="ANTHROPIC_API_KEY"):
        AnthropicAdapter(_settings(key=None))
    with pytest.raises(MissingConfigError, match="ANTHROPIC_API_KEY"):
        AnthropicAdapter(_settings(key=""))


def test_missing_model_raises_missing_config_error() -> None:
    with pytest.raises(MissingConfigError, match="ATLAS_PRIMARY_MODEL"):
        AnthropicAdapter(_settings(model=None))


def test_sdk_client_has_no_retries_and_configured_timeout() -> None:
    adapter = AnthropicAdapter(_settings())
    assert adapter.max_retries == 0
    assert adapter.timeout_seconds == 5.0


async def test_other_sdk_errors_become_non_recoverable_without_request_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import anthropic

    sdk_request = httpx.Request(
        "POST",
        f"https://{API_HOST}/v1/messages",
        headers={"authorization": f"Bearer {FAKE_KEY}"},
    )
    sdk_response = httpx.Response(200, request=sdk_request, json={"echo": FAKE_KEY})

    async def invalid_response(**_: Any) -> Any:
        raise anthropic.APIResponseValidationError(
            response=sdk_response, body={"echo": FAKE_KEY}, message=f"bad body {FAKE_KEY}"
        )

    adapter = AnthropicAdapter(_settings())
    monkeypatch.setattr(adapter._client.messages, "create", invalid_response)

    with pytest.raises(LLMError) as caught:
        await adapter.complete(_request())

    error = caught.value
    assert not isinstance(error, anthropic.APIError)
    assert error.recoverable is False
    assert error.status is None
    assert FAKE_KEY not in str(error)
    assert API_HOST not in str(error)
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def _assert_malformed_error(error: LLMError) -> None:
    assert error.recoverable is False
    assert error.status is None
    assert FAKE_KEY not in str(error)
    assert "leak" not in str(error)
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


async def test_200_with_non_json_body_becomes_non_recoverable_error() -> None:
    body = f"<html>leak {FAKE_KEY}</html>".encode()
    adapter = _adapter(
        lambda _: httpx.Response(200, content=body, headers={"content-type": "application/json"})
    )

    with pytest.raises(LLMError) as caught:
        await adapter.complete(_request())

    _assert_malformed_error(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        [{"leak": FAKE_KEY}],
        {"leak": FAKE_KEY},
        {**_message_body([]), "usage": None},
        {**_message_body([]), "content": f"leak {FAKE_KEY}"},
        {**_message_body([]), "content": [{"type": "tool_use", "leak": FAKE_KEY}]},
    ],
    ids=["json-list", "missing-fields", "null-usage", "string-content", "broken-tool-use"],
)
async def test_malformed_200_response_becomes_non_recoverable_error(body: Any) -> None:
    adapter = _adapter(lambda _: httpx.Response(200, json=body))

    with pytest.raises(LLMError) as caught:
        await adapter.complete(_request())

    _assert_malformed_error(caught.value)
