"""`CodexCliAdapter` with an injected fake runner: no process and no real CLI call."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from api.config import Settings
from api.llm import codex_cli_adapter
from api.llm.cli_runner import CliResult
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
from api.llm.codex_cli_adapter import PROVIDER, CodexCliAdapter

SUCCESS_EVENTS: list[dict[str, Any]] = [
    {"type": "thread.started", "thread_id": "0199a213-81c0-7800-8aa1-bbab2a035a53"},
    {"type": "turn.started"},
    {
        "type": "item.completed",
        "item": {"id": "item_0", "type": "reasoning", "text": "**Looking up the order**"},
    },
    {
        "type": "item.completed",
        "item": {"id": "item_1", "type": "agent_message", "text": "Draft answer."},
    },
    {
        "type": "item.completed",
        "item": {"id": "item_2", "type": "agent_message", "text": "Your order shipped."},
    },
    {
        "type": "turn.completed",
        "usage": {"input_tokens": 2400, "cached_input_tokens": 2048, "output_tokens": 57},
    },
]


def _jsonl(events: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


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
        schema: Any = None
        if "--output-schema" in argv:
            schema = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text())
        self.calls.append(
            {
                "argv": list(argv),
                "stdin": stdin,
                "timeout": timeout,
                "cwd": cwd,
                "cwd_listing": sorted(os.listdir(cwd)),
                "schema": schema,
                "env_remove": env_remove,
            }
        )
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _ok(events: list[dict[str, Any]], returncode: int = 0) -> FakeRunner:
    return FakeRunner(CliResult(returncode=returncode, stdout=_jsonl(events), stderr=""))


def _settings(**values: Any) -> Settings:
    values.setdefault("codex_cli_path", "/opt/bin/codex")
    values.setdefault("cli_timeout_seconds", 42.0)
    return Settings(_env_file=None, llm_provider="codex_cli", **values)  # type: ignore[call-arg]


def _request(**values: Any) -> LLMRequest:
    values.setdefault("system", [SystemBlock("You are Atlas."), SystemBlock("Be brief.")])
    values.setdefault(
        "messages", [ChatMessage(role="user", content="Where is my order SECRET-PROMPT?")]
    )
    return LLMRequest(**values)


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


async def test_argv_reads_prompt_from_stdin_and_never_holds_it() -> None:
    runner = _ok(SUCCESS_EVENTS)

    await CodexCliAdapter(_settings(), runner=runner).complete(_request())

    call = runner.calls[0]
    argv = call["argv"]
    assert argv[:2] == ["/opt/bin/codex", "exec"]
    assert argv[-1] == "-"
    for flag in (
        "--json",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
    ):
        assert flag in argv
    assert _flag(argv, "--sandbox") == "read-only"
    assert _flag(argv, "--color") == "never"
    assert _flag(argv, "--cd") == call["cwd"]
    assert "-m" not in argv
    assert "--output-schema" not in argv
    assert all("SECRET-PROMPT" not in arg for arg in argv)
    assert "SECRET-PROMPT" in call["stdin"]
    assert call["timeout"] == 42.0
    assert call["cwd_listing"] == []
    assert not os.path.exists(call["cwd"])
    assert {"OPENAI_API_KEY", "CODEX_API_KEY"} <= call["env_remove"]


# Requested off. NOT a claim that each one is effective: in codex-cli 0.165 `unified_exec`
# stays on even when disabled; the real command-execution barrier is `shell_tool`.
REQUESTED_OFF = {
    "shell_tool",
    "unified_exec",
    "unified_exec_tty",
    "shell_snapshot",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "in_app_browser",
    "in_app_local_automation",
    "computer_use",
    "apps",
    "code_mode_host",
    "image_generation",
    "view_image",
    "multi_agent",
    "plugins",
    "remote_plugin",
    "hooks",
    "skill_search",
    "skill_mcp_dependency_install",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "sleep_tool",
    "goals",
    "workspace_dependencies",
    "daemon_auto_start",
}


def _disabled(argv: list[str]) -> set[str]:
    return {argv[i + 1] for i, arg in enumerate(argv) if arg == "--disable"}


def _config_overrides(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg in ("-c", "--config")]


async def test_argv_requests_tool_features_off() -> None:
    runner = _ok(SUCCESS_EVENTS)

    await CodexCliAdapter(_settings(), runner=runner).complete(_request())

    argv = runner.calls[0]["argv"]
    assert REQUESTED_OFF <= _disabled(argv)
    assert "--enable" not in argv
    # Every `--disable` is followed by a feature name, never by another flag.
    assert all(not name.startswith("-") for name in _disabled(argv))


async def test_shell_tool_is_the_exec_barrier_and_no_daemon_is_started() -> None:
    runner = _ok(SUCCESS_EVENTS)

    await CodexCliAdapter(_settings(), runner=runner).complete(_request())

    disabled = _disabled(runner.calls[0]["argv"])
    assert "shell_tool" in disabled
    assert "daemon_auto_start" in disabled


def test_docstring_does_not_claim_every_tool_feature_is_off() -> None:
    doc = codex_cli_adapter.__doc__ or ""

    assert "every tool-capable feature is turned off" not in doc
    assert "`shell_tool`" in doc
    assert "unified_exec" in doc and "cannot be disabled" in doc


async def test_argv_disables_web_search() -> None:
    runner = _ok(SUCCESS_EVENTS)

    await CodexCliAdapter(_settings(), runner=runner).complete(_request())

    assert 'web_search="disabled"' in _config_overrides(runner.calls[0]["argv"])


async def test_tool_flags_come_before_the_stdin_marker() -> None:
    runner = _ok(SUCCESS_EVENTS)

    await CodexCliAdapter(_settings(), runner=runner).complete(
        _request(response_schema={"type": "object"})
    )

    argv = runner.calls[0]["argv"]
    assert argv[-1] == "-"
    assert argv.count("-") == 1


async def test_system_prompt_goes_on_top_of_the_stdin_prompt() -> None:
    runner = _ok(SUCCESS_EVENTS)

    await CodexCliAdapter(_settings(), runner=runner).complete(_request())

    assert runner.calls[0]["stdin"] == (
        "<system>\nYou are Atlas.\n\nBe brief.\n</system>\n\nWhere is my order SECRET-PROMPT?"
    )


async def test_model_flag_from_argument_or_settings() -> None:
    runner = _ok(SUCCESS_EVENTS)
    await CodexCliAdapter(_settings(primary_model="gpt-x"), runner=runner).complete(_request())
    await CodexCliAdapter(_settings(), model="gpt-y", runner=runner).complete(_request())

    assert _flag(runner.calls[0]["argv"], "-m") == "gpt-x"
    assert _flag(runner.calls[1]["argv"], "-m") == "gpt-y"


async def test_success_parses_last_agent_message_and_usage() -> None:
    response = await CodexCliAdapter(_settings(), runner=_ok(SUCCESS_EVENTS)).complete(_request())

    assert response.text == "Your order shipped."
    assert response.tool_calls == []
    assert response.usage == Usage(input_tokens=2400, output_tokens=57, cache_read_tokens=2048)
    assert response.model == "codex-subscription"
    assert response.provider == PROVIDER == "codex_cli"
    assert response.stop_reason == "end_turn"


async def test_model_is_the_configured_one() -> None:
    response = await CodexCliAdapter(
        _settings(primary_model="gpt-x"), runner=_ok(SUCCESS_EVENTS)
    ).complete(_request())

    assert response.model == "gpt-x"


async def test_non_json_lines_are_ignored() -> None:
    runner = FakeRunner(
        CliResult(
            returncode=0,
            stdout="Reading prompt from stdin...\n" + _jsonl(SUCCESS_EVENTS),
            stderr="",
        )
    )

    response = await CodexCliAdapter(_settings(), runner=runner).complete(_request())

    assert response.text == "Your order shipped."


async def test_response_schema_goes_in_a_temp_file_outside_cwd() -> None:
    schema = {
        "type": "object",
        "properties": {"priority": {"type": "string"}},
        "required": ["priority"],
        "additionalProperties": False,
    }
    events = [*SUCCESS_EVENTS[:3], SUCCESS_EVENTS[5]]
    events.insert(
        3,
        {
            "type": "item.completed",
            "item": {"id": "item_1", "type": "agent_message", "text": '{"priority":"high"}'},
        },
    )
    runner = _ok(events)

    response = await CodexCliAdapter(_settings(), runner=runner).complete(
        _request(response_schema=schema)
    )

    call = runner.calls[0]
    schema_path = _flag(call["argv"], "--output-schema")
    assert call["schema"] == schema
    assert not schema_path.startswith(call["cwd"] + os.sep)
    assert not os.path.exists(schema_path)
    assert json.loads(response.text) == {"priority": "high"}


@pytest.mark.parametrize(
    ("events", "recoverable"),
    [
        ([{"type": "error", "message": "unexpected status 401 for user@example.com"}], False),
        (
            [
                {"type": "turn.started"},
                {"type": "turn.failed", "error": {"message": "429 Too Many Requests: rate limit"}},
            ],
            True,
        ),
        ([{"type": "error", "message": "stream error: overloaded, retrying"}], True),
    ],
)
async def test_error_events_raise_without_raw_text(
    events: list[dict[str, Any]], recoverable: bool
) -> None:
    with pytest.raises(LLMError) as info:
        await CodexCliAdapter(_settings(), runner=_ok(events, returncode=1)).complete(_request())

    assert info.value.recoverable is recoverable
    assert "user@example.com" not in info.value.message
    assert "Too Many" not in info.value.message


async def test_missing_agent_message_or_bad_exit_raises() -> None:
    no_message = [SUCCESS_EVENTS[0], SUCCESS_EVENTS[5]]
    garbage = FakeRunner(CliResult(returncode=2, stdout="token=abc", stderr="secret"))

    with pytest.raises(LLMError, match="no agent message"):
        await CodexCliAdapter(_settings(), runner=_ok(no_message)).complete(_request())
    with pytest.raises(LLMError, match="exit 2") as info:
        await CodexCliAdapter(_settings(), runner=garbage).complete(_request())
    assert "abc" not in info.value.message
    assert "secret" not in info.value.message


async def test_timeout_and_missing_binary_propagate() -> None:
    with pytest.raises(LLMTimeoutError):
        await CodexCliAdapter(
            _settings(), runner=FakeRunner(LLMTimeoutError("codex CLI timed out"))
        ).complete(_request())
    with pytest.raises(LLMError, match="not found"):
        await CodexCliAdapter(
            _settings(), runner=FakeRunner(LLMError("codex CLI not found", recoverable=False))
        ).complete(_request())


async def test_tools_are_rejected_before_running_the_cli() -> None:
    runner = _ok(SUCCESS_EVENTS)
    request = _request(tools=[ToolSpec("get_order", "Get an order", {"type": "object"})])

    with pytest.raises(LLMError, match="tool calling is not supported") as info:
        await CodexCliAdapter(_settings(), runner=runner).complete(request)

    assert info.value.recoverable is False
    assert runner.calls == []


def test_stream_is_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        CodexCliAdapter(_settings(), runner=_ok(SUCCESS_EVENTS)).stream(_request())


def test_adapter_satisfies_llm_client_protocol() -> None:
    assert isinstance(CodexCliAdapter(_settings(), runner=_ok(SUCCESS_EVENTS)), LLMClient)
