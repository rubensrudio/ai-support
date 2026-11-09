"""Subprocess runner and shared helpers for the subscription CLI adapters (LAC-43).

`run_cli` runs a logged-in CLI (`claude`, `codex`) without a shell: the argv is passed as is,
the prompt goes through stdin (never the argv, so it does not show up in `ps`), and `cwd` is an
empty temporary directory so the CLI cannot read the repository or load its `CLAUDE.md` /
`AGENTS.md`. The environment is inherited (the CLI needs `HOME` for its login), minus the names
in `env_remove`.

The child starts in its own session/process group (`start_new_session=True`). On timeout or
cancellation the whole group gets `SIGKILL` and the child is reaped, and after a normal exit
any process left in the group is killed too (only if the group still exists, to avoid a reused
id), so CLI grandchildren never outlive the call.

Error messages never carry the raw stdout/stderr, which may hold account data: at most the CLI
name, the exit code and a short sanitized label.
"""

import asyncio
import contextlib
import os
import re
import signal
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from api.llm.client import ChatMessage, LLMError, LLMRequest, LLMTimeoutError, SystemBlock

TOOLS_NOT_SUPPORTED = "tool calling is not supported by subscription CLI providers"
STREAM_NOT_SUPPORTED = "streaming is not supported by subscription CLI providers yet (phase 9)"
_LABEL = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_RECOVERABLE_HINTS = re.compile(
    r"rate[ _-]?limit|too many requests|\b429\b|overloaded|\b529\b|usage limit", re.IGNORECASE
)


@dataclass(frozen=True)
class CliResult:
    returncode: int
    stdout: str
    stderr: str


class CliRunner(Protocol):
    async def __call__(
        self,
        argv: list[str],
        stdin: str,
        timeout: float,
        cwd: str,
        *,
        env_remove: frozenset[str] = ...,
    ) -> CliResult: ...


def cli_name(argv: list[str]) -> str:
    return os.path.basename(argv[0]) if argv else "unknown"


def _child_env(env_remove: frozenset[str]) -> dict[str, str] | None:
    if not env_remove:
        return None
    return {key: value for key, value in os.environ.items() if key not in env_remove}


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _kill_group(process: asyncio.subprocess.Process) -> None:
    """SIGKILL every process in the child's group (its pid is the group id).

    While the leader is alive (not reaped) its pid cannot be reused, so the group is ours. Once
    it was reaped, the group is only signalled if it still has members: an empty group's id
    could be reused by an unrelated process.
    """
    pgid = process.pid
    if process.returncode is not None and not _group_exists(pgid):
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


async def _kill(process: asyncio.subprocess.Process) -> None:
    _kill_group(process)
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    await process.wait()


async def run_cli(
    argv: list[str],
    stdin: str,
    timeout: float,
    cwd: str,
    *,
    env_remove: frozenset[str] = frozenset(),
) -> CliResult:
    """Run `argv` with `stdin` and return its exit code and decoded output.

    Raises `LLMTimeoutError` after `timeout` seconds (the process is killed and reaped) and a
    non-recoverable `LLMError` when the binary does not exist or cannot be executed.
    """
    name = cli_name(argv)
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=_child_env(env_remove),
            start_new_session=True,
        )
    except FileNotFoundError:
        raise LLMError(
            f"{name} CLI not found: install it or set its path in the settings",
            recoverable=False,
        ) from None
    except PermissionError:
        raise LLMError(f"{name} CLI is not executable", recoverable=False) from None
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await process.communicate(stdin.encode("utf-8"))
    except TimeoutError:
        await _kill(process)
        raise LLMTimeoutError(f"{name} CLI timed out after {timeout:g}s") from None
    except BaseException:
        # Cancellation (or any other failure) must not leave the CLI running.
        await _kill(process)
        raise
    # The child exited; anything it left behind in its group dies with the call.
    _kill_group(process)
    return CliResult(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


def check_request(request: LLMRequest) -> None:
    """Reject what the subscription CLIs cannot do (tool calling is emulated in phase 6)."""
    if request.tools:
        raise LLMError(TOOLS_NOT_SUPPORTED, recoverable=False)


def system_text(system: list[SystemBlock]) -> str:
    """System blocks joined by a blank line (`cache_breakpoint` has no meaning for a CLI)."""
    return "\n\n".join(block.text for block in system if block.text)


def _label(message: ChatMessage) -> str:
    if message.role == "assistant":
        return "Assistant"
    if message.role == "tool":
        return f"Tool result ({message.tool_call_id})" if message.tool_call_id else "Tool result"
    return "User"


def render_conversation(messages: list[ChatMessage]) -> str:
    """Serialize the conversation into the single text prompt a CLI accepts.

    A lone user turn is sent verbatim. Otherwise each message becomes a block
    `User: ...`, `Assistant: ...` or `Tool result (<id>): ...`, in order, separated by a blank
    line; the last block is the current turn.
    """
    if len(messages) == 1 and messages[0].role == "user":
        return messages[0].content
    return "\n\n".join(f"{_label(message)}: {message.content}" for message in messages)


def safe_label(value: object) -> str | None:
    """`value` if it is a short identifier-like string (e.g. a subtype), else `None`."""
    return value if isinstance(value, str) and _LABEL.match(value) else None


def looks_recoverable(texts: Iterable[object]) -> bool:
    """Rate limit / overloaded hints in the CLI's own error text (never echoed to callers)."""
    return any(isinstance(text, str) and _RECOVERABLE_HINTS.search(text) for text in texts)


def int_field(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key, 0)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"malformed usage field: {key}")
    return value


def failed_exit(name: str, returncode: int) -> LLMError:
    return LLMError(f"{name} CLI failed (exit {returncode})", recoverable=False)


def malformed_output(name: str, returncode: int) -> LLMError:
    return LLMError(f"{name} CLI returned malformed output (exit {returncode})", recoverable=False)
