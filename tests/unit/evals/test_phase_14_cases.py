"""Phase 14 eval cases: RAG regression cases born from a negative-feedback trace (the outdated
shipping-fee bulletin of demo 14), in pt and en, and ADR 0014 (TASK-140, DOD-01, DOD-04)."""

import json
from pathlib import Path
from typing import Any

import yaml

from api.chat.language import detect_language
from api.llm.client import LLMResponse, Usage
from api.llm.fake import FakeLLMClient
from api.seed.personas import PERSONAS
from evals.__main__ import build_graders, grader_name
from evals.dataset import EvalCase, load_cases
from evals.runner import run_cases

ROOT_DIR = Path(__file__).resolve().parents[3]
DATASETS_DIR = ROOT_DIR / "evals" / "datasets"
PHASE_14 = DATASETS_DIR / "phase_14.jsonl"
ADR_0014 = ROOT_DIR / "docs" / "adr" / "0014-observability.md"
MANIFEST = ROOT_DIR / "corpus" / "manifest.yaml"
MIN_NON_SMOKE = 90
MAX_NON_SMOKE = 130


def _phase_14_cases() -> list[EvalCase]:
    return [case for case in load_cases([PHASE_14]) if case.phase == 14]


def _safe_doc_keys() -> set[str]:
    manifest: dict[str, Any] = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    return {doc["doc_key"] for doc in manifest["documents"] if not doc["malicious"]}


class CitingTarget:
    """A chat output that cites the expected document, as a passing run would produce."""

    async def run(self, case: EvalCase) -> dict[str, Any]:
        return {
            "text": "On the Essential tier you pay USD 6.90 shipping per box. [1]",
            "status": "completed",
            "language": case.language,
            "trajectory": [],
            "citations": [case.expected["citation_doc_key"]],
            "chunks": ["On the Essential tier you pay USD 6.90 shipping per box."],
            "triage": None,
            "error": None,
        }


def _judge_reply() -> LLMResponse:
    return LLMResponse(
        text=json.dumps({"faithful": True, "score": 1.0, "rationale": "Supported."}),
        tool_calls=[],
        usage=Usage(input_tokens=1, output_tokens=1),
        model="fake-model",
        provider="fake",
        stop_reason="end_turn",
    )


def test_phase_14_has_rag_cases_in_pt_and_en() -> None:
    cases = _phase_14_cases()
    personas = {persona.username: persona.language for persona in PERSONAS}

    assert len(cases) >= 2
    assert {case.language for case in cases} == {"pt", "en"}
    assert len({case.id for case in cases}) == len(cases)
    for case in cases:
        assert case.id.startswith(f"phase14-rag-{case.language}-"), case.id
        assert case.category == "rag", case.id
        assert case.grader == "rag", case.id
        assert set(case.input) == {"message", "persona"}, case.id
        assert personas[case.input["persona"]] == case.language, case.id
        message = case.input["message"]
        assert detect_language(message, fallback=case.language) == case.language, case.id


def test_phase_14_cases_cite_a_real_safe_corpus_document() -> None:
    doc_keys = _safe_doc_keys()

    for case in _phase_14_cases():
        assert set(case.expected) == {"citation_doc_key"}, case.id
        assert case.expected["citation_doc_key"] in doc_keys, case.id


def test_phase_14_cases_go_to_the_default_rag_grader() -> None:
    assert {grader_name(case) for case in _phase_14_cases()} == {"rag"}


async def test_phase_14_cases_pass_when_the_expected_document_is_cited() -> None:
    cases = _phase_14_cases()
    judge = FakeLLMClient(script=[_judge_reply() for _ in cases])

    report = await run_cases(cases, CitingTarget(), build_graders(judge))

    assert report.status == "ok"
    assert all(result.passed for result in report.cases), report.cases


def test_all_datasets_keep_the_non_smoke_total_within_the_limits() -> None:
    cases = load_cases(sorted(DATASETS_DIR.glob("*.jsonl")))

    assert {case.phase for case in cases} >= set(range(1, 15))
    total = len([case for case in cases if case.category != "smoke"])
    assert MIN_NON_SMOKE <= total <= MAX_NON_SMOKE, total


def test_adr_0014_follows_the_template_in_english() -> None:
    text = ADR_0014.read_text(encoding="utf-8")

    assert text.startswith("# ADR 0014:")
    assert "- Phase: 14" in text
    for heading in ("## Context", "## Decision", "## Alternatives considered", "## Consequences"):
        assert heading in text
    for topic in (
        "OpenTelemetry",
        "OTLP",
        "Langfuse",
        "/api/public/otel/v1/traces",
        "X-Trace-Id",
        "trace_id",
        "langfuse.session.id",
        "conversation",
        "mask_pii",
        "best effort",
        "telemetry_dropped",
        "score",
        "-1",
        "MESSAGE_NOT_FOUND",
        "retrieval",
        "phase_14.jsonl",
        "demo_14",
        "profile",
        "Langfuse Cloud",
        "SDK",
    ):
        assert topic in text, topic
