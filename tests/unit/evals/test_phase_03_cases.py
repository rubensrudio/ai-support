"""Phase 03 eval cases: grounded answers with the expected citation (TASK-052, DOD-01)."""

from pathlib import Path
from typing import Any

from evals.dataset import EvalCase, load_cases
from evals.runner import DEFAULT_GRADERS, run_cases

ROOT_DIR = Path(__file__).resolve().parents[3]
DATASETS_DIR = ROOT_DIR / "evals" / "datasets"
PHASE_03 = DATASETS_DIR / "phase_03.jsonl"
ADR_0003 = ROOT_DIR / "docs" / "adr" / "0003-rag-full-text.md"


class ExpectedTarget:
    """Returns the expected fields plus a cited answer, as a passing RAG chat run would."""

    async def run(self, case: EvalCase) -> dict[str, Any]:
        return {**case.expected, "content": "See the knowledge base [1]."}


def _phase_03_cases() -> list[EvalCase]:
    return [case for case in load_cases([PHASE_03]) if case.phase == 3]


def test_phase_03_has_rag_cases_split_evenly_between_pt_and_en() -> None:
    cases = _phase_03_cases()

    assert len(cases) >= 4
    assert all(case.category == "rag" for case in cases)
    by_language = {lang: [c for c in cases if c.language == lang] for lang in ("pt", "en")}
    assert len(by_language["pt"]) == len(by_language["en"]) == len(cases) // 2
    for case in cases:
        assert case.input["message"].strip(), case.id
        assert case.input["ui_language"] == case.language, case.id
        assert case.expected["language"] == case.language, case.id
        assert case.expected["citation_doc_key"], case.id
        assert case.id.startswith(f"phase03-rag-{case.language}-"), case.id


def test_phase_03_cases_cite_the_same_documents_in_both_languages() -> None:
    cases = _phase_03_cases()
    doc_keys = {
        lang: sorted(c.expected["citation_doc_key"] for c in cases if c.language == lang)
        for lang in ("pt", "en")
    }

    assert doc_keys["pt"] == doc_keys["en"]


def test_phase_03_cases_use_existing_graders() -> None:
    for case in load_cases([PHASE_03]):
        assert case.grader in DEFAULT_GRADERS, case.id


def test_all_datasets_load_together_including_phase_03() -> None:
    cases = load_cases(sorted(DATASETS_DIR.glob("*.jsonl")))

    assert {case.phase for case in cases} >= {1, 2, 3}


async def test_phase_03_cases_pass_for_a_matching_output() -> None:
    report = await run_cases(load_cases([PHASE_03]), ExpectedTarget(), DEFAULT_GRADERS)

    assert report.status == "ok"
    assert all(result.passed for result in report.cases)


def test_adr_0003_follows_the_template_in_english() -> None:
    text = ADR_0003.read_text(encoding="utf-8")

    assert text.startswith("# ADR 0003:")
    for heading in ("## Context", "## Decision", "## Alternatives considered", "## Consequences"):
        assert heading in text
    for topic in ("500", "80", "tsvector", "[n]", "demo_03"):
        assert topic in text
    assert "- Phase: 03" in text
