"""`LLMClient` over the logged-in Claude Code CLI (`claude -p`, LAC-43).

Local manual testing only: it uses the user's Claude subscription, so there is no API key and
no real cost (`InstrumentedLLMClient` logs `pricing_error="subscription"`).

Command: `claude -p --output-format json --system-prompt <system> --tools ""` plus flags that
keep the user's environment out of the call:
- `--setting-sources ""`: load no user/project/local settings (hooks, permissions, plugins);
- `--strict-mcp-config`: no MCP server (none is passed with `--mcp-config`);
- `--no-session-persistence`: the call is not saved as a resumable session;
- `--disable-slash-commands`: no skills or custom commands.
`--bare` is not used: it ignores the OAuth login, which is the whole point of this provider.
`--model` is added when a model is set and `--json-schema` when the request has a schema.

The conversation is sent through stdin as one text prompt (`render_conversation`): a lone user
turn verbatim, otherwise `User:` / `Assistant:` / `Tool result (<id>):` blocks separated by a
blank line, the last block being the current turn. `ANTHROPIC_API_KEY` is removed from the
child environment so the CLI cannot silently bill an API key instead of the subscription.
"""

import json
import tempfile
from collections.abc import AsyncIterator
from typing import Any

from api.config import Settings
from api.llm.cli_runner import (
    STREAM_NOT_SUPPORTED,
    CliResult,
    CliRunner,
    check_request,
    failed_exit,
    int_field,
    looks_recoverable,
    malformed_output,
    render_conversation,
    run_cli,
    safe_label,
    system_text,
)
from api.llm.client import LLMError, LLMRequest, LLMResponse, LLMStreamEvent, Usage

PROVIDER = "claude_cli"
DEFAULT_MODEL = "claude-subscription"
CLI_NAME = "claude"
ENV_REMOVE = frozenset({"ANTHROPIC_API_KEY"})
_ISOLATION_FLAGS = (
    "--setting-sources",
    "",
    "--strict-mcp-config",
    "--no-session-persistence",
    "--disable-slash-commands",
)


def _is_recoverable_status(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and (value == 429 or value >= 500)


class ClaudeCliAdapter:
    """`LLMClient` that shells out (without a shell) to `claude -p`."""

    def __init__(
        self, settings: Settings, model: str | None = None, runner: CliRunner = run_cli
    ) -> None:
        self.cli_path = settings.claude_cli_path
        self.timeout_seconds = settings.cli_timeout_seconds
        self.model: str | None = model or settings.primary_model or None
        self._runner = runner

    def _argv(self, request: LLMRequest) -> list[str]:
        argv = [
            self.cli_path,
            "-p",
            "--output-format",
            "json",
            "--system-prompt",
            system_text(request.system),
            "--tools",
            "",
            *_ISOLATION_FLAGS,
        ]
        if self.model:
            argv += ["--model", self.model]
        if request.response_schema is not None:
            argv += ["--json-schema", json.dumps(request.response_schema)]
        return argv

    async def complete(self, request: LLMRequest) -> LLMResponse:
        check_request(request)
        argv = self._argv(request)
        prompt = render_conversation(request.messages)
        with tempfile.TemporaryDirectory(prefix="atlas-claude-cli-") as cwd:
            result = await self._runner(
                argv, prompt, self.timeout_seconds, cwd, env_remove=ENV_REMOVE
            )
        return self._parse(result, structured=request.response_schema is not None)

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        raise NotImplementedError(STREAM_NOT_SUPPORTED)

    def _parse(self, result: CliResult, structured: bool) -> LLMResponse:
        try:
            data = json.loads(result.stdout)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            if result.returncode != 0:
                raise failed_exit(CLI_NAME, result.returncode)
            raise malformed_output(CLI_NAME, result.returncode)
        subtype = data.get("subtype")
        if data.get("is_error") is True or (subtype is not None and subtype != "success"):
            raise self._error(data, result.returncode)
        if result.returncode != 0:
            raise failed_exit(CLI_NAME, result.returncode)
        try:
            return self._response(data, structured)
        except ValueError:
            raise malformed_output(CLI_NAME, result.returncode) from None

    def _error(self, data: dict[str, Any], returncode: int) -> LLMError:
        subtype = data.get("subtype")
        label = safe_label(subtype) if subtype != "success" else None
        detail = f" ({label})" if label else ""
        recoverable = _is_recoverable_status(data.get("api_error_status")) or looks_recoverable(
            [data.get("result"), subtype]
        )
        return LLMError(
            f"{CLI_NAME} CLI reported an error{detail} (exit {returncode})",
            recoverable=recoverable,
        )

    def _response(self, data: dict[str, Any], structured: bool) -> LLMResponse:
        structured_output = data.get("structured_output")
        result = data.get("result")
        if structured and structured_output is not None:
            text = json.dumps(structured_output)
        elif isinstance(result, str):
            text = result
        else:
            raise ValueError("malformed field: result")
        usage = data.get("usage")
        if not isinstance(usage, dict):
            raise ValueError("malformed field: usage")
        stop_reason = data.get("stop_reason")
        return LLMResponse(
            text=text,
            tool_calls=[],
            usage=Usage(
                input_tokens=int_field(usage, "input_tokens"),
                output_tokens=int_field(usage, "output_tokens"),
                cache_read_tokens=int_field(usage, "cache_read_input_tokens"),
                cache_write_tokens=int_field(usage, "cache_creation_input_tokens"),
            ),
            model=self._reported_model(data),
            provider=PROVIDER,
            stop_reason=stop_reason if isinstance(stop_reason, str) else "end_turn",
        )

    def _reported_model(self, data: dict[str, Any]) -> str:
        model = data.get("model")
        if isinstance(model, str) and model:
            return model
        model_usage = data.get("modelUsage")
        if isinstance(model_usage, dict) and len(model_usage) == 1:
            (only,) = model_usage
            if isinstance(only, str) and only:
                return only
        return self.model or DEFAULT_MODEL
