"""Phase 10 eval cases: red-team attacks blocked at the input or at the output, in pt and en
(TASK-109)."""

import uuid
from pathlib import Path
from typing import Any

from api.config import Settings
from api.guardrails.input_guard import InputGuard
from api.guardrails.output_guard import UNFULFILLED_PROMISE, OutputContext, OutputGuard
from api.guardrails.patterns import find_injection
from api.llm.fake import FakeLLMClient
from api.seed.personas import PERSONAS
from evals.dataset import EvalCase, load_cases
from evals.runner import DEFAULT_GRADERS, run_cases

ROOT_DIR = Path(__file__).resolve().parents[3]
DATASETS_DIR = ROOT_DIR / "evals" / "datasets"
PHASE_10 = DATASETS_DIR / "phase_10.jsonl"
ADR_0010 = ROOT_DIR / "docs" / "adr" / "0010-guardrails.md"


class ExpectedTarget:
    """Returns the expected fields, as a passing guarded run would."""

    async def run(self, case: EvalCase) -> dict[str, Any]:
        return dict(case.expected)


def _phase_10_cases() -> list[EvalCase]:
    return [case for case in load_cases([PHASE_10]) if case.phase == 10]


def _stage(stage: str) -> list[EvalCase]:
    return [case for case in _phase_10_cases() if case.expected["stage"] == stage]


def _settings() -> Settings:
    return Settings(_env_file=None)


def test_phase_10_has_red_team_cases_in_pt_and_en() -> None:
    cases = _phase_10_cases()
    personas = {persona.username: persona.language for persona in PERSONAS}

    assert len(cases) >= 2
    assert {case.language for case in cases} == {"pt", "en"}
    assert len({case.id for case in cases}) == len(cases)
    for case in cases:
        assert case.category == "red_team", case.id
        assert case.id.startswith(f"phase10-guard-{case.language}-"), case.id
        assert case.input["endpoint"] == "/chat", case.id
        assert personas[case.input["persona"]] == case.language, case.id
        assert case.input["ui_language"] == case.language, case.id
        assert case.expected["status"] == "blocked", case.id
        assert case.expected["stage"] in ("input", "output"), case.id
    # Both layers are covered in each language.
    for language in ("pt", "en"):
        stages = {case.expected["stage"] for case in cases if case.language == language}
        assert stages == {"input", "output"}, language


def test_input_stage_cases_are_blocked_by_the_input_guard() -> None:
    guard = InputGuard(_settings())

    for case in _stage("input"):
        verdict = guard.check(case.input["message"])
        assert verdict.action == "block", case.id
        assert verdict.reasons, case.id


async def test_output_stage_cases_pass_the_input_and_their_answer_is_blocked() -> None:
    input_guard = InputGuard(_settings())
    output_guard = OutputGuard(FakeLLMClient(script=[]), _settings())

    for case in _stage("output"):
        assert input_guard.check(case.input["message"]).action == "allow", case.id
        ctx = OutputContext(
            customer_id=uuid.uuid4(),
            own_order_ids={1},
            own_email="alex@example.com",
            successful_tools=set(),  # no tool ran in this turn
            question=case.input["message"],
            language=case.language,
        )
        verdict = await output_guard.check_sentence(case.input["model_answer"], ctx)
        assert (verdict.action, verdict.reason) == ("block", UNFULFILLED_PROMISE), case.id


def test_malicious_document_cases_point_at_a_document_the_ingestion_quarantines() -> None:
    documents = [case for case in _phase_10_cases() if "document" in case.input]

    assert {case.language for case in documents} == {"pt", "en"}
    for case in documents:
        text = (ROOT_DIR / case.input["document"]).read_text(encoding="utf-8")
        assert find_injection(text), case.id


def test_phase_10_cases_use_existing_graders() -> None:
    for case in load_cases([PHASE_10]):
        assert case.grader in DEFAULT_GRADERS, case.id


def test_all_datasets_load_together_including_phase_10() -> None:
    cases = load_cases(sorted(DATASETS_DIR.glob("*.jsonl")))

    assert {case.phase for case in cases} >= set(range(1, 11))


async def test_phase_10_cases_pass_for_a_matching_output() -> None:
    report = await run_cases(load_cases([PHASE_10]), ExpectedTarget(), DEFAULT_GRADERS)

    assert report.status == "ok"
    assert all(result.passed for result in report.cases)


def test_adr_0010_follows_the_template_in_english() -> None:
    text = ADR_0010.read_text(encoding="utf-8")

    assert text.startswith("# ADR 0010:")
    assert "- Phase: 10" in text
    for heading in ("## Context", "## Decision", "## Alternatives considered", "## Consequences"):
        assert heading in text
    for topic in (
        "InputGuard",
        "mask_pii",
        "find_injection",
        "untrusted_document",
        "quarantined",
        "ATLAS_INGEST_INJECTION_SCAN",
        "StreamOutputFilter",
        "OutputGuard",
        "topic_check",
        "ATLAS_GUARDRAIL_MODEL",
        "fail-closed",
        "guard_failure",
        "TTFT",
        "ATLAS_GUARDRAILS_ENABLED",
        "demo_10",
        "redteam.jsonl",
        "false positive",
    ):
        assert topic in text, topic
