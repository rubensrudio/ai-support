"""Phase 08 eval cases: valid triage JSON in pt and en (TASK-091)."""

from pathlib import Path
from typing import Any

from api.triage.schemas import TriageResult
from evals.dataset import EvalCase, load_cases
from evals.runner import DEFAULT_GRADERS, run_cases

ROOT_DIR = Path(__file__).resolve().parents[3]
DATASETS_DIR = ROOT_DIR / "evals" / "datasets"
PHASE_08 = DATASETS_DIR / "phase_08.jsonl"
ADR_0008 = ROOT_DIR / "docs" / "adr" / "0008-structured-output.md"
TRIAGE_FIELDS = sorted(TriageResult.model_fields)


class ExpectedTarget:
    """Returns the expected fields, as a passing triage run would."""

    async def run(self, case: EvalCase) -> dict[str, Any]:
        return dict(case.expected)


def _phase_08_cases() -> list[EvalCase]:
    return [case for case in load_cases([PHASE_08]) if case.phase == 8]


def test_phase_08_has_json_valid_cases_in_pt_and_en() -> None:
    cases = _phase_08_cases()

    assert len(cases) >= 2
    assert {case.language for case in cases} == {"pt", "en"}
    assert len({case.id for case in cases}) == len(cases)
    for case in cases:
        assert case.category == "json_valid", case.id
        assert case.id.startswith(f"phase08-triage-{case.language}-"), case.id
        assert case.input["text"].strip(), case.id
        assert case.input["mode"] in ("prompt_only", "schema"), case.id


def test_phase_08_cases_expect_a_valid_triage_object() -> None:
    allowed = TriageResult.model_json_schema()["properties"]["categoria"]["enum"]

    for case in _phase_08_cases():
        assert case.expected["json_valid"] is True, case.id
        assert case.expected["fields"] == TRIAGE_FIELDS, case.id
        assert case.expected["categoria"] in allowed, case.id
        assert case.expected["resumo_language"] == case.language, case.id


def test_phase_08_cases_use_existing_graders() -> None:
    for case in load_cases([PHASE_08]):
        assert case.grader in DEFAULT_GRADERS, case.id


def test_all_datasets_load_together_including_phase_08() -> None:
    cases = load_cases(sorted(DATASETS_DIR.glob("*.jsonl")))

    assert {case.phase for case in cases} >= {1, 2, 3, 4, 5, 6, 7, 8}


async def test_phase_08_cases_pass_for_a_matching_output() -> None:
    report = await run_cases(load_cases([PHASE_08]), ExpectedTarget(), DEFAULT_GRADERS)

    assert report.status == "ok"
    assert all(result.passed for result in report.cases)


def test_adr_0008_follows_the_template_in_english() -> None:
    text = ADR_0008.read_text(encoding="utf-8")

    assert text.startswith("# ADR 0008:")
    assert "- Phase: 08" in text
    for heading in ("## Context", "## Decision", "## Alternatives considered", "## Consequences"):
        assert heading in text
    for topic in (
        "TriageResult",
        "TriageService",
        "response_schema",
        "output_config",
        "--json-schema",
        "extra='forbid'",
        "ATLAS_TRIAGE_MODE",
        "ATLAS_TRIAGE_MAX_REPAIRS",
        "TRIAGE_UNAVAILABLE",
        "triage_failed",
        "X-Operator-Key",
        "require_customer_or_operator",
        "prompt_only",
        "demo_08",
    ):
        assert topic in text, topic
