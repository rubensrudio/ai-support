"""Phase 12 eval cases: the degraded answer when every LLM provider failed or every circuit is
open, in pt and en, and ADR 0012 (TASK-123, RES-06, RES-92)."""

from pathlib import Path
from typing import Any

import pytest

from api.chat.language import detect_language
from api.chat.service import ALL_PROVIDERS_FAILED
from api.llm.circuit_breaker import BreakerLLMClient, CircuitBreaker
from api.llm.client import LLMClient, LLMError, LLMRequest
from api.llm.fake import FakeLLMClient
from api.llm.fallback import AllProvidersFailed, FallbackLLMClient
from api.messages import MessageKey, user_message
from api.seed.personas import PERSONAS
from evals.dataset import EvalCase, load_cases
from evals.runner import DEFAULT_GRADERS, run_cases

ROOT_DIR = Path(__file__).resolve().parents[3]
DATASETS_DIR = ROOT_DIR / "evals" / "datasets"
PHASE_12 = DATASETS_DIR / "phase_12.jsonl"
ADR_0012 = ROOT_DIR / "docs" / "adr" / "0012-retry-fallback.md"
PROVIDER_STATES = ("fail", "circuit_open")
CHAIN = ("primary", "secondary")


class ExpectedTarget:
    """Returns the expected fields, as a passing run would."""

    async def run(self, case: EvalCase) -> dict[str, Any]:
        return dict(case.expected)


def _phase_12_cases() -> list[EvalCase]:
    return [case for case in load_cases([PHASE_12]) if case.phase == 12]


def _chain(case: EvalCase) -> tuple[FallbackLLMClient, dict[str, FakeLLMClient]]:
    """The chain of the case: each provider fails with a 503 or has its circuit open."""
    fakes: dict[str, FakeLLMClient] = {}
    chain: list[tuple[str, LLMClient, CircuitBreaker]] = []
    for name in CHAIN:
        fake = FakeLLMClient(script=[LLMError("unavailable", recoverable=True, status=503)])
        breaker = CircuitBreaker(name, failure_threshold=1, reset_seconds=60)
        if case.input["providers"][name] == "circuit_open":
            breaker.record_failure()
        fakes[name] = fake
        chain.append((name, BreakerLLMClient(fake, breaker), breaker))
    return FallbackLLMClient(chain), fakes


def test_phase_12_has_degraded_answer_cases_in_pt_and_en() -> None:
    cases = _phase_12_cases()
    personas = {persona.username: persona.language for persona in PERSONAS}

    assert len(cases) >= 2
    assert {case.language for case in cases} == {"pt", "en"}
    assert len({case.id for case in cases}) == len(cases)
    for case in cases:
        assert case.id.startswith(f"phase12-degraded-{case.language}-"), case.id
        assert case.category == "smoke", case.id
        assert case.input["endpoint"] == "/chat", case.id
        assert personas[case.input["persona"]] == case.language, case.id
        assert case.expected["status"] == "degraded", case.id
        assert case.expected["language"] == case.language, case.id
        assert case.expected["ticket_reason"] == ALL_PROVIDERS_FAILED, case.id


def test_expected_content_is_the_degraded_message_of_the_case_language() -> None:
    """RES-06: 'try again in a moment' plus the notice that a ticket was opened."""
    for case in _phase_12_cases():
        expected = user_message(MessageKey.DEGRADED_RESPONSE, case.language)
        assert case.expected["content"] == expected, case.id
        message = case.input["message"]
        assert detect_language(message, fallback=case.language) == case.language, case.id


def test_cases_cover_failed_providers_and_open_circuits_in_both_languages() -> None:
    """RES-06 (both providers fail) and RES-92 (every circuit open), in pt and en."""
    for language in ("pt", "en"):
        states = {
            case.input["providers"]["primary"]
            for case in _phase_12_cases()
            if case.language == language
        }
        assert states == set(PROVIDER_STATES), language


@pytest.mark.parametrize("case", _phase_12_cases(), ids=lambda case: case.id)
async def test_the_chain_of_each_case_ends_in_all_providers_failed(case: EvalCase) -> None:
    """The chain raises `AllProvidersFailed` (the chat answers degraded on it) after calling
    exactly the providers the case expects: none when every circuit is open (RES-92)."""
    chain, fakes = _chain(case)

    with pytest.raises(AllProvidersFailed):
        await chain.complete(LLMRequest(system=[], messages=[]))

    called = [name for name in CHAIN if fakes[name].requests]
    assert called == case.expected["providers_called"], case.id


def test_phase_12_cases_use_existing_graders() -> None:
    for case in load_cases([PHASE_12]):
        assert case.grader in DEFAULT_GRADERS, case.id


def test_all_datasets_load_together_including_phase_12() -> None:
    cases = load_cases(sorted(DATASETS_DIR.glob("*.jsonl")))

    assert {case.phase for case in cases} >= set(range(1, 13))


async def test_phase_12_cases_pass_for_a_matching_output() -> None:
    report = await run_cases(load_cases([PHASE_12]), ExpectedTarget(), DEFAULT_GRADERS)

    assert report.status == "ok"
    assert all(result.passed for result in report.cases)


def test_adr_0012_follows_the_template_in_english() -> None:
    text = ADR_0012.read_text(encoding="utf-8")

    assert text.startswith("# ADR 0012:")
    assert "- Phase: 12" in text
    for heading in ("## Context", "## Decision", "## Alternatives considered", "## Consequences"):
        assert heading in text
    for topic in (
        "recoverable",
        "jitter",
        "timeout",
        "cli_timeout_seconds",
        "claude_cli",
        "CircuitBreaker",
        "per provider",
        "per process",
        "DA-24",
        "first token",
        "LAC-29",
        "RES-93",
        "AllProvidersFailed",
        "DEGRADED_RESPONSE",
        "ticket",
        "RES-91",
        "CHAOS_RATE",
        "demo_12",
        "max_retries=0",
    ):
        assert topic in text, topic
