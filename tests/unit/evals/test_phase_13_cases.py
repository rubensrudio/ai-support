"""Phase 13 eval cases: tone cases in pt and en graded by the LLM-as-judge tone grader, and
ADR 0013 (TASK-131, DOD-01, DOD-04)."""

import json
from pathlib import Path
from typing import Any

from api.chat.language import detect_language
from api.llm.client import LLMRequest, LLMResponse, Usage
from api.llm.fake import FakeLLMClient
from api.seed.personas import PERSONAS
from evals.__main__ import build_graders, grader_name
from evals.dataset import EvalCase, load_cases
from evals.runner import run_cases

ROOT_DIR = Path(__file__).resolve().parents[3]
DATASETS_DIR = ROOT_DIR / "evals" / "datasets"
PHASE_13 = DATASETS_DIR / "phase_13.jsonl"
ADR_0013 = ROOT_DIR / "docs" / "adr" / "0013-evals.md"
MAX_NON_SMOKE = 130


def _phase_13_cases() -> list[EvalCase]:
    return [case for case in load_cases([PHASE_13]) if case.phase == 13]


class PoliteTarget:
    """A chat output in the case language, as a passing run would produce."""

    async def run(self, case: EvalCase) -> dict[str, Any]:
        text = {
            "en": "I'm sorry about that. Here is how to fix it, step by step.",
            "pt": "Sinto muito por isso. Veja como resolver, passo a passo.",
        }[case.language]
        return {
            "text": text,
            "status": "completed",
            "language": case.language,
            "trajectory": [],
            "citations": [],
            "chunks": [],
            "triage": None,
            "error": None,
        }


def _judge_reply(score: float) -> LLMResponse:
    return LLMResponse(
        text=json.dumps({"score": score, "reason": "Polite, clear and in the right language."}),
        tool_calls=[],
        usage=Usage(input_tokens=1, output_tokens=1),
        model="fake-model",
        provider="fake",
        stop_reason="end_turn",
    )


def test_phase_13_has_tone_cases_in_pt_and_en() -> None:
    cases = _phase_13_cases()
    personas = {persona.username: persona.language for persona in PERSONAS}

    assert len(cases) >= 2
    assert {case.language for case in cases} == {"pt", "en"}
    assert len({case.id for case in cases}) == len(cases)
    for case in cases:
        assert case.id.startswith(f"phase13-tone-{case.language}-"), case.id
        assert case.category == "tone", case.id
        assert case.grader == "tone", case.id
        assert case.expected == {"tone": "polite"}, case.id
        assert personas[case.input["persona"]] == case.language, case.id
        message = case.input["message"]
        assert detect_language(message, fallback=case.language) == case.language, case.id


def test_phase_13_cases_go_to_the_tone_judge() -> None:
    assert {grader_name(case) for case in _phase_13_cases()} == {"tone"}


async def test_phase_13_cases_pass_for_a_polite_answer_and_send_it_to_the_judge() -> None:
    cases = _phase_13_cases()
    judge = FakeLLMClient(script=[_judge_reply(0.9) for _ in cases])

    report = await run_cases(cases, PoliteTarget(), build_graders(judge))

    assert report.status == "ok"
    assert all(result.passed and result.score == 90.0 for result in report.cases)
    requests: list[LLMRequest] = judge.requests
    assert len(requests) == len(cases)
    for case, request in zip(cases, requests, strict=True):
        content = request.messages[-1].content
        assert f"<expected_language>{case.language}</expected_language>" in content
        assert case.input["message"] in content


def test_all_datasets_load_together_and_stay_within_the_size_limit() -> None:
    cases = load_cases(sorted(DATASETS_DIR.glob("*.jsonl")))

    assert {case.phase for case in cases} >= set(range(1, 14))
    assert len([case for case in cases if case.category != "smoke"]) <= MAX_NON_SMOKE


def test_adr_0013_follows_the_template_in_english() -> None:
    text = ADR_0013.read_text(encoding="utf-8")

    assert text.startswith("# ADR 0013:")
    assert "- Phase: 13" in text
    for heading in ("## Context", "## Decision", "## Alternatives considered", "## Consequences"):
        assert heading in text
    for topic in (
        "0 to 100",
        "LAC-22",
        "LAC-33",
        "eval-report",
        "default branch",
        "2.0 points",
        "EVAL-91",
        "EVAL-92",
        "baseline_created",
        "by category",
        "reported only",
        "EVAL-90",
        "exit 2",
        "status='error'",
        "ANTHROPIC_API_KEY",
        "skipped",
        "required check",
        "prompts/**",
        "api/**",
        "--target auto",
        "LLM-as-judge",
        "deterministic",
        "demo_13",
        "citation rule",
    ):
        assert topic in text, topic
