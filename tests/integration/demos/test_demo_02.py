"""Phase 02 context window demo (TASK-038, CHAT-02, CHAT-07, DOD-02)."""

import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from api.config import get_settings
from api.db.engine import dispose_engine
from demos.demo_01_stateless import NAME
from demos.demo_02_context_window import (
    CHART_NAME,
    TURNS,
    ContextAwareFakeLLM,
    RecordingLLMClient,
    main,
    run_long_conversation,
    run_name_dialogues,
)

pytestmark = pytest.mark.integration

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture(autouse=True)
def fake_settings(migrated_database: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "fake")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ATLAS_CONTEXT_BUDGET_TOKENS", raising=False)
    monkeypatch.delenv("ATLAS_HISTORY_MODE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def fresh_engine() -> AsyncIterator[None]:
    """Close the engine after each test: `main()` runs its own event loop."""
    yield
    await dispose_engine()


def _metric(output: str, name: str, mode: str) -> int:
    match = re.search(rf"^\[{mode}\] {name}=(\d+)$", output, re.MULTILINE)
    assert match, f"missing [{mode}] {name}= in output"
    return int(match.group(1))


def test_fake_run_writes_chart_and_row_and_keeps_summarized_history_in_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results.md"
    img_dir = tmp_path / "img"
    budget = get_settings().context_budget_tokens

    exit_code = main(["--provider", "fake", "--results", str(results), "--out-dir", str(img_dir)])

    output = capsys.readouterr().out
    assert exit_code == 0
    chart = img_dir / CHART_NAME
    assert chart.read_bytes().startswith(PNG_MAGIC)
    rows = [line for line in results.read_text().splitlines() if line.startswith("| 02 |")]
    assert len(rows) == 1
    assert "full:" in rows[0] and "summarized:" in rows[0]
    assert "[en] name_remembered=True" in output
    assert "[pt] name_remembered=True" in output
    assert _metric(output, "chat_calls", "full") == TURNS
    assert _metric(output, "chat_calls", "summarized") == TURNS
    assert _metric(output, "max_history_tokens", "summarized") <= budget
    assert _metric(output, "max_history_tokens", "full") > budget
    assert _metric(output, "summarize_calls", "full") == 0
    assert _metric(output, "summarize_calls", "summarized") >= 1


async def test_summarized_mode_never_sends_history_above_a_small_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATLAS_CONTEXT_BUDGET_TOKENS", "1500")
    get_settings.cache_clear()
    settings = get_settings()
    llm = RecordingLLMClient(ContextAwareFakeLLM())

    run = await run_long_conversation(llm, settings, "summarized", turns=20)

    chat_calls = [call for call in run.calls if call.purpose == "chat"]
    assert len(chat_calls) == 20
    assert all(call.history_tokens <= 1500 for call in chat_calls)
    assert any(call.purpose == "summarize" for call in run.calls)
    assert run.name_remembered, run.last_answer


async def test_full_mode_grows_history_every_turn_and_never_summarizes() -> None:
    llm = RecordingLLMClient(ContextAwareFakeLLM())

    run = await run_long_conversation(llm, get_settings(), "full", turns=6)

    history = [call.history_tokens for call in run.calls if call.purpose == "chat"]
    assert len(history) == 6
    assert history == sorted(history) and len(set(history)) == 6
    assert all(call.purpose == "chat" for call in run.calls)
    assert run.name_remembered


async def test_dialogue_in_one_conversation_sends_the_earlier_turns() -> None:
    fake = ContextAwareFakeLLM()

    results = await run_name_dialogues(fake, get_settings())

    assert [r.language for r in results] == ["en", "pt"]
    assert all(r.name_remembered for r in results)
    for result in results:
        first, second = result.conversation_ids
        assert first is not None and first == second
    chat = [request for request in fake.requests if request.purpose == "chat"]
    assert len(chat) == 4
    assert [m.role for m in chat[1].messages] == ["user", "assistant", "user"]
    assert NAME in chat[1].messages[0].content
