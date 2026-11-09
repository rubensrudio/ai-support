"""`run_cli` against a tiny Python script run with `sys.executable` (no LLM CLI is ever run)."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from api.llm import cli_runner
from api.llm.cli_runner import CliResult, run_cli
from api.llm.client import LLMError, LLMTimeoutError

ECHO_SCRIPT = """
import os, sys
data = sys.stdin.read()
sys.stdout.write("out:" + data.upper())
sys.stderr.write("err:" + os.getcwd())
sys.exit(int(os.environ.get("EXIT_CODE", "0")))
"""

SLEEP_SCRIPT = """
import sys, time
sys.stdout.write("started")
sys.stdout.flush()
time.sleep(30)
"""


FAMILY_SCRIPT = """
import json, os, subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
info = {"child": os.getpid(), "pgid": os.getpgid(0), "grandchild": grandchild.pid}
with open("pids.json.tmp", "w") as handle:
    json.dump(info, handle)
os.replace("pids.json.tmp", "pids.json")
time.sleep(60)
"""


async def _wait_for_pids(cwd: Path) -> dict[str, int]:
    path = cwd / "pids.json"
    for _ in range(200):
        if path.exists():
            data: dict[str, int] = json.loads(path.read_text())
            return data
        await asyncio.sleep(0.05)
    raise AssertionError("child never wrote its pids")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


async def _gone(check: object, target: int) -> bool:
    """An orphaned grandchild is reaped by init asynchronously, so poll for a while."""
    assert callable(check)
    for _ in range(100):
        if not check(target):
            return True
        await asyncio.sleep(0.05)
    return False


async def test_run_cli_starts_child_in_its_own_process_group(tmp_path: Path) -> None:
    task = asyncio.create_task(
        run_cli([sys.executable, "-c", FAMILY_SCRIPT], "", 30, str(tmp_path))
    )
    pids = await _wait_for_pids(tmp_path)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert pids["pgid"] == pids["child"]
    assert pids["pgid"] != os.getpgid(0)


async def test_run_cli_cancel_kills_and_reaps_the_whole_group(tmp_path: Path) -> None:
    task = asyncio.create_task(
        run_cli([sys.executable, "-c", FAMILY_SCRIPT], "", 30, str(tmp_path))
    )
    pids = await _wait_for_pids(tmp_path)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The direct child was killed and reaped (no zombie left behind) ...
    assert not _alive(pids["child"])
    # ... and so was its grandchild: nothing in the group outlives the call.
    assert await _gone(_alive, pids["grandchild"])
    assert await _gone(_group_alive, pids["pgid"])


async def test_run_cli_timeout_kills_the_whole_group(tmp_path: Path) -> None:
    task = asyncio.create_task(run_cli([sys.executable, "-c", FAMILY_SCRIPT], "", 5, str(tmp_path)))
    pids = await _wait_for_pids(tmp_path)

    with pytest.raises(LLMTimeoutError):
        await task

    assert not _alive(pids["child"])
    assert await _gone(_alive, pids["grandchild"])
    assert await _gone(_group_alive, pids["pgid"])


LEFTOVER_SCRIPT = """
import json, subprocess, sys
grandchild = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print(json.dumps({"grandchild": grandchild.pid}))
"""


def _record_killpg(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    calls: list[tuple[int, int]] = []
    real_killpg = os.killpg

    def recording_killpg(pgid: int, sig: int) -> None:
        calls.append((pgid, sig))
        real_killpg(pgid, sig)

    monkeypatch.setattr(cli_runner.os, "killpg", recording_killpg)
    return calls


async def test_run_cli_normal_exit_kills_leftovers_in_the_group(tmp_path: Path) -> None:
    result = await run_cli([sys.executable, "-c", LEFTOVER_SCRIPT], "", 10, str(tmp_path))

    grandchild = json.loads(result.stdout)["grandchild"]
    assert result.returncode == 0
    assert await _gone(_alive, grandchild)


async def test_run_cli_normal_exit_does_not_signal_a_vanished_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_killpg(monkeypatch)

    result = await run_cli([sys.executable, "-c", ECHO_SCRIPT], "x", 10, str(tmp_path))

    assert result.returncode == 0
    # After the leader was reaped the group is only probed (signal 0), never SIGKILLed.
    assert calls
    assert all(sig == 0 for _, sig in calls)


async def test_run_cli_passes_stdin_and_returns_output(tmp_path: Path) -> None:
    result = await run_cli(
        [sys.executable, "-c", ECHO_SCRIPT], stdin="hello", timeout=10, cwd=str(tmp_path)
    )

    assert result == CliResult(
        returncode=0, stdout="out:HELLO", stderr=f"err:{os.path.realpath(tmp_path)}"
    )


async def test_run_cli_reports_non_zero_returncode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXIT_CODE", "3")

    result = await run_cli([sys.executable, "-c", ECHO_SCRIPT], "x", 10, str(tmp_path))

    assert result.returncode == 3
    assert result.stdout == "out:X"


async def test_run_cli_inherits_env_minus_removed_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEEP_ME", "kept")
    monkeypatch.setenv("DROP_ME", "dropped")
    script = "import os; print(os.environ.get('KEEP_ME'), os.environ.get('DROP_ME'))"

    result = await run_cli(
        [sys.executable, "-c", script],
        "",
        10,
        str(tmp_path),
        env_remove=frozenset({"DROP_ME"}),
    )

    assert result.stdout.strip() == "kept None"


async def test_run_cli_timeout_kills_process_and_raises(tmp_path: Path) -> None:
    started = time.monotonic()

    with pytest.raises(LLMTimeoutError) as info:
        await run_cli([sys.executable, "-c", SLEEP_SCRIPT], "", 0.5, str(tmp_path))

    assert time.monotonic() - started < 10
    assert info.value.recoverable is True
    assert "started" not in info.value.message


async def test_run_cli_missing_binary_raises_non_recoverable(tmp_path: Path) -> None:
    with pytest.raises(LLMError) as info:
        await run_cli(["atlas-no-such-cli-binary"], "", 5, str(tmp_path))

    assert info.value.recoverable is False
    assert "atlas-no-such-cli-binary CLI not found" in info.value.message
    assert not isinstance(info.value, LLMTimeoutError)
