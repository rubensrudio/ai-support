"""Phase 06 eval cases: tool trajectories of order requests in pt and en (TASK-082)."""

import re
from pathlib import Path
from typing import Any

from api.seed.personas import PERSONAS
from api.tools.catalog import DESTRUCTIVE_TOOLS, TOOL_NAMES
from api.tools.models import OrderStatus
from evals.dataset import EvalCase, load_cases
from evals.runner import DEFAULT_GRADERS, run_cases

ROOT_DIR = Path(__file__).resolve().parents[3]
DATASETS_DIR = ROOT_DIR / "evals" / "datasets"
PHASE_06 = DATASETS_DIR / "phase_06.jsonl"
ADR_0006 = ROOT_DIR / "docs" / "adr" / "0006-tool-calling.md"

# Order numbers change on every commerce reset, so a case names the order by a placeholder
# `{<status>_order_id}` that the target resolves against the persona's seeded orders.
_PLACEHOLDER = re.compile(r"\{(\w+)_order_id\}")


class ExpectedTarget:
    """Returns the expected fields, as a passing tool-calling chat run would."""

    async def run(self, case: EvalCase) -> dict[str, Any]:
        return {**case.expected, "content": "Done."}


def _phase_06_cases() -> list[EvalCase]:
    return [case for case in load_cases([PHASE_06]) if case.phase == 6]


def test_phase_06_has_tool_trajectory_cases_in_pt_and_en() -> None:
    cases = _phase_06_cases()

    assert len(cases) >= 2
    assert {case.language for case in cases} == {"pt", "en"}
    assert len({case.id for case in cases}) == len(cases)
    for case in cases:
        assert case.category == "tool_trajectory", case.id
        assert case.input["message"].strip(), case.id
        assert case.input["ui_language"] == case.language, case.id
        assert case.expected["language"] == case.language, case.id
        assert case.id.startswith(f"phase06-tool-{case.language}-"), case.id


def test_phase_06_cases_log_in_as_the_persona_of_their_language() -> None:
    personas = {persona.username: persona.language for persona in PERSONAS}

    for case in _phase_06_cases():
        assert personas.get(case.input["persona"]) == case.language, case.id


def test_phase_06_trajectories_use_catalog_tools_and_confirm_destructive_ones() -> None:
    for case in _phase_06_cases():
        calls = case.expected["tool_calls"]
        assert calls and set(calls) <= set(TOOL_NAMES), case.id
        destructive = [name for name in calls if name in DESTRUCTIVE_TOOLS]
        if destructive:
            # The loop suspends at the first destructive call (TOOL-05).
            assert calls[-1] == destructive[0], case.id
            assert case.expected["status"] == "awaiting_confirmation", case.id
            assert case.expected["confirmation_tool"] == destructive[0], case.id
        else:
            assert case.expected["status"] == "completed", case.id
            assert "confirmation_tool" not in case.expected, case.id


def test_phase_06_cancel_cases_target_a_cancellable_order_by_placeholder() -> None:
    statuses = {status.value for status in OrderStatus}
    cancel_languages = set()
    for case in _phase_06_cases():
        message = case.input["message"]
        assert not re.search(r"#\d", message), f"{case.id} hard-codes an order number"
        for status in _PLACEHOLDER.findall(message):
            assert status in statuses, case.id
        if case.expected.get("confirmation_tool") == "cancel_order":
            assert _PLACEHOLDER.findall(message) == ["paid"], case.id
            cancel_languages.add(case.language)

    assert cancel_languages == {"pt", "en"}


def test_phase_06_cases_use_existing_graders() -> None:
    for case in load_cases([PHASE_06]):
        assert case.grader in DEFAULT_GRADERS, case.id


def test_all_datasets_load_together_including_phase_06() -> None:
    cases = load_cases(sorted(DATASETS_DIR.glob("*.jsonl")))

    assert {case.phase for case in cases} >= {1, 2, 3, 4, 5, 6}


async def test_phase_06_cases_pass_for_a_matching_output() -> None:
    report = await run_cases(load_cases([PHASE_06]), ExpectedTarget(), DEFAULT_GRADERS)

    assert report.status == "ok"
    assert all(result.passed for result in report.cases)


def test_adr_0006_follows_the_template_in_english() -> None:
    text = ADR_0006.read_text(encoding="utf-8")

    assert text.startswith("# ADR 0006:")
    for heading in ("## Context", "## Decision", "## Alternatives considered", "## Consequences"):
        assert heading in text
    for topic in (
        "ToolContext",
        "IDENTITY_FIELDS",
        "pending_confirmations",
        "agent_state",
        "evaluate_refund",
        "FOR UPDATE",
        "RETURNING",
        "audit_log",
        "claude_cli",
        "ATLAS_AGENT_TOOLS_ENABLED",
        "demo_06",
    ):
        assert topic in text, topic
    assert "- Phase: 06" in text
