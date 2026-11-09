"""`ClaudeCliAdapter` with an injected fake runner: no process and no real CLI call."""

import json
import os
from dataclasses import dataclass, field
from typing import Any

import pytest

from api.config import Settings
from api.llm.claude_cli_adapter import PROVIDER, ClaudeCliAdapter
from api.llm.cli_runner import CliResult, render_conversation
from api.llm.client import (
    ChatMessage,
    LLMClient,
    LLMError,
    LLMRequest,
    LLMTimeoutError,
    SystemBlock,
    ToolSpec,
    Usage,
)

SUCCESS = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 2310,
    "num_turns": 1,
    "result": "Your order shipped yesterday.",
    "session_id": "4f1c2d3e-0000-4000-8000-000000000000",
    "total_cost_usd": 0.0123,
    "usage": {
        "input_tokens": 12,
        "cache_creation_input_tokens": 1500,
        "cache_read_input_tokens": 9000,
        "output_tokens": 34,
        "server_tool_use": {"web_search_requests": 0},
        "service_tier": "standard",
    },
    "modelUsage": {"claude-sonnet-test": {"inputTokens": 12, "outputTokens": 34}},
}


@dataclass
class FakeRunner:
    result: CliResult | BaseException
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(
        self,
        argv: list[str],
        stdin: str,
        timeout: float,
        cwd: str,
        *,
        env_remove: frozenset[str] = frozenset(),
    ) -> CliResult:
        self.calls.append(
            {
                "argv": list(argv),
                "stdin": stdin,
                "timeout": timeout,
                "cwd": cwd,
                "cwd_listing": sorted(os.listdir(cwd)),
                "env_remove": env_remove,
            }
        )
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _ok(payload: dict[str, Any], returncode: int = 0) -> FakeRunner:
    return FakeRunner(CliResult(returncode=returncode, stdout=json.dumps(payload), stderr=""))


def _settings(**values: Any) -> Settings:
    values.setdefault("claude_cli_path", "/opt/bin/claude")
    values.setdefault("cli_timeout_seconds", 42.0)
    return Settings(_env_file=None, llm_provider="claude_cli", **values)  # type: ignore[call-arg]


def _request(**values: Any) -> LLMRequest:
    values.setdefault("system", [SystemBlock("You are Atlas."), SystemBlock("Be brief.")])
    values.setdefault(
        "messages", [ChatMessage(role="user", content="Where is my order SECRET-PROMPT?")]
    )
    return LLMRequest(**values)


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


async def test_argv_has_no_shell_and_no_prompt() -> None:
    runner = _ok(SUCCESS)
    adapter = ClaudeCliAdapter(_settings(), runner=runner)

    await adapter.complete(_request())

    call = runner.calls[0]
    argv = call["argv"]
    assert argv[:4] == ["/opt/bin/claude", "-p", "--output-format", "json"]
    assert _flag(argv, "--system-prompt") == "You are Atlas.\n\nBe brief."
    assert _flag(argv, "--tools") == ""
    assert _flag(argv, "--setting-sources") == ""
    assert "--strict-mcp-config" in argv
    assert "--no-session-persistence" in argv
    assert "--disable-slash-commands" in argv
    assert "--model" not in argv
    assert "--json-schema" not in argv
    assert all("SECRET-PROMPT" not in arg for arg in argv)
    assert "sh" not in argv and "-c" not in argv
    assert "SECRET-PROMPT" in call["stdin"]
    assert call["timeout"] == 42.0
    assert call["cwd_listing"] == []
    assert not os.path.exists(call["cwd"])
    assert "ANTHROPIC_API_KEY" in call["env_remove"]


async def test_model_flag_from_argument_or_settings() -> None:
    runner = _ok(SUCCESS)
    await ClaudeCliAdapter(_settings(primary_model="sonnet"), runner=runner).complete(_request())
    await ClaudeCliAdapter(_settings(), model="opus", runner=runner).complete(_request())

    assert _flag(runner.calls[0]["argv"], "--model") == "sonnet"
    assert _flag(runner.calls[1]["argv"], "--model") == "opus"


async def test_success_parses_text_usage_and_model() -> None:
    adapter = ClaudeCliAdapter(_settings(), runner=_ok(SUCCESS))

    response = await adapter.complete(_request())

    assert response.text == "Your order shipped yesterday."
    assert response.tool_calls == []
    assert response.usage == Usage(
        input_tokens=12, output_tokens=34, cache_read_tokens=9000, cache_write_tokens=1500
    )
    assert response.model == "claude-sonnet-test"
    assert response.provider == PROVIDER == "claude_cli"
    assert response.stop_reason == "end_turn"


async def test_model_falls_back_to_explicit_model_or_subscription_label() -> None:
    payload = {k: v for k, v in SUCCESS.items() if k != "modelUsage"}
    with_model = await ClaudeCliAdapter(
        _settings(primary_model="sonnet"), runner=_ok(payload)
    ).complete(_request())
    without = await ClaudeCliAdapter(_settings(), runner=_ok(payload)).complete(_request())
    reported = await ClaudeCliAdapter(
        _settings(), runner=_ok({**payload, "model": "claude-x"})
    ).complete(_request())

    assert with_model.model == "sonnet"
    assert without.model == "claude-subscription"
    assert reported.model == "claude-x"


async def test_response_schema_is_passed_and_structured_output_is_text() -> None:
    schema = {"type": "object", "properties": {"priority": {"type": "string"}}}
    payload = {**SUCCESS, "result": "", "structured_output": {"priority": "high"}}
    runner = _ok(payload)

    response = await ClaudeCliAdapter(_settings(), runner=runner).complete(
        _request(response_schema=schema)
    )

    assert json.loads(_flag(runner.calls[0]["argv"], "--json-schema")) == schema
    assert json.loads(response.text) == {"priority": "high"}


async def test_response_schema_without_structured_output_keeps_result_text() -> None:
    payload = {**SUCCESS, "result": '{"priority": "low"}'}

    response = await ClaudeCliAdapter(_settings(), runner=_ok(payload)).complete(
        _request(response_schema={"type": "object"})
    )

    assert json.loads(response.text) == {"priority": "low"}


async def test_cli_error_result_raises_non_recoverable_without_raw_text() -> None:
    payload = {
        **SUCCESS,
        "subtype": "error_during_execution",
        "is_error": True,
        "result": "account user@example.com failed",
    }

    with pytest.raises(LLMError) as info:
        await ClaudeCliAdapter(_settings(), runner=_ok(payload, returncode=1)).complete(_request())

    assert info.value.recoverable is False
    assert "error_during_execution" in info.value.message
    assert "user@example.com" not in info.value.message


@pytest.mark.parametrize(
    "result_text",
    ["API Error: 429 rate_limit_error", "API Error: 529 Overloaded", "Usage limit reached"],
)
async def test_rate_limit_or_overloaded_is_recoverable(result_text: str) -> None:
    payload = {**SUCCESS, "is_error": True, "result": result_text}

    with pytest.raises(LLMError) as info:
        await ClaudeCliAdapter(_settings(), runner=_ok(payload, returncode=1)).complete(_request())

    assert info.value.recoverable is True
    assert result_text not in info.value.message


async def test_non_json_output_raises_with_returncode_only() -> None:
    runner = FakeRunner(
        CliResult(returncode=2, stdout="Invalid API key token=abc", stderr="secret stuff")
    )

    with pytest.raises(LLMError) as info:
        await ClaudeCliAdapter(_settings(), runner=runner).complete(_request())

    assert "exit 2" in info.value.message
    assert "abc" not in info.value.message
    assert "secret" not in info.value.message
    assert info.value.recoverable is False


async def test_timeout_and_missing_binary_propagate() -> None:
    timeout = FakeRunner(LLMTimeoutError("claude CLI timed out"))
    missing = FakeRunner(LLMError("claude CLI not found", recoverable=False))

    with pytest.raises(LLMTimeoutError):
        await ClaudeCliAdapter(_settings(), runner=timeout).complete(_request())
    with pytest.raises(LLMError, match="not found"):
        await ClaudeCliAdapter(_settings(), runner=missing).complete(_request())


async def test_tools_are_emulated_with_a_json_schema_answer() -> None:
    # Phase 6: tool calling is emulated (details in test_claude_cli_tools.py).
    answer = {"text": "", "tool_calls": [{"name": "get_order", "arguments": {"order_id": 1}}]}
    runner = _ok({**SUCCESS, "result": "", "structured_output": answer})
    request = _request(tools=[ToolSpec("get_order", "Get an order", {"type": "object"})])

    response = await ClaudeCliAdapter(_settings(), runner=runner).complete(request)

    assert "--json-schema" in runner.calls[0]["argv"]
    assert _flag(runner.calls[0]["argv"], "--tools") == ""
    assert [call.name for call in response.tool_calls] == ["get_order"]


def test_stream_is_not_implemented() -> None:
    adapter = ClaudeCliAdapter(_settings(), runner=_ok(SUCCESS))

    with pytest.raises(NotImplementedError):
        adapter.stream(_request())


def test_adapter_satisfies_llm_client_protocol() -> None:
    assert isinstance(ClaudeCliAdapter(_settings(), runner=_ok(SUCCESS)), LLMClient)


def test_render_conversation_single_user_turn_is_verbatim() -> None:
    assert render_conversation([ChatMessage(role="user", content="Hi")]) == "Hi"


def test_render_conversation_multi_turn_uses_labeled_blocks() -> None:
    messages = [
        ChatMessage(role="user", content="Hi"),
        ChatMessage(role="assistant", content="Hello!"),
        ChatMessage(role="tool", content='{"ok": true}', tool_call_id="call_1"),
        ChatMessage(role="user", content="Where is my order?"),
    ]

    assert render_conversation(messages) == (
        "User: Hi\n\n"
        "Assistant: Hello!\n\n"
        'Tool result (call_1): {"ok": true}\n\n'
        "User: Where is my order?"
    )
