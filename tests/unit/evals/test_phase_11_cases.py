"""Phase 11 eval cases: the reply language stays right with the `cache_ordered` layout, in pt
and en (TASK-113)."""

from pathlib import Path
from typing import Any

from api.chat.context import HistoryWindow
from api.chat.language import detect_language
from api.chat.prompt_builder import build_request, grounded_system_prompt, language_instruction
from api.llm.client import ChatMessage, LLMRequest, ToolSpec
from api.seed.personas import PERSONAS
from evals.dataset import EvalCase, load_cases
from evals.runner import DEFAULT_GRADERS, run_cases

ROOT_DIR = Path(__file__).resolve().parents[3]
DATASETS_DIR = ROOT_DIR / "evals" / "datasets"
PHASE_11 = DATASETS_DIR / "phase_11.jsonl"
ADR_0011 = ROOT_DIR / "docs" / "adr" / "0011-prompt-cache.md"
TOOLS = [ToolSpec("get_subscription", "Read the plan.", {"type": "object", "properties": {}})]


class ExpectedTarget:
    """Returns the expected fields, as a passing run would."""

    async def run(self, case: EvalCase) -> dict[str, Any]:
        return dict(case.expected)


def _phase_11_cases() -> list[EvalCase]:
    return [case for case in load_cases([PHASE_11]) if case.phase == 11]


def _request(case: EvalCase) -> LLMRequest:
    """The chat request of the case's turn, in its layout (history as alternating turns)."""
    history = [
        ChatMessage("user" if i % 2 == 0 else "assistant", text)
        for i, text in enumerate(case.input["history"])
    ]
    return build_request(
        language=case.expected["language"],
        history=HistoryWindow(summary=None, messages=history, tokens=0, summarized_upto=None),
        chunks=[],
        user_message=case.input["message"],
        tools=TOOLS,
        layout=case.input["layout"],
    )


def test_phase_11_has_language_regression_cases_in_pt_and_en() -> None:
    cases = _phase_11_cases()
    personas = {persona.username: persona.language for persona in PERSONAS}

    assert len(cases) >= 2
    assert {case.language for case in cases} == {"pt", "en"}
    assert len({case.id for case in cases}) == len(cases)
    for case in cases:
        assert case.id.startswith(f"phase11-cache-{case.language}-"), case.id
        assert case.category == "tone", case.id
        assert case.input["endpoint"] == "/chat", case.id
        assert case.input["layout"] == "cache_ordered", case.id
        assert personas[case.input["persona"]] == case.language, case.id
        assert case.expected == {"status": "completed", "language": case.language}, case.id


def test_cases_include_a_turn_that_switches_from_the_history_language() -> None:
    """The reply follows the language of the current message, not of the conversation."""
    for language in ("pt", "en"):
        switched = [
            case
            for case in _phase_11_cases()
            if case.language == language
            and any(
                detect_language(text, fallback=language) != language
                for text in case.input["history"]
            )
        ]
        assert switched, language


def test_every_message_is_detected_in_the_expected_language() -> None:
    for case in _phase_11_cases():
        message = case.input["message"]
        assert detect_language(message, fallback=case.language) == case.language, case.id


def test_the_language_goes_after_an_identical_stable_prefix() -> None:
    """CACHE-02 / DA-23: same prefix bytes for every case; the language comes after it."""
    requests = {case.id: _request(case) for case in _phase_11_cases()}
    prefixes = {request.system[0].text for request in requests.values()}

    assert prefixes == {grounded_system_prompt()}
    for case in _phase_11_cases():
        request = requests[case.id]
        assert request.system[0].cache_breakpoint, case.id
        assert not any(block.cache_breakpoint for block in request.system[1:]), case.id
        stable = request.system[0].text
        assert all(language_instruction(lang) not in stable for lang in ("pt", "en")), case.id
        assert language_instruction(case.language) in request.system[1].text, case.id
        assert case.input["message"] in request.messages[-1].content, case.id


def test_phase_11_cases_use_existing_graders() -> None:
    for case in load_cases([PHASE_11]):
        assert case.grader in DEFAULT_GRADERS, case.id


def test_all_datasets_load_together_including_phase_11() -> None:
    cases = load_cases(sorted(DATASETS_DIR.glob("*.jsonl")))

    assert {case.phase for case in cases} >= set(range(1, 12))


async def test_phase_11_cases_pass_for_a_matching_output() -> None:
    report = await run_cases(load_cases([PHASE_11]), ExpectedTarget(), DEFAULT_GRADERS)

    assert report.status == "ok"
    assert all(result.passed for result in report.cases)


def test_adr_0011_follows_the_template_in_english() -> None:
    text = ADR_0011.read_text(encoding="utf-8")

    assert text.startswith("# ADR 0011:")
    assert "- Phase: 11" in text
    for heading in ("## Context", "## Decision", "## Alternatives considered", "## Consequences"):
        assert heading in text
    for topic in (
        "cache_ordered",
        "naive",
        "ATLAS_PROMPT_LAYOUT",
        "cache_breakpoint",
        "cache_control",
        "grounded_system_prompt",
        "language_instruction",
        "DA-23",
        "date and time",
        "cache_read_tokens",
        "cache_write_tokens",
        "estimate_cost",
        "llm_call",
        "CACHE-90",
        "demo_11",
        "claude_cli",
    ):
        assert topic in text, topic
