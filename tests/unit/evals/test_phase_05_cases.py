"""Phase 05 eval cases: questions mixing an exact corpus term with a synonym (TASK-064)."""

import re
from pathlib import Path
from typing import Any

from demos.demo_04_embeddings import FAKE_SYNONYMS
from evals.dataset import EvalCase, load_cases
from evals.runner import DEFAULT_GRADERS, run_cases

ROOT_DIR = Path(__file__).resolve().parents[3]
DATASETS_DIR = ROOT_DIR / "evals" / "datasets"
PHASE_05 = DATASETS_DIR / "phase_05.jsonl"
ADR_0005 = ROOT_DIR / "docs" / "adr" / "0005-vector-database.md"

_WORD = re.compile(r"\w+")
# Function words that never count as the "exact term" of a question.
STOP_WORDS = {
    "en": {"a", "can", "do", "for", "how", "i", "my", "the", "to", "two", "what"},
    "pt": {"a", "como", "da", "de", "dois", "e", "minha", "o", "para", "por", "posso", "que"},
}


class ExpectedTarget:
    """Returns the expected fields plus a cited answer, as a passing RAG chat run would."""

    async def run(self, case: EvalCase) -> dict[str, Any]:
        return {**case.expected, "content": "See the knowledge base [1]."}


def _phase_05_cases() -> list[EvalCase]:
    return [case for case in load_cases([PHASE_05]) if case.phase == 5]


def _words(value: str) -> set[str]:
    return set(_WORD.findall(value.lower()))


def test_phase_05_has_rag_cases_in_pt_and_en() -> None:
    cases = _phase_05_cases()

    assert len(cases) >= 2
    assert {case.language for case in cases} == {"pt", "en"}
    assert len({case.id for case in cases}) == len(cases)
    for case in cases:
        assert case.category == "rag", case.id
        assert case.input["message"].strip(), case.id
        assert case.input["ui_language"] == case.language, case.id
        assert case.expected["language"] == case.language, case.id
        assert case.expected["citation_doc_key"], case.id
        assert case.id.startswith(f"phase05-rag-{case.language}-"), case.id


def test_phase_05_cases_combine_an_exact_term_and_a_synonym() -> None:
    for case in _phase_05_cases():
        key = case.expected["citation_doc_key"]
        document = _words((ROOT_DIR / "corpus" / case.language / f"{key}.md").read_text())
        question = _words(case.input["message"])

        exact = (question & document) - STOP_WORDS[case.language]
        synonyms = {word for word in question - document if word in FAKE_SYNONYMS}
        assert exact, f"{case.id} has no exact term of {key}"
        assert synonyms, f"{case.id} has no synonym absent from {key}"


def test_phase_05_cases_use_existing_graders() -> None:
    for case in load_cases([PHASE_05]):
        assert case.grader in DEFAULT_GRADERS, case.id


def test_all_datasets_load_together_including_phase_05() -> None:
    cases = load_cases(sorted(DATASETS_DIR.glob("*.jsonl")))

    assert {case.phase for case in cases} >= {1, 2, 3, 4, 5}


async def test_phase_05_cases_pass_for_a_matching_output() -> None:
    report = await run_cases(load_cases([PHASE_05]), ExpectedTarget(), DEFAULT_GRADERS)

    assert report.status == "ok"
    assert all(result.passed for result in report.cases)


def test_adr_0005_follows_the_template_in_english() -> None:
    text = ADR_0005.read_text(encoding="utf-8")

    assert text.startswith("# ADR 0005:")
    for heading in ("## Context", "## Decision", "## Alternatives considered", "## Consequences"):
        assert heading in text
    for topic in (
        "HNSW",
        "ef_construction",
        "ef_search",
        "reindex-hnsw",
        "rrf_k",
        "60",
        "LAC-18",
        "256",
        "hybrid",
        "demo_05",
    ):
        assert topic in text, topic
    assert "- Phase: 05" in text
