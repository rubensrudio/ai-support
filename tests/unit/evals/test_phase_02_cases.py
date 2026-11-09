"""Phase 02 eval cases: multi-turn memory in one conversation (TASK-039, DOD-01, DOD-04)."""

from pathlib import Path
from typing import Any

from evals.dataset import EvalCase, load_cases
from evals.runner import DEFAULT_GRADERS, run_cases

ROOT_DIR = Path(__file__).resolve().parents[3]
DATASETS_DIR = ROOT_DIR / "evals" / "datasets"
PHASE_02 = DATASETS_DIR / "phase_02.jsonl"


class ExpectedTarget:
    """Returns the expected fields plus an answer, as a passing multi-turn chat run would."""

    async def run(self, case: EvalCase) -> dict[str, Any]:
        return {**case.expected, "content": f"Your name is {case.input['recall']}."}


def test_phase_02_has_memory_cases_in_both_languages() -> None:
    cases = [case for case in load_cases([PHASE_02]) if case.phase == 2]

    assert len(cases) >= 2
    assert {case.language for case in cases} >= {"pt", "en"}
    for case in cases:
        assert case.category == "tone"
        assert case.input["ui_language"] == case.language
        assert case.expected["language"] == case.language
        assert case.expected["name_recalled"] is True


def test_phase_02_cases_state_the_fact_before_asking_for_it() -> None:
    for case in load_cases([PHASE_02]):
        history = case.input["history"]
        assert history, case.id
        assert all(turn.strip() for turn in history), case.id
        assert case.input["recall"] in history[0], case.id
        assert case.input["recall"] not in case.input["message"], case.id


def test_phase_02_cases_use_existing_graders() -> None:
    for case in load_cases([PHASE_02]):
        assert case.grader in DEFAULT_GRADERS, case.id


def test_all_datasets_load_together_without_duplicate_ids() -> None:
    cases = load_cases(sorted(DATASETS_DIR.glob("*.jsonl")))

    assert {case.phase for case in cases} >= {1, 2}


async def test_phase_02_cases_pass_for_a_matching_output() -> None:
    report = await run_cases(load_cases([PHASE_02]), ExpectedTarget(), DEFAULT_GRADERS)

    assert report.status == "ok"
    assert all(result.passed for result in report.cases)
