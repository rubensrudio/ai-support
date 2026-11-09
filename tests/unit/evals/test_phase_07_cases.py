"""Phase 07 eval cases: multi-step agent trajectories in pt and en (TASK-086)."""

import re
from pathlib import Path
from typing import Any

from api.seed.personas import PERSONAS
from api.tools.catalog import TOOL_NAMES
from evals.dataset import EvalCase, load_cases
from evals.runner import DEFAULT_GRADERS, run_cases

ROOT_DIR = Path(__file__).resolve().parents[3]
DATASETS_DIR = ROOT_DIR / "evals" / "datasets"
PHASE_07 = DATASETS_DIR / "phase_07.jsonl"
ADR_0007 = ROOT_DIR / "docs" / "adr" / "0007-agent-loop.md"

AGT_06_TRAJECTORY = [
    "list_orders",
    "get_order",
    "check_refund_policy",
    "cancel_order",
    "issue_refund",
    "send_email",
]


class ExpectedTarget:
    """Returns the expected fields, as a passing multi-step agent run would."""

    async def run(self, case: EvalCase) -> dict[str, Any]:
        return {**case.expected, "content": "Done."}


def _phase_07_cases() -> list[EvalCase]:
    return [case for case in load_cases([PHASE_07]) if case.phase == 7]


def test_phase_07_has_six_tool_trajectory_cases_in_pt_and_en() -> None:
    cases = _phase_07_cases()

    assert len(cases) >= 2
    assert {case.language for case in cases} == {"pt", "en"}
    assert len({case.id for case in cases}) == len(cases)
    six_tools = [case for case in cases if len(case.expected["trajectory"]) == 6]
    assert {case.language for case in six_tools} == {"pt", "en"}
    for case in cases:
        assert case.category == "tool_trajectory", case.id
        assert case.id.startswith(f"phase07-agent-{case.language}-"), case.id
        assert case.input["message"].strip(), case.id
        assert case.input["ui_language"] == case.language, case.id
        assert case.expected["language"] == case.language, case.id


def test_phase_07_six_tool_cases_follow_the_agt_06_trajectory_to_completion() -> None:
    for case in _phase_07_cases():
        trajectory = case.expected["trajectory"]
        assert set(trajectory) <= set(TOOL_NAMES), case.id
        if len(trajectory) == 6:
            assert trajectory == AGT_06_TRAJECTORY, case.id
            # Both destructive steps are confirmed, so the request ends completed.
            assert case.expected["status"] == "completed", case.id
            assert case.expected["confirmations"] == ["cancel_order", "issue_refund"], case.id


def test_phase_07_cases_log_in_as_the_persona_of_their_language() -> None:
    personas = {persona.username: persona.language for persona in PERSONAS}

    for case in _phase_07_cases():
        assert personas.get(case.input["persona"]) == case.language, case.id
        assert not re.search(r"#\d", case.input["message"]), case.id


def test_phase_07_cases_use_existing_graders() -> None:
    for case in load_cases([PHASE_07]):
        assert case.grader in DEFAULT_GRADERS, case.id


def test_all_datasets_load_together_including_phase_07() -> None:
    cases = load_cases(sorted(DATASETS_DIR.glob("*.jsonl")))

    assert {case.phase for case in cases} >= {1, 2, 3, 4, 5, 6, 7}


async def test_phase_07_cases_pass_for_a_matching_output() -> None:
    report = await run_cases(load_cases([PHASE_07]), ExpectedTarget(), DEFAULT_GRADERS)

    assert report.status == "ok"
    assert all(result.passed for result in report.cases)


def test_adr_0007_follows_the_template_in_english() -> None:
    text = ADR_0007.read_text(encoding="utf-8")

    assert text.startswith("# ADR 0007:")
    assert "- Phase: 07" in text
    for heading in ("## Context", "## Decision", "## Alternatives considered", "## Consequences"):
        assert heading in text
    for topic in (
        "AgentLoop",
        "StepBudget",
        "CostBudget",
        "RepeatDetector",
        "ATLAS_AGENT_MAX_STEPS",
        "ATLAS_AGENT_MAX_COST_USD",
        "unknown_calls",
        "claude_cli",
        "codex_cli",
        "repeat_error",
        "AGENT_LIMIT_REACHED",
        "agent_limit",
        "agent_repeat_detected",
        "audit_log",
        "demo_07",
    ):
        assert topic in text, topic
