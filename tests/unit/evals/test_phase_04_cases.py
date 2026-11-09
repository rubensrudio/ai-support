"""Phase 04 eval cases: synonym questions answered from the expected document (TASK-059)."""

from pathlib import Path
from typing import Any

from evals.dataset import EvalCase, load_cases
from evals.runner import DEFAULT_GRADERS, run_cases

ROOT_DIR = Path(__file__).resolve().parents[3]
DATASETS_DIR = ROOT_DIR / "evals" / "datasets"
PHASE_04 = DATASETS_DIR / "phase_04.jsonl"
ADR_0004 = ROOT_DIR / "docs" / "adr" / "0004-embeddings.md"


class ExpectedTarget:
    """Returns the expected fields plus a cited answer, as a passing RAG chat run would."""

    async def run(self, case: EvalCase) -> dict[str, Any]:
        return {**case.expected, "content": "See the knowledge base [1]."}


def _phase_04_cases() -> list[EvalCase]:
    return [case for case in load_cases([PHASE_04]) if case.phase == 4]


def test_phase_04_has_synonym_rag_cases_in_pt_and_en() -> None:
    cases = _phase_04_cases()

    assert len(cases) >= 2
    assert {case.language for case in cases} == {"pt", "en"}
    for case in cases:
        assert case.category == "rag", case.id
        assert case.input["message"].strip(), case.id
        assert case.input["ui_language"] == case.language, case.id
        assert case.expected["language"] == case.language, case.id
        assert case.expected["citation_doc_key"], case.id
        assert case.id.startswith(f"phase04-rag-{case.language}-"), case.id


def test_phase_04_cases_ask_cancel_plan_and_cite_end_subscription() -> None:
    cases = _phase_04_cases()
    synonym = {"pt": "cancelar", "en": "cancel"}

    for language, word in synonym.items():
        assert any(
            case.language == language
            and word in case.input["message"].lower()
            and case.expected["citation_doc_key"] == "end-subscription"
            for case in cases
        ), language


def test_phase_04_cases_cite_documents_of_the_corpus() -> None:
    for case in _phase_04_cases():
        key = case.expected["citation_doc_key"]
        assert (ROOT_DIR / "corpus" / case.language / f"{key}.md").is_file(), case.id


def test_phase_04_cases_use_existing_graders() -> None:
    for case in load_cases([PHASE_04]):
        assert case.grader in DEFAULT_GRADERS, case.id


def test_all_datasets_load_together_including_phase_04() -> None:
    cases = load_cases(sorted(DATASETS_DIR.glob("*.jsonl")))

    assert {case.phase for case in cases} >= {1, 2, 3, 4}


async def test_phase_04_cases_pass_for_a_matching_output() -> None:
    report = await run_cases(load_cases([PHASE_04]), ExpectedTarget(), DEFAULT_GRADERS)

    assert report.status == "ok"
    assert all(result.passed for result in report.cases)


def test_adr_0004_follows_the_template_in_english() -> None:
    text = ADR_0004.read_text(encoding="utf-8")

    assert text.startswith("# ADR 0004:")
    for heading in ("## Context", "## Decision", "## Alternatives considered", "## Consequences"):
        assert heading in text
    for topic in ("text-embedding-3-small", "1536", "NumPy", "256", "demo_04", "DA-19"):
        assert topic in text
    assert "- Phase: 04" in text
