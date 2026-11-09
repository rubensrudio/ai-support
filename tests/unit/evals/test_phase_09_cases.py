"""Phase 09 eval cases: SSE event order in pt and en (TASK-099)."""

import typing
from pathlib import Path
from typing import Any

from api.agent.events import AgentEvent
from api.seed.personas import PERSONAS
from api.tools.catalog import TOOL_NAMES
from evals.dataset import EvalCase, load_cases
from evals.runner import DEFAULT_GRADERS, run_cases

ROOT_DIR = Path(__file__).resolve().parents[3]
DATASETS_DIR = ROOT_DIR / "evals" / "datasets"
PHASE_09 = DATASETS_DIR / "phase_09.jsonl"
ADR_0009 = ROOT_DIR / "docs" / "adr" / "0009-streaming.md"
TERMINAL = ("done", "error")

Event = tuple[str, dict[str, Any]]


def _event_types() -> set[str]:
    """The `type` literals of the CT-45 event union."""
    union = typing.get_args(AgentEvent)[0]
    return {model.model_fields["type"].default for model in typing.get_args(union)}


def _is_subsequence(required: list[str], names: list[str]) -> bool:
    remaining = iter(names)
    return all(any(name == wanted for name in remaining) for wanted in required)


def conforms(events: list[Event], expected: dict[str, Any]) -> bool:
    """Reference check of a parsed SSE stream against a phase 09 case's `expected`.

    Only the relative order of `required_order` is fixed: tokens may precede a tool call (the
    loop streams text that comes before tool calls) and `citation` events are optional.
    """
    names = [name for name, _ in events]
    if not names or names[0] != expected["first_event"] or names[-1] != expected["last_event"]:
        return False
    if names.count("start") != expected["start_count"]:
        return False
    if names.count("done") != expected["done_count"] or "error" in names:
        return False
    if not set(names) <= set(expected["allowed_events"]):
        return False
    if not _is_subsequence(expected["required_order"], names):
        return False
    starts = [(i, data) for i, (name, data) in enumerate(events) if name == "tool_start"]
    if [data["tool"] for _, data in starts] != expected["tools"]:
        return False
    for index, data in starts:  # every tool_start is closed by its tool_end
        ends = [i for i, (name, end) in enumerate(events) if name == "tool_end"]
        if not any(i > index and events[i][1]["call_id"] == data["call_id"] for i in ends):
            return False
    done = events[-1][1]
    if done["status"] != expected["status"] or done["language"] != expected["language"]:
        return False
    tokens = "".join(data["text"] for name, data in events if name == "token")
    if expected.get("tokens_equal_content"):
        return bool(tokens) and tokens == done["content"]
    if expected.get("tokens_end_with_content"):
        return bool(done["content"]) and tokens.endswith(done["content"])
    return True


def _start() -> Event:
    return ("start", {"type": "start", "conversation_id": None, "trace_id": "t"})


def _token(text: str) -> Event:
    return ("token", {"type": "token", "text": text})


def _tool(name: str, step: str) -> Event:
    return (step, {"type": step, "call_id": "call-1", "tool": name})


def _citation() -> Event:
    return ("citation", {"type": "citation", "index": 1, "document": "plans.md"})


def _done(content: str, language: str) -> Event:
    return (
        "done",
        {"type": "done", "status": "completed", "content": content, "language": language},
    )


class ExpectedTarget:
    """Returns the expected fields, as a passing streaming run would."""

    async def run(self, case: EvalCase) -> dict[str, Any]:
        return dict(case.expected)


def _phase_09_cases() -> list[EvalCase]:
    return [case for case in load_cases([PHASE_09]) if case.phase == 9]


def _cases(category: str) -> list[EvalCase]:
    return [case for case in _phase_09_cases() if case.category == category]


def test_phase_09_has_event_order_cases_in_pt_and_en() -> None:
    cases = _phase_09_cases()
    personas = {persona.username: persona.language for persona in PERSONAS}

    assert len(cases) >= 2
    assert {case.language for case in cases} == {"pt", "en"}
    assert len({case.id for case in cases}) == len(cases)
    for case in cases:
        assert case.id.startswith(f"phase09-stream-{case.language}-"), case.id
        assert case.category in ("smoke", "tool_trajectory"), case.id
        assert case.input["endpoint"] == "/chat/stream", case.id
        assert personas[case.input["persona"]] == case.language, case.id
        assert case.input["ui_language"] == case.language, case.id
        assert case.input["message"].strip(), case.id
    # Both a plain answer and a tool step are covered in each language.
    for language in ("pt", "en"):
        categories = {case.category for case in cases if case.language == language}
        assert categories == {"smoke", "tool_trajectory"}, language


def test_phase_09_cases_start_with_start_and_end_with_exactly_one_done() -> None:
    known = _event_types()

    for case in _phase_09_cases():
        expected = case.expected
        assert set(expected["allowed_events"]) <= known, case.id
        assert set(expected["required_order"]) <= set(expected["allowed_events"]), case.id
        assert "error" not in expected["allowed_events"], case.id
        # The `start` event is always first and comes once (CT-45).
        assert expected["first_event"] == "start" and expected["start_count"] == 1, case.id
        assert expected["required_order"][0] == "start", case.id
        # STR-05: exactly one terminal event, `done`, and it is the last one.
        assert expected["last_event"] == "done" and expected["done_count"] == 1, case.id
        assert [e for e in expected["required_order"] if e in TERMINAL] == ["done"], case.id
        assert expected["required_order"][-1] == "done", case.id
        # Citations are optional: allowed, never required.
        assert "citation" in expected["allowed_events"], case.id
        assert "citation" not in expected["required_order"], case.id
        assert expected["status"] == "completed", case.id
        assert expected["language"] == case.language, case.id


def test_phase_09_tool_cases_fix_only_the_relative_order_of_the_tool_step() -> None:
    for case in _cases("tool_trajectory"):
        expected = case.expected
        assert expected["tools"] and set(expected["tools"]) <= set(TOOL_NAMES), case.id
        assert expected["required_order"] == ["start", "tool_start", "tool_end", "done"]
        assert {"token", "tool_start", "tool_end"} <= set(expected["allowed_events"]), case.id
        # `done.content` is only the last model call's text: tokens end with it.
        assert "tokens_equal_content" not in expected, case.id
        assert expected["tokens_end_with_content"] is True, case.id


def test_phase_09_smoke_cases_stream_the_whole_answer_without_tools() -> None:
    for case in _cases("smoke"):
        expected = case.expected
        assert expected["tools"] == [], case.id
        assert expected["required_order"] == ["start", "token", "done"], case.id
        assert not {"tool_start", "tool_end"} & set(expected["allowed_events"]), case.id
        assert expected["tokens_equal_content"] is True, case.id


def test_tool_cases_accept_every_stream_the_contract_allows() -> None:
    for case in _cases("tool_trajectory"):
        tool = case.expected["tools"][0]
        language = case.language
        step = [_tool(tool, "tool_start"), _tool(tool, "tool_end")]
        answer = [_token("Your plan "), _token("is Pro.")]
        done = _done("Your plan is Pro.", language)

        # Text before the tool call, the tool step, the answer and an optional citation.
        assert conforms(
            [_start(), _token("Checking. "), *step, *answer, _citation(), done], case.expected
        ), case.id
        assert conforms([_start(), *step, *answer, done], case.expected), case.id
        # Not conforming: no start first, tool_end before tool_start, a second done, an
        # error, a done whose content is not the end of the tokens.
        assert not conforms([*step, _start(), *answer, done], case.expected), case.id
        assert not conforms([_start(), *step[::-1], *answer, done], case.expected), case.id
        assert not conforms([_start(), *step, *answer, done, done], case.expected), case.id
        error = ("error", {"type": "error", "code": "LLM_UNAVAILABLE"})
        assert not conforms([_start(), *step, error], case.expected), case.id
        assert not conforms([_start(), *step, *answer, _done("Other", language)], case.expected), (
            case.id
        )


def test_smoke_cases_accept_tokens_and_an_optional_citation() -> None:
    for case in _cases("smoke"):
        language = case.language
        tokens = [_token("Yes, "), _token("you can.")]
        done = _done("Yes, you can.", language)

        assert conforms([_start(), *tokens, _citation(), done], case.expected), case.id
        assert conforms([_start(), *tokens, done], case.expected), case.id
        assert not conforms([*tokens, done], case.expected), case.id
        assert not conforms([_start(), *tokens, done, _token("!")], case.expected), case.id
        assert not conforms([_start(), _token("Yes"), done], case.expected), case.id
        step = [_tool("get_order", "tool_start"), _tool("get_order", "tool_end")]
        assert not conforms([_start(), *step, *tokens, done], case.expected), case.id


def test_phase_09_cases_use_existing_graders() -> None:
    for case in load_cases([PHASE_09]):
        assert case.grader in DEFAULT_GRADERS, case.id


def test_all_datasets_load_together_including_phase_09() -> None:
    cases = load_cases(sorted(DATASETS_DIR.glob("*.jsonl")))

    assert {case.phase for case in cases} >= {1, 2, 3, 4, 5, 6, 7, 8, 9}


async def test_phase_09_cases_pass_for_a_matching_output() -> None:
    report = await run_cases(load_cases([PHASE_09]), ExpectedTarget(), DEFAULT_GRADERS)

    assert report.status == "ok"
    assert all(result.passed for result in report.cases)


def test_adr_0009_follows_the_template_in_english() -> None:
    text = ADR_0009.read_text(encoding="utf-8")

    assert text.startswith("# ADR 0009:")
    assert "- Phase: 09" in text
    for heading in ("## Context", "## Decision", "## Alternatives considered", "## Consequences"):
        assert heading in text
    for topic in (
        "POST /chat/stream",
        "/chat/confirmations/{id}/stream",
        "text/event-stream",
        "fetch",
        "EventSource",
        "AbortController",
        "encode_sse",
        "guard_terminal",
        "stream_or_complete",
        "claude_cli",
        "codex_cli",
        "cancelled",
        "is_disconnected",
        "X-Accel-Buffering",
        "TTFT",
        "demo_09",
        "WebSocket",
        "StartEvent",
        "`start`",
        "citation*",
    ):
        assert topic in text, topic
