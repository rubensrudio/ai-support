"""Phase 01 stateless demo (TASK-021, CHAT-02, DOD-02).

From phase 02 on the chat service persists conversations, so the demo runs against the test
database with the seeded personas. Each call goes without `conversation_id` and therefore
starts a new conversation (DA-16): the problem of phase 01 is still reproduced on HEAD.
"""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from api.config import get_settings
from api.db.engine import dispose_engine
from api.llm.client import LLMResponse, Usage
from api.llm.fake import FakeLLMClient
from demos.demo_01_stateless import NAME, default_fake_script, main, run_dialogues

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def fake_settings(migrated_database: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "fake")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def fresh_engine() -> AsyncIterator[None]:
    """Close the engine after each test: `main()` runs its own event loop."""
    yield
    await dispose_engine()


def _reply(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        usage=Usage(input_tokens=1, output_tokens=1),
        model="fake-model",
        provider="fake",
        stop_reason="end_turn",
    )


def _phase_01_row(path: Path) -> str:
    rows = [line for line in path.read_text().splitlines() if line.startswith("| 01 |")]
    assert len(rows) == 1
    return rows[0]


def test_fake_run_reports_the_name_is_forgotten(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results.md"

    exit_code = main(["--provider", "fake", "--results", str(results)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "name_remembered=False" in output
    assert "name_remembered=True" not in output
    assert "What is my name?" in output
    assert "Qual é o meu nome?" in output
    row = _phase_01_row(results)
    assert "en: no, pt: no" in row
    assert "phase 02" in row


def test_answer_with_the_name_is_reported_as_remembered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results.md"
    script = [
        _reply("Hi!"),
        _reply(f"Your name is {NAME}."),
        _reply("Oi!"),
        _reply("Não sei."),
    ]

    assert main(["--provider", "fake", "--results", str(results)], script=script) == 0

    output = capsys.readouterr().out
    assert "[en] name_remembered=True" in output
    assert "[pt] name_remembered=False" in output
    assert output.splitlines()[-2] == "name_remembered=True"
    assert "en: yes, pt: no" in _phase_01_row(results)


async def test_each_call_starts_a_new_conversation_with_only_the_current_message() -> None:
    fake = FakeLLMClient(script=default_fake_script())

    results = await run_dialogues(fake, get_settings())

    assert [r.language for r in results] == ["en", "pt"]
    assert not any(r.name_remembered for r in results)
    assert len({r.conversation_ids for r in results}) == 2
    for result in results:
        first, second = result.conversation_ids
        assert first is not None and second is not None and first != second
    assert len(fake.requests) == 4
    for request in fake.requests:
        assert [m.role for m in request.messages] == ["user"]
    asked = [request.messages[0].content for request in fake.requests]
    assert asked[1] == "What is my name?"
    assert NAME not in asked[1]
    assert NAME not in asked[3]
