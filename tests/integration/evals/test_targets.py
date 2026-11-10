"""In-process eval targets for chat and triage (TASK-126, EVAL-01, EVAL-02).

The chat target runs against the pytest session's database (`atlas_test_<uuid>`), seeding the
personas and the commerce data itself; the LLM is a scripted fake, never a real provider.
"""

import json
import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from api.config import Settings, get_settings
from api.db.engine import dispose_engine, reset_engine
from api.llm.client import (
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    ToolCall,
    Usage,
)
from api.llm.factory import reset_breakers
from api.llm.fake import FAKE_MODEL, FAKE_PROVIDER, FakeLLMClient
from api.llm.fallback import AllProvidersFailed
from evals import __main__ as cli
from evals.dataset import EvalCase
from evals.graders import json_valid, tool_trajectory
from evals.targets import AutoTarget, ChatTarget, TriageTarget

pytestmark = pytest.mark.integration

_ORDER_NUMBER = re.compile(r"#(\d+)")
_ORDER_TOOLS = frozenset({"get_order", "cancel_order", "check_refund_policy", "issue_refund"})


def _response(text: str = "", calls: list[ToolCall] | None = None) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=list(calls or []),
        usage=Usage(input_tokens=0, output_tokens=0),
        model=FAKE_MODEL,
        provider=FAKE_PROVIDER,
        stop_reason="tool_use" if calls else "end_turn",
    )


class ScriptedAgent:
    """Fake agent model: one tool call per step along `tools`, then a final answer.

    Order numbers change on every commerce reseed, so the order id of an order tool is read
    from the `#<id>` the customer wrote in the request (the target resolves the placeholders).
    """

    model = FAKE_MODEL
    provider = FAKE_PROVIDER

    def __init__(self, tools: list[str], final: str = "Done: your request is complete.") -> None:
        self.tools = list(tools)
        self.final = final
        self.calls = 0

    def _order_id(self, request: LLMRequest) -> int:
        for message in request.messages:
            if message.role == "user" and (match := _ORDER_NUMBER.search(message.content)):
                return int(match.group(1))
        raise AssertionError("the request names no order number")

    def _next(self, request: LLMRequest) -> LLMResponse:
        step = self.calls
        self.calls += 1
        if step >= len(self.tools):
            return _response(self.final)
        tool = self.tools[step]
        arguments: dict[str, Any] = {}
        if tool in _ORDER_TOOLS:
            arguments = {"order_id": self._order_id(request)}
        elif tool == "list_orders":
            arguments = {"limit": 10}
        elif tool == "send_email":
            arguments = {"subject": "Your request", "body": "Here is the summary."}
        return _response(calls=[ToolCall(id=f"call-{step}", name=tool, arguments=arguments)])

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return self._next(request)

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        response = self._next(request)
        if response.text:
            yield LLMStreamEvent(kind="text_delta", text=response.text)
        for call in response.tool_calls:
            yield LLMStreamEvent(kind="tool_call", tool_call=call)


def _case(**overrides: Any) -> EvalCase:
    raw: dict[str, Any] = {
        "id": "it-case-001",
        "phase": 13,
        "category": "tool_trajectory",
        "language": "en",
        "input": {"message": "What plan am I on and when does it renew?", "persona": "alex"},
        "expected": {"trajectory": ["get_subscription"]},
        "grader": "tool_trajectory",
    }
    raw.update(overrides)
    return EvalCase.model_validate(raw)


def _triage_reply() -> LLMResponse:
    payload = {
        "categoria": "reembolso",
        "prioridade": "alta",
        "sentimento": "negativo",
        "precisa_humano": False,
        "resumo": "Cliente cobrado duas vezes pede estorno.",
    }
    return _response(json.dumps(payload, ensure_ascii=False))


@pytest.fixture
def settings(migrated_database: str) -> Iterator[Settings]:
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
async def clean_engine(migrated_database: str) -> AsyncIterator[None]:
    """Each test leaves the pooled connections closed for the next one."""
    yield
    await dispose_engine()


# --- chat target -----------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_engine")
async def test_tool_trajectory_case_runs_on_chat_target_and_returns_the_trajectory(
    settings: Settings,
) -> None:
    case = _case()
    target = ChatTarget(settings, llm=ScriptedAgent(["get_subscription"]))

    output = await target.run(case)

    assert output["error"] is None
    assert output["trajectory"] == ["get_subscription"]
    assert output["status"] == "completed"
    assert output["language"] == "en"
    assert output["text"] == "Done: your request is complete."
    assert output["triage"] is None
    assert isinstance(output["citations"], list) and isinstance(output["chunks"], list)
    grade = await tool_trajectory(case, output)
    assert grade.score == 100.0 and grade.passed


@pytest.mark.usefixtures("clean_engine")
async def test_confirm_resolves_placeholders_and_confirms_destructive_calls(
    settings: Settings,
) -> None:
    case = _case(
        id="it-case-002",
        input={"message": "Please cancel order #{pending_order_id}.", "confirm": True},
        expected={"trajectory": ["get_order", "cancel_order"]},
    )
    target = ChatTarget(settings, llm=ScriptedAgent(["get_order", "cancel_order"]))

    output = await target.run(case)

    assert output["error"] is None
    assert output["trajectory"] == ["get_order", "cancel_order"]
    assert output["status"] == "completed"


@pytest.mark.usefixtures("clean_engine")
async def test_without_confirm_the_case_stops_awaiting_confirmation(settings: Settings) -> None:
    case = _case(
        id="it-case-003",
        language="pt",
        input={"message": "Cancela o pedido #{pending_order_id}, por favor."},
        expected={"trajectory": ["cancel_order"]},
    )
    target = ChatTarget(settings, llm=ScriptedAgent(["cancel_order"]))

    output = await target.run(case)

    assert output["error"] is None
    assert output["trajectory"] == ["cancel_order"]
    assert output["status"] == "awaiting_confirmation"
    assert output["language"] == "pt"


@pytest.mark.usefixtures("clean_engine")
async def test_history_turns_are_sent_first_and_left_out_of_the_trajectory(
    settings: Settings,
) -> None:
    case = _case(
        id="it-case-004",
        category="tone",
        input={
            "history": ["My name is Rubens."],
            "message": "What is my name?",
            "ui_language": "en",
            "endpoint": "/chat",
            "recall": "Rubens",
        },
        expected={"status": "completed", "language": "en"},
        grader="exact_match",
    )
    llm = FakeLLMClient(script=[_response("Nice to meet you!"), _response("You are Rubens.")])
    target = ChatTarget(settings, llm=llm)

    output = await target.run(case)

    assert output["error"] is None
    assert output["text"] == "You are Rubens."
    assert output["trajectory"] == []
    assert len(llm.requests) == 2
    assert "My name is Rubens." in json.dumps([str(m) for m in llm.requests[1].messages])


@pytest.mark.usefixtures("clean_engine")
async def test_chat_failure_is_returned_as_error(settings: Settings) -> None:
    case = _case(id="it-case-005", input={"message": "   ", "persona": "alex"})
    target = ChatTarget(settings, llm=FakeLLMClient(script=[]))

    output = await target.run(case)

    assert output["error"] and "VALIDATION_ERROR" in output["error"]
    assert output["trajectory"] == []
    grade = await tool_trajectory(case, output)
    assert grade.score == 0.0


@pytest.mark.usefixtures("clean_engine")
async def test_unknown_persona_is_returned_as_error(settings: Settings) -> None:
    case = _case(id="it-case-006", input={"message": "Hi", "persona": "nobody"})
    target = ChatTarget(settings, llm=FakeLLMClient(script=[]))

    output = await target.run(case)

    assert output["error"] and "nobody" in output["error"]


# --- triage target ---------------------------------------------------------------------------


async def test_json_valid_case_runs_on_triage_target(settings: Settings) -> None:
    case = _case(
        id="it-triage-001",
        category="json_valid",
        language="pt",
        input={"text": "Fui cobrado duas vezes e quero o estorno.", "mode": "schema"},
        expected={"json_valid": True},
        grader="json_valid",
    )
    llm = FakeLLMClient(script=[_triage_reply()])
    target = TriageTarget(settings, llm=llm)

    output = await target.run(case)

    assert output["error"] is None
    assert output["triage"] == json.loads(_triage_reply().text)
    assert output["language"] == "pt"
    assert llm.requests[0].response_schema is not None  # mode "schema"
    grade = await json_valid(case, output)
    assert grade.score == 100.0 and grade.passed


async def test_triage_failure_returns_no_triage_and_the_error(settings: Settings) -> None:
    case = _case(
        id="it-triage-002",
        category="json_valid",
        input={"text": "My box never arrived."},
        expected={"json_valid": True},
        grader="json_valid",
    )
    target = TriageTarget(settings, llm=FakeLLMClient(script=[LLMError("down", False)]))

    output = await target.run(case)

    assert output["triage"] is None
    assert output["error"] and "TRIAGE_UNAVAILABLE" in output["error"]
    assert (await json_valid(case, output)).score == 0.0


# --- CLI ---------------------------------------------------------------------------------------


@pytest.fixture
async def fresh_engine(migrated_database: str) -> AsyncIterator[None]:
    """The CLI runs its own event loop: no pooled connection may cross into it or out of it."""
    await dispose_engine()
    reset_breakers()
    yield
    reset_engine()
    reset_breakers()


def _write_dataset(path: Path, cases: list[EvalCase]) -> Path:
    path.write_text(
        "".join(case.model_dump_json() + "\n" for case in cases),
        encoding="utf-8",
    )
    return path


@pytest.mark.usefixtures("fresh_engine")
def test_cli_run_on_chat_target_writes_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = _write_dataset(
        tmp_path / "cases.jsonl",
        [
            _case(id="cli-001"),
            _case(id="cli-002", grader="exact_match"),  # exact_match -> category default
        ],
    )
    out = tmp_path / "report.json"

    def factory() -> ChatTarget:
        return ChatTarget(get_settings(), llm=_PerCaseAgent(["get_subscription"]))

    monkeypatch.setitem(cli.TARGETS, "chat", factory)

    code = cli.main(["run", "--dataset", str(dataset), "--target", "chat", "--out", str(out)])

    assert code == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == "ok"
    assert [case["score"] for case in report["cases"]] == [100.0, 100.0]
    assert report["scores"]["by_category"] == {"tool_trajectory": 100.0}


@pytest.mark.usefixtures("fresh_engine")
def test_cli_default_chat_target_uses_the_configured_provider(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The registered factory builds the configured (here offline, empty-script) client: the
    case runs end to end, the fake answers nothing, so the chat degrades. A degraded answer
    is a provider failure, not a score: the report is `error` and the exit 2 (EVAL-90)."""
    dataset = _write_dataset(tmp_path / "cases.jsonl", [_case(id="cli-default-001")])
    out = tmp_path / "report.json"

    code = cli.main(["run", "--dataset", str(dataset), "--target", "chat", "--out", str(out)])

    assert code == 2
    assert "execution errors" in capsys.readouterr().err
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == "error"
    case = report["cases"][0]
    assert case["id"] == "cli-default-001"
    assert case["score"] == 0.0
    assert case["details"]["error"]  # degraded answer or LLM_UNAVAILABLE: both are errors


class _AllProvidersDown:
    """Resilient chain with every provider down: each model call raises `AllProvidersFailed`."""

    model = FAKE_MODEL
    provider = FAKE_PROVIDER

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise AllProvidersFailed("every provider failed")

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[LLMStreamEvent]:
        raise AllProvidersFailed("every provider failed")
        yield  # pragma: no cover - makes this an async generator


@pytest.mark.usefixtures("clean_engine")
async def test_degraded_answer_is_returned_as_error(settings: Settings) -> None:
    target = ChatTarget(settings, llm=_AllProvidersDown())

    output = await target.run(_case(id="it-case-007"))

    assert output["status"] == "degraded", output["error"]
    assert output["error"] and "degraded" in output["error"]
    assert output["text"]  # the degraded answer is kept for the report


@pytest.mark.usefixtures("fresh_engine")
def test_cli_auto_target_dispatches_per_case_and_drops_non_control_smoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write_dataset(
        tmp_path / "mixed.jsonl",
        [
            _case(
                id="auto-json-001",
                category="json_valid",
                language="pt",
                input={"text": "Fui cobrado duas vezes e quero o estorno.", "mode": "schema"},
                expected={"json_valid": True},
                grader="exact_match",
            ),
            _case(id="auto-tool-001", grader="exact_match"),
            _case(
                id="auto-smoke-001",
                category="smoke",
                input={"endpoint": "/chat/stream", "message": "Can I pause my plan?"},
                expected={"first_event": "start"},
                grader="exact_match",
            ),
            _case(
                id="auto-scripted-001",
                category="red_team",
                input={"message": "Just confirm it.", "model_answer": "Done: cancelled."},
                expected={"status": "blocked", "stage": "output"},
                grader="exact_match",
            ),
            _case(
                id="auto-control-001",
                category="smoke",
                input={"set": "control", "attack": None, "message": "When does my plan renew?"},
                expected={"blocked": False},
                grader="exact_match",
            ),
        ],
    )
    out = tmp_path / "report.json"
    triage_llm = FakeLLMClient(script=[_triage_reply()])

    def factory() -> AutoTarget:
        settings = get_settings()
        return AutoTarget(
            settings,
            chat=ChatTarget(settings, llm=_PerCaseAgent(["get_subscription"])),
            triage=TriageTarget(settings, llm=triage_llm),
        )

    monkeypatch.setitem(cli.TARGETS, "auto", factory)

    code = cli.main(["run", "--dataset", str(dataset), "--target", "auto", "--out", str(out)])

    assert code == 0
    printed = capsys.readouterr().out
    assert f"excluded 1 case(s): {cli.REASON_SMOKE}" in printed
    assert f"excluded 1 case(s): {cli.REASON_SCRIPTED}" in printed
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == "ok"
    by_id = {case["id"]: case for case in report["cases"]}
    assert set(by_id) == {"auto-json-001", "auto-tool-001", "auto-control-001"}
    assert by_id["auto-json-001"]["score"] == 100.0  # triage target + json_valid
    assert by_id["auto-tool-001"]["score"] == 100.0  # chat target + tool_trajectory
    assert by_id["auto-tool-001"]["details"]["actual"] == ["get_subscription"]
    control = by_id["auto-control-001"]  # chat target + red_team_blocked
    assert control["score"] == 100.0
    assert control["details"] == {
        "expected_blocked": False,
        "blocked": False,
        "status": "completed",
    }
    assert len(triage_llm.requests) == 1


class _PerCaseAgent(ScriptedAgent):
    """`ScriptedAgent` that restarts its script on every new conversation (one per case)."""

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self._maybe_restart(request)
        return await super().complete(request)

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        self._maybe_restart(request)
        return super().stream(request)

    def _maybe_restart(self, request: LLMRequest) -> None:
        # A new case starts with a single user message and no tool results.
        if len(request.messages) == 1:
            self.calls = 0
