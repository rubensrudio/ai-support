"""`LLMClient` over the logged-in Codex CLI (`codex exec`, LAC-43).

Local manual testing only: it uses the user's ChatGPT/Codex subscription, so there is no API key
and no real cost (`InstrumentedLLMClient` logs `pricing_error="subscription"`).

Command: `codex exec --json --skip-git-repo-check --ephemeral --ignore-user-config --ignore-rules
--sandbox read-only --color never --cd <empty dir> --disable <feature>... -c web_search="disabled"
[-m <model>] [--output-schema <file>] -`:
- `-` reads the prompt from stdin, so it never shows up in the argv;
- `--ephemeral`: no session files; `--ignore-user-config` / `--ignore-rules`: no user
  `config.toml` (MCP servers, profiles) and no execpolicy rules. Auth still comes from
  `CODEX_HOME`, so the subscription login keeps working;
- tool features are requested off with a repeated `--disable` (`TOOL_FEATURES`, names from
  `codex features list`: shell / exec, browser, computer use, apps and plugins, code mode, image
  tools, sub-agents, hooks, skills/MCP installs, and `daemon_auto_start` so no detached app
  server escapes the process-group kill) and web search with `-c web_search="disabled"`
  (`codex exec` has no dedicated flag for it). Not every flag is effective: in codex-cli 0.165
  `unified_exec` cannot be disabled (it stays on even with `-c features.unified_exec=false`;
  passing it is harmless). The command-execution barrier is `shell_tool` being off. The model
  is meant as a plain text completer that does not run commands or read files.
  `--sandbox read-only` only blocks writes, so it is a second layer, not the protection against
  reading `~/.codex/auth.json`, `~/.ssh` or the repository by absolute path;
- `--cd` and the process cwd are an empty temporary directory, so the repository and its
  `AGENTS.md` are not the agent's workspace.
The schema file for `--output-schema` lives next to (not inside) that directory.

Codex has no system prompt flag: the system blocks go on top of the prompt as
`<system>\\n...\\n</system>`, followed by a blank line and the conversation rendered by
`render_conversation`. `OPENAI_API_KEY` / `CODEX_API_KEY` are removed from the child
environment so the CLI cannot silently bill an API key instead of the subscription.

Output (JSONL): the text is the last `item.completed` whose item is an `agent_message`, usage
comes from `turn.completed` (`cached_input_tokens` -> `cache_read_tokens`). A `turn.failed`, or
an `error` event without a completed turn (Codex also emits `error` for reconnect attempts that
later succeed), raises `LLMError`.
"""

import json
import os
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
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
    system_text,
)
from api.llm.client import LLMError, LLMRequest, LLMResponse, LLMStreamEvent, Usage

PROVIDER = "codex_cli"
DEFAULT_MODEL = "codex-subscription"
CLI_NAME = "codex"
ENV_REMOVE = frozenset({"OPENAI_API_KEY", "CODEX_API_KEY"})
_AGENT_MESSAGE_TYPES = frozenset({"agent_message", "assistant_message"})
TOOL_FEATURES: tuple[str, ...] = (
    # shell and command execution
    "shell_tool",
    "unified_exec",
    "unified_exec_tty",
    "shell_snapshot",
    "code_mode_host",
    # browser, computer use and local automation
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "in_app_browser",
    "in_app_local_automation",
    "computer_use",
    # apps, plugins, MCP and skills
    "apps",
    "plugins",
    "remote_plugin",
    "skill_search",
    "skill_mcp_dependency_install",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "workspace_dependencies",
    "hooks",
    # other model-callable tools
    "image_generation",
    "view_image",
    "multi_agent",
    "sleep_tool",
    "goals",
    # background processes that would outlive the call
    "daemon_auto_start",
)
"""Codex features (all `stable` and on by default in codex-cli 0.165) that give the model a
tool, run something, or reach outside the prompt. Verified with `codex features list`.
Requested off, not guaranteed off: `unified_exec` stays on in 0.165; `shell_tool` is the
effective exec barrier."""
WEB_SEARCH_OFF = 'web_search="disabled"'


def build_prompt(request: LLMRequest) -> str:
    conversation = render_conversation(request.messages)
    system = system_text(request.system)
    if not system:
        return conversation
    return f"<system>\n{system}\n</system>\n\n{conversation}"


@dataclass
class _Outcome:
    text: str | None = None
    usage: dict[str, Any] | None = None
    completed: bool = False
    failed: bool = False
    error_texts: list[object] = field(default_factory=list)


def _events(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _read(events: list[dict[str, Any]]) -> _Outcome:
    outcome = _Outcome()
    for event in events:
        kind = event.get("type")
        if kind == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") in _AGENT_MESSAGE_TYPES:
                text = item.get("text")
                if isinstance(text, str):
                    outcome.text = text
        elif kind == "turn.completed":
            outcome.completed = True
            usage = event.get("usage")
            outcome.usage = usage if isinstance(usage, dict) else None
        elif kind == "turn.failed":
            outcome.failed = True
            error = event.get("error")
            outcome.error_texts.append(error.get("message") if isinstance(error, dict) else None)
        elif kind == "error":
            outcome.error_texts.append(event.get("message"))
    return outcome


class CodexCliAdapter:
    """`LLMClient` that shells out (without a shell) to `codex exec`."""

    def __init__(
        self, settings: Settings, model: str | None = None, runner: CliRunner = run_cli
    ) -> None:
        self.cli_path = settings.codex_cli_path
        self.timeout_seconds = settings.cli_timeout_seconds
        self.model: str | None = model or settings.primary_model or None
        self._runner = runner

    def _argv(self, cwd: str, schema_path: str | None) -> list[str]:
        argv = [
            self.cli_path,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "--cd",
            cwd,
        ]
        for feature in TOOL_FEATURES:
            argv += ["--disable", feature]
        argv += ["-c", WEB_SEARCH_OFF]
        if self.model:
            argv += ["-m", self.model]
        if schema_path is not None:
            argv += ["--output-schema", schema_path]
        argv.append("-")
        return argv

    async def complete(self, request: LLMRequest) -> LLMResponse:
        check_request(request)
        prompt = build_prompt(request)
        with tempfile.TemporaryDirectory(prefix="atlas-codex-cli-") as root:
            cwd = os.path.join(root, "work")
            os.mkdir(cwd)
            schema_path: str | None = None
            if request.response_schema is not None:
                schema_path = os.path.join(root, "schema.json")
                with open(schema_path, "w", encoding="utf-8") as handle:
                    json.dump(request.response_schema, handle)
            result = await self._runner(
                self._argv(cwd, schema_path),
                prompt,
                self.timeout_seconds,
                cwd,
                env_remove=ENV_REMOVE,
            )
        return self._parse(result)

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        raise NotImplementedError(STREAM_NOT_SUPPORTED)

    def _parse(self, result: CliResult) -> LLMResponse:
        events = _events(result.stdout)
        if not events and result.returncode != 0:
            raise failed_exit(CLI_NAME, result.returncode)
        outcome = _read(events)
        if outcome.failed or (outcome.error_texts and not outcome.completed):
            raise LLMError(
                f"{CLI_NAME} CLI reported an error (exit {result.returncode})",
                recoverable=looks_recoverable(outcome.error_texts),
            )
        if result.returncode != 0:
            raise failed_exit(CLI_NAME, result.returncode)
        if outcome.text is None:
            raise LLMError(
                f"{CLI_NAME} CLI returned no agent message (exit {result.returncode})",
                recoverable=False,
            )
        usage = outcome.usage or {}
        try:
            parsed_usage = Usage(
                input_tokens=int_field(usage, "input_tokens"),
                output_tokens=int_field(usage, "output_tokens"),
                cache_read_tokens=int_field(usage, "cached_input_tokens"),
            )
        except ValueError:
            raise malformed_output(CLI_NAME, result.returncode) from None
        return LLMResponse(
            text=outcome.text,
            tool_calls=[],
            usage=parsed_usage,
            model=self.model or DEFAULT_MODEL,
            provider=PROVIDER,
            stop_reason="end_turn",
        )
