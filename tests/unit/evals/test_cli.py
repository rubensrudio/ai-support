"""Eval CLI wiring (TASK-126): registered targets, grader per case and dataset globs."""

import json
from pathlib import Path
from typing import Any

import pytest

from api.llm.client import LLMError
from api.llm.fake import FakeLLMClient
from evals import __main__ as cli
from evals.dataset import EvalCase, load_cases
from evals.targets import AutoTarget, ChatTarget, TriageTarget, is_triage_case

ROOT_DIR = Path(__file__).resolve().parents[3]
DATASETS = ROOT_DIR / "evals" / "datasets"


def _case(**overrides: Any) -> EvalCase:
    raw: dict[str, Any] = {
        "id": "cli-case",
        "phase": 13,
        "category": "red_team",
        "language": "en",
        "input": {"message": "hi"},
        "expected": {"blocked": True},
        "grader": "exact_match",
    }
    raw.update(overrides)
    return EvalCase.model_validate(raw)


def test_chat_triage_and_auto_targets_are_registered_next_to_echo() -> None:
    assert {"echo", "chat", "triage", "auto"} <= set(cli.TARGETS)
    assert isinstance(cli.TARGETS["chat"](), ChatTarget)
    assert isinstance(cli.TARGETS["triage"](), TriageTarget)
    assert isinstance(cli.TARGETS["auto"](), AutoTarget)


@pytest.mark.parametrize(
    ("overrides", "triage"),
    [
        ({"category": "json_valid", "input": {"text": "x"}}, True),
        ({"category": "json_valid", "input": {"message": "x"}}, True),
        ({"category": "smoke", "input": {"text": "x"}}, True),
        ({"category": "tool_trajectory", "input": {"message": "x"}}, False),
        ({"category": "rag", "input": {"text": "x", "message": "y"}}, False),
    ],
)
def test_auto_target_routes_triage_cases(overrides: dict[str, Any], triage: bool) -> None:
    assert is_triage_case(_case(**overrides)) is triage


@pytest.mark.parametrize(
    ("overrides", "excluded"),
    [
        ({"category": "smoke", "input": {"endpoint": "/chat/stream"}}, True),
        ({"category": "smoke", "input": {"set": "control"}}, False),
        ({"category": "red_team", "input": {"set": "attack"}}, False),
        ({"category": "tone", "input": {}}, False),
    ],
)
def test_auto_excludes_only_non_control_smoke(overrides: dict[str, Any], excluded: bool) -> None:
    assert cli.excluded_for_auto(_case(**overrides)) is excluded


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "red_team"),  # exact_match on a non-smoke case -> category default
        ({"grader": "red_team"}, "red_team"),  # core datasets: grader == category
        ({"category": "tool_trajectory", "grader": "exact_match"}, "tool_trajectory"),
        ({"category": "json_valid", "grader": "json_valid"}, "json_valid"),
        ({"category": "smoke", "grader": "exact_match"}, "exact_match"),  # smoke keeps it
        (
            {"category": "smoke", "input": {"set": "control", "message": "hi"}},
            "red_team_blocked",
        ),
        ({"input": {"set": "control", "message": "hi"}}, "red_team_blocked"),
        ({"grader": "citation", "category": "rag"}, "citation"),  # explicit named grader
    ],
)
def test_grader_name_for_case(overrides: dict[str, Any], expected: str) -> None:
    assert cli.grader_name(_case(**overrides)) == expected


def test_unknown_grader_name_stays_unknown() -> None:
    assert cli.grader_name(_case(grader="does_not_exist")) is None
    assert "does_not_exist" not in cli.build_graders(FakeLLMClient(script=[]))


async def test_control_case_is_graded_by_red_team_blocked() -> None:
    graders = cli.build_graders(FakeLLMClient(script=[]))
    control = _case(category="smoke", input={"set": "control"}, expected={"blocked": False})

    passed = await graders["exact_match"](control, {"status": "completed", "error": None})
    failed = await graders["exact_match"](control, {"status": "blocked", "error": None})

    assert (passed.score, failed.score) == (100.0, 0.0)


async def test_smoke_case_keeps_exact_match() -> None:
    graders = cli.build_graders(FakeLLMClient(script=[]))
    smoke = _case(category="smoke", input={"text": "x"}, expected={"text": "x"})

    assert (await graders["exact_match"](smoke, {"text": "x"})).score == 100.0


async def test_judge_graders_use_the_judge_client() -> None:
    judge = FakeLLMClient(script=[LLMError("judge down", False)])
    graders = cli.build_graders(judge)
    tone = _case(category="tone", grader="tone", expected={})

    grade = await graders["tone"](tone, {"text": "Hello!", "error": None})

    assert grade.score == 0.0 and "judge down" in grade.details["error"]
    assert len(judge.requests) == 1


def test_brace_glob_selects_the_chat_datasets_without_smoke() -> None:
    pattern = str(DATASETS / "{core_*,phase_*,redteam}.jsonl")

    paths = cli.dataset_paths(pattern)

    names = [path.name for path in paths]
    assert "smoke.jsonl" not in names
    assert {"core_en.jsonl", "phase_01.jsonl", "redteam.jsonl"} <= set(names)
    assert names == sorted(names) and len(names) == len(set(names))


def test_plain_glob_is_unchanged() -> None:
    assert cli.dataset_paths(str(DATASETS / "smoke.jsonl")) == [DATASETS / "smoke.jsonl"]


def test_every_chat_dataset_case_gets_a_known_grader() -> None:
    cases = load_cases(cli.dataset_paths(str(DATASETS / "{core_*,phase_*,redteam}.jsonl")))

    assert len(cases) > 100
    unknown = [case.id for case in cases if cli.grader_name(case) not in cli.GRADER_NAMES]
    assert unknown == []
    assert {cli.grader_name(case) for case in cases} >= {
        "rag",
        "tool_trajectory",
        "json_valid",
        "red_team",
        "tone",
        "red_team_blocked",
    }


@pytest.mark.parametrize(
    ("case_input", "reason"),
    [
        ({"category": "smoke", "input": {"endpoint": "/chat/stream"}}, cli.REASON_SMOKE),
        ({"category": "red_team", "input": {"model_answer": "Done."}}, cli.REASON_SCRIPTED),
        (
            {"category": "smoke", "input": {"set": "control", "fake_reply": "x"}},
            cli.REASON_SCRIPTED,
        ),
        ({"category": "red_team", "input": {"set": "attack"}}, None),
    ],
)
def test_auto_exclusion_reason(case_input: dict[str, Any], reason: str | None) -> None:
    assert cli.exclusion_reason(_case(**case_input)) == reason


# --- execution errors (EVAL-90) ----------------------------------------------------------------


class _FixedTarget:
    def __init__(self, output: dict[str, Any]) -> None:
        self.output = output

    async def run(self, case: EvalCase) -> dict[str, Any]:
        return dict(self.output)


def _run_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cases: list[EvalCase], output: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text("".join(case.model_dump_json() + "\n" for case in cases), encoding="utf-8")
    out = tmp_path / "report.json"
    monkeypatch.setitem(cli.TARGETS, "fixed", lambda: _FixedTarget(output))
    code = cli.main(["run", "--dataset", str(dataset), "--target", "fixed", "--out", str(out)])
    return code, json.loads(out.read_text(encoding="utf-8"))


def test_target_error_marks_the_report_error_and_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = [_case(id="ok-001", expected={"blocked": True}), _case(id="down-001")]
    outputs = {"status": "", "error": "AtlasError: LLM_UNAVAILABLE (502)"}

    code, report = _run_cli(tmp_path, monkeypatch, cases, outputs)

    assert code == 2
    assert report["status"] == "error"
    assert [case["score"] for case in report["cases"]] == [0.0, 0.0]  # scores kept
    assert all("LLM_UNAVAILABLE" in case["details"]["error"] for case in report["cases"])
    assert "2 case(s) had execution errors" in capsys.readouterr().err


def test_exact_match_case_gets_the_target_error_in_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = _case(id="smoke-001", category="smoke", input={"text": "x"}, expected={"text": "x"})

    code, report = _run_cli(tmp_path, monkeypatch, [smoke], {"text": "", "error": "boom"})

    assert code == 2 and report["status"] == "error"
    assert report["cases"][0]["details"]["error"] == "boom"


def test_judge_error_inside_the_rag_mean_is_an_execution_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rag = _case(
        id="rag-001",
        category="rag",
        grader="rag",
        input={"message": "How do I cancel?"},
        expected={"citation_doc_key": "end-subscription"},
    )
    output = {"text": "See [1].", "citations": ["end-subscription"], "chunks": [], "error": None}

    code, report = _run_cli(tmp_path, monkeypatch, [rag], output)

    assert code == 2 and report["status"] == "error"
    details = report["cases"][0]["details"]
    assert details["citation"]["score"] == 100.0  # the partial score stays
    assert details["error"] == details["faithfulness"]["error"]
    assert report["cases"][0]["score"] == 50.0


def test_clean_run_stays_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, report = _run_cli(
        tmp_path, monkeypatch, [_case(id="ok-001")], {"status": "blocked", "error": None}
    )

    assert code == 0 and report["status"] == "ok"
    assert report["cases"][0]["score"] == 100.0
