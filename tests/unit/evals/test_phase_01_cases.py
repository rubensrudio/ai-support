"""Phase 01 eval cases (TASK-022, DOD-01, DOD-04)."""

from pathlib import Path
from typing import Any

from evals.dataset import EvalCase, load_cases
from evals.runner import DEFAULT_GRADERS, run_cases

ROOT_DIR = Path(__file__).resolve().parents[3]
DATASETS_DIR = ROOT_DIR / "evals" / "datasets"
PHASE_01 = DATASETS_DIR / "phase_01.jsonl"


class ExpectedTarget:
    """Returns the expected fields plus a non-empty answer, as a passing chat run would."""

    async def run(self, case: EvalCase) -> dict[str, Any]:
        return {**case.expected, "content": "Sure, I can help."}


def test_phase_01_has_tone_cases_in_both_languages() -> None:
    cases = load_cases([PHASE_01])

    phase_cases = [case for case in cases if case.phase == 1]
    assert len(phase_cases) >= 2
    assert {case.language for case in phase_cases} >= {"pt", "en"}
    assert all(case.category == "tone" for case in phase_cases)
    for case in phase_cases:
        assert case.input["message"].strip()
        assert case.input["ui_language"] == case.language
        assert case.expected["language"] == case.language


def test_phase_01_cases_use_existing_graders() -> None:
    for case in load_cases([PHASE_01]):
        assert case.grader in DEFAULT_GRADERS, case.id


def test_all_datasets_load_together_without_duplicate_ids() -> None:
    cases = load_cases(sorted(DATASETS_DIR.glob("*.jsonl")))

    assert any(case.phase == 1 for case in cases)


async def test_phase_01_cases_pass_for_a_matching_output() -> None:
    cases = load_cases([PHASE_01])

    report = await run_cases(cases, ExpectedTarget(), DEFAULT_GRADERS)

    assert report.status == "ok"
    assert all(result.passed for result in report.cases)
    assert report.scores.by_category == {"tone": 100.0}
