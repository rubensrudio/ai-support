"""Minimal eval runner: JSONL datasets, scoring and the CT-10 JSON report (TASK-011)."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from api.config import get_settings
from evals.__main__ import main
from evals.dataset import DatasetError, EvalCase, load_cases
from evals.runner import DEFAULT_GRADERS, EchoTarget, exact_match, run_cases

ROOT_DIR = Path(__file__).resolve().parents[3]
SMOKE_DATASET = ROOT_DIR / "evals" / "datasets" / "smoke.jsonl"

CASE_KEYS = {"id", "category", "language", "score", "passed", "details"}
REPORT_KEYS = {"run_id", "status", "cases", "scores"}


def _case(**overrides: Any) -> dict[str, Any]:
    case: dict[str, Any] = {
        "id": "smoke-001",
        "phase": 0,
        "category": "smoke",
        "language": "en",
        "input": {"text": "ping"},
        "expected": {"text": "ping"},
        "grader": "exact_match",
    }
    case.update(overrides)
    return case


def _write_jsonl(path: Path, rows: list[dict[str, Any] | str]) -> Path:
    lines = [row if isinstance(row, str) else json.dumps(row) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class FixedTarget:
    """Fake target returning a fixed output for every case."""

    def __init__(self, output: dict[str, Any]) -> None:
        self.output = output

    async def run(self, case: EvalCase) -> dict[str, Any]:
        return self.output


class ExplodingTarget:
    async def run(self, case: EvalCase) -> dict[str, Any]:
        if case.id == "boom":
            raise RuntimeError("target exploded")
        return dict(case.input)


# --- dataset -------------------------------------------------------------------------------


def test_load_cases_parses_every_field(tmp_path: Path) -> None:
    path = _write_jsonl(tmp_path / "cases.jsonl", [_case(), "", _case(id="smoke-002")])

    cases = load_cases([path])

    assert [case.id for case in cases] == ["smoke-001", "smoke-002"]
    assert cases[0].phase == 0
    assert cases[0].category == "smoke"
    assert cases[0].language == "en"
    assert cases[0].input == {"text": "ping"}
    assert cases[0].expected == {"text": "ping"}
    assert cases[0].grader == "exact_match"


def test_invalid_json_line_raises_error_citing_file_and_line(tmp_path: Path) -> None:
    path = _write_jsonl(tmp_path / "broken.jsonl", [_case(), "{not json"])

    with pytest.raises(DatasetError) as error:
        load_cases([path])

    assert error.value.line_number == 2
    assert "broken.jsonl:2" in str(error.value)


@pytest.mark.parametrize(
    "bad_case",
    [
        _case(category="unknown"),
        _case(language="es"),
        _case(phase="one"),
        _case(input="not a dict"),
        _case(extra_field=True),
        {key: value for key, value in _case().items() if key != "grader"},
    ],
    ids=["category", "language", "phase", "input", "extra-field", "missing-grader"],
)
def test_schema_violation_raises_error_citing_line(
    tmp_path: Path, bad_case: dict[str, Any]
) -> None:
    path = _write_jsonl(tmp_path / "cases.jsonl", [_case(), _case(id="ok-2"), bad_case])

    with pytest.raises(DatasetError) as error:
        load_cases([path])

    assert error.value.line_number == 3
    assert "cases.jsonl:3" in str(error.value)


def test_duplicate_case_id_raises_error(tmp_path: Path) -> None:
    first = _write_jsonl(tmp_path / "a.jsonl", [_case()])
    second = _write_jsonl(tmp_path / "b.jsonl", [_case()])

    with pytest.raises(DatasetError) as error:
        load_cases([first, second])

    assert "b.jsonl:1" in str(error.value)
    assert "smoke-001" in str(error.value)


def test_smoke_dataset_is_valid() -> None:
    cases = load_cases([SMOKE_DATASET])

    assert cases
    assert {case.category for case in cases} == {"smoke"}
    assert {case.language for case in cases} == {"pt", "en"}


# --- graders and runner --------------------------------------------------------------------


async def test_exact_match_scores_100_or_0() -> None:
    case = EvalCase.model_validate(_case(expected={"text": "ping"}))

    hit = await exact_match(case, {"text": "ping", "extra": 1})
    miss = await exact_match(case, {"text": "pong"})

    assert (hit.score, hit.passed) == (100.0, True)
    assert (miss.score, miss.passed) == (0.0, False)
    assert miss.details["mismatches"] == {"text": {"expected": "ping", "actual": "pong"}}


async def test_single_case_with_fake_target_scores_100() -> None:
    cases = [EvalCase.model_validate(_case())]

    report = await run_cases(cases, FixedTarget({"text": "ping"}), DEFAULT_GRADERS)
    payload = json.loads(report.model_dump_json())

    assert set(payload) == REPORT_KEYS
    assert payload["status"] == "ok"
    assert payload["scores"]["overall"] == 100.0
    assert payload["scores"]["by_category"] == {"smoke": 100.0}
    assert set(payload["cases"][0]) == CASE_KEYS
    assert payload["cases"][0]["passed"] is True
    assert payload["run_id"]


async def test_scores_are_averaged_per_category_and_overall() -> None:
    cases = [
        EvalCase.model_validate(_case(id="a", category="smoke")),
        EvalCase.model_validate(_case(id="b", category="smoke", expected={"text": "x"})),
        EvalCase.model_validate(_case(id="c", category="tone")),
    ]

    report = await run_cases(cases, EchoTarget(), DEFAULT_GRADERS)

    assert report.scores.by_category == {"smoke": 50.0, "tone": 100.0}
    assert report.scores.overall == pytest.approx(66.67)
    assert report.status == "ok"


async def test_target_error_is_recorded_without_aborting_the_run() -> None:
    cases = [
        EvalCase.model_validate(_case(id="boom")),
        EvalCase.model_validate(_case(id="fine")),
    ]

    report = await run_cases(cases, ExplodingTarget(), DEFAULT_GRADERS)

    assert report.status == "error"
    failed, fine = report.cases
    assert (failed.id, failed.passed, failed.score) == ("boom", False, 0.0)
    assert "target exploded" in failed.details["error"]
    assert (fine.id, fine.passed, fine.score) == ("fine", True, 100.0)
    assert report.scores.overall == 50.0


async def test_unknown_grader_is_a_case_error() -> None:
    cases = [EvalCase.model_validate(_case(grader="does_not_exist"))]

    report = await run_cases(cases, EchoTarget(), DEFAULT_GRADERS)

    assert report.status == "error"
    assert report.cases[0].passed is False
    assert "does_not_exist" in report.cases[0].details["error"]


async def test_empty_case_list_yields_zero_scores() -> None:
    report = await run_cases([], EchoTarget(), DEFAULT_GRADERS)

    assert report.status == "ok"
    assert report.scores.overall == 0.0
    assert report.scores.by_category == {}


# --- CLI -----------------------------------------------------------------------------------


@pytest.fixture
def fake_llm_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "fake")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.usefixtures("fake_llm_env")
def test_cli_run_writes_report_and_exits_0(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "eval-smoke.json"

    code = main(["run", "--dataset", str(SMOKE_DATASET), "--target", "echo", "--out", str(out)])

    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == "ok"
    assert payload["scores"]["overall"] == 100.0


@pytest.mark.usefixtures("fake_llm_env")
def test_cli_accepts_a_glob(tmp_path: Path) -> None:
    _write_jsonl(tmp_path / "one.jsonl", [_case(id="one")])
    _write_jsonl(tmp_path / "two.jsonl", [_case(id="two")])
    out = tmp_path / "report.json"

    code = main(
        ["run", "--dataset", str(tmp_path / "*.jsonl"), "--target", "echo", "--out", str(out)]
    )

    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert [case["id"] for case in payload["cases"]] == ["one", "two"]


@pytest.mark.usefixtures("fake_llm_env")
@pytest.mark.parametrize(
    "scenario", ["invalid-dataset", "no-match", "unknown-target", "case-error"]
)
def test_cli_exits_2_on_execution_error(tmp_path: Path, scenario: str) -> None:
    dataset = tmp_path / "cases.jsonl"
    target = "echo"
    if scenario == "invalid-dataset":
        _write_jsonl(dataset, ["{bad"])
    elif scenario == "no-match":
        dataset = tmp_path / "missing-*.jsonl"
    elif scenario == "unknown-target":
        _write_jsonl(dataset, [_case()])
        target = "nope"
    else:
        _write_jsonl(dataset, [_case(grader="does_not_exist")])
    out = tmp_path / "report.json"

    code = main(["run", "--dataset", str(dataset), "--target", target, "--out", str(out)])

    assert code == 2
    if scenario == "case-error":
        assert json.loads(out.read_text(encoding="utf-8"))["status"] == "error"
