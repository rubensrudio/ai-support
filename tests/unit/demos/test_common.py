from pathlib import Path

import pytest

from api.config import get_settings
from api.llm.client import ChatMessage, LLMClient, LLMRequest, LLMResponse, SystemBlock, Usage
from api.llm.instrumented import InstrumentedLLMClient
from demos.common import (
    DEFAULT_OUT_DIR,
    DEFAULT_RESULTS_PATH,
    build_demo_llm,
    parse_demo_args,
    record_result,
)

HEADER = "| Phase | Metric | Before | After | Notes |\n|---|---|---|---|---|\n"


def _results_file(tmp_path: Path) -> Path:
    rows = "".join(f"| {n:02d} | | | | |\n" for n in range(1, 15))
    path = tmp_path / "results.md"
    path.write_text(f"# Results\n\nIntro line.\n\n{HEADER}{rows}\nFooter text.\n")
    return path


def _phase_rows(path: Path, phase: int) -> list[str]:
    return [line for line in path.read_text().splitlines() if line.startswith(f"| {phase:02d} |")]


def test_record_result_twice_keeps_one_row_with_last_values(tmp_path: Path) -> None:
    path = _results_file(tmp_path)

    record_result(3, "hallucination rate", "40%", "10%", "first run", results_path=path)
    record_result(3, "hallucination rate", "35%", "5%", "second run", results_path=path)

    rows = _phase_rows(path, 3)
    assert rows == ["| 03 | hallucination rate | 35% | 5% | second run |"]
    text = path.read_text()
    assert text.startswith("# Results\n\nIntro line.\n")
    assert text.endswith("\nFooter text.\n")
    assert len(_phase_rows(path, 2)) == 1
    assert len(_phase_rows(path, 4)) == 1
    lines = text.splitlines()
    assert lines.index(rows[0]) == lines.index(_phase_rows(path, 2)[0]) + 1


def test_record_result_is_idempotent(tmp_path: Path) -> None:
    path = _results_file(tmp_path)
    record_result(5, "p95", "120 ms", "8 ms", results_path=path)
    once = path.read_text()
    record_result(5, "p95", "120 ms", "8 ms", results_path=path)
    assert path.read_text() == once


def test_record_result_escapes_pipes_and_newlines(tmp_path: Path) -> None:
    path = _results_file(tmp_path)
    record_result(1, "a|b", "x\ny", "z", "n", results_path=path)
    assert _phase_rows(path, 1) == [r"| 01 | a\|b | x y | z | n |"]


def test_record_result_inserts_missing_row_in_order(tmp_path: Path) -> None:
    path = tmp_path / "results.md"
    path.write_text(f"{HEADER}| 01 | m | a | b | |\n| 05 | m | a | b | |\n")

    record_result(3, "metric", "1", "2", results_path=path)

    phases = [line[:6] for line in path.read_text().splitlines() if line.startswith("| 0")]
    assert phases == ["| 01 |", "| 03 |", "| 05 |"]


def test_record_result_removes_duplicate_rows(tmp_path: Path) -> None:
    path = tmp_path / "results.md"
    path.write_text(f"{HEADER}| 03 | old | a | b | |\n| 03 | older | a | b | |\n")

    record_result(3, "new", "1", "2", results_path=path)

    assert _phase_rows(path, 3) == ["| 03 | new | 1 | 2 |  |"]


def test_record_result_creates_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "docs" / "results.md"
    record_result(2, "tokens", "100", "50", results_path=path)
    assert path.read_text().startswith("| Phase | Metric | Before | After | Notes |")
    assert len(_phase_rows(path, 2)) == 1


def test_record_result_rejects_invalid_phase(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        record_result(0, "m", "a", "b", results_path=_results_file(tmp_path))


async def test_build_demo_llm_fake_needs_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "anthropic")
    get_settings.cache_clear()
    reply = LLMResponse(
        text="hi",
        tool_calls=[],
        usage=Usage(input_tokens=1, output_tokens=1),
        model="fake-model",
        provider="fake",
        stop_reason="end_turn",
    )
    try:
        client = build_demo_llm("fake", script=[reply])
    finally:
        get_settings.cache_clear()

    assert isinstance(client, LLMClient)
    assert isinstance(client, InstrumentedLLMClient)
    request = LLMRequest(system=[SystemBlock("s")], messages=[ChatMessage("user", "hello")])
    assert (await client.complete(request)).text == "hi"


def test_build_demo_llm_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError):
        build_demo_llm("openai")


def test_parse_demo_args_defaults_and_flags(tmp_path: Path) -> None:
    defaults = parse_demo_args("demo", argv=[])
    assert defaults.provider == "real"
    assert defaults.results == DEFAULT_RESULTS_PATH
    assert defaults.out_dir == DEFAULT_OUT_DIR

    args = parse_demo_args(
        "demo",
        argv=["--provider", "fake", "--results", str(tmp_path / "r.md"), "--out-dir", "img"],
    )
    assert args.provider == "fake"
    assert args.results == tmp_path / "r.md"
    assert args.out_dir == Path("img")

    with pytest.raises(SystemExit):
        parse_demo_args("demo", argv=["--provider", "other"])
