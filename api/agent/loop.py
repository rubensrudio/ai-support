"""Agent loop (CT-45, DA-20, AGT-01, AGT-04, AGT-90, AGT-91, TOOL-05, TOOL-12..14).

model -> tool calls -> tool results -> model, until a final answer. Each tool call emits
`tool_start{call_id, tool, label_key}` then `tool_end{call_id, tool, ok}`; a failed tool
(`ToolResult.ok=False` or an exception -> `TOOL_FAILED`) goes back to the model as a result
(AGT-04). A destructive tool (`registry.is_destructive`) is never executed by `run`: the loop
serialises its `AgentState`, calls `turn.suspend(call, state)` (the chat service stores the
pending confirmation and returns the `ConfirmationPrompt`) and ends with
`done{status: 'awaiting_confirmation', confirmation}` (TOOL-05). `resume(state, 'confirm')`
executes the suspended call and continues; `resume(state, 'refuse')` sends the model the result
`{'error': 'customer_refused'}` (`CUSTOMER_REFUSED`) and continues (TOOL-13). The same destructive
call (same name and arguments) asked again after a refusal in the same request returns
`ACTION_ALREADY_REFUSED` without a new confirmation (TOOL-14).

The final `done` carries `content` and `status='completed'`, `message_id=None`: the chat service
persists the answer and re-emits `done` with the message id.

Orchestrator additions to CT-45: `AgentTurn.suspend` and the keyword `suspend` of `resume`.

Round 1 additions: before suspending, the loop runs the tool's read-only
`registry.precheck(call, ctx)`; a refusal (or a precheck exception -> `TOOL_FAILED`) goes back to
the model as the tool result, with `tool_start`/`tool_end(ok=False)` and no confirmation card.
`resume(..., 'confirm')` still runs the handler, which re-checks atomically. Every precheck and
handler runs inside `ctx.session.begin_nested()`, so an exception undoes only that tool's effects
and the caller's transaction stays usable (AGT-04).

Phase 07 limits (CT-47, AGT-02, AGT-03, AGT-05, AGT-08..10, LAC-21, AS-2): the guards live in
the `AgentState` (version 2), so they add up across `run` and `resume`; a version 1 state from
phase 06 loads with an empty cost and repeat history.
- Steps: `StepBudget(settings.agent_max_steps).consume()` before every model call; False ends
  the loop with `step_limit`. The tool calls returned by the call that used the last step never
  run, nor are they prechecked or suspended: the loop ends with `step_limit` (AGT-02).
- Cost: after every model call, `CostBudget(settings.agent_max_cost_usd).add(cost)`. The cost is
  `None` (unknown, not added, logged as `agent_cost_unknown`) for subscription CLI providers
  (`SUBSCRIPTION_PROVIDERS`), else `estimate_cost(model, usage)`. A model without a price
  (`UnknownModelPricing`) is treated as reaching the limit (safe side, logged as
  `agent_cost_unpriced_model`, not counted as an unknown cost). Once the limit is reached the
  loop ends with `cost_limit`, even when that response is a final answer: no tool of it runs and
  no model call follows. A state restored already at the limit ends before any model call.
- Repeats: every tool call goes through `RepeatDetector.check` before it runs or is prechecked:
  `repeat_error` -> not executed, the model gets `{'error': 'repeated call', 'error_code':
  'REPEATED_CALL'}`; `terminate` -> `repeat_limit`. Both log `agent_repeat_detected`. A refused
  destructive repeat still answers `ACTION_ALREADY_REFUSED` (TOOL-14 takes precedence over
  `repeat_error`).
- A limit ends with `done{status: 'limit_reached'}` and `AGENT_LIMIT_REACHED`, listing the
  successful audited actions of every request of the logical turn (`trace_ids`: the `run` and
  each `resume`, oldest first, read through `audit_reader`), without a new model call, and logs
  `agent_limit{reason, steps, cost_usd, unknown_cost_calls}` (AGT-08).

Phase 09 streaming (TASK-094, STR-01, STR-05, STR-91): every model call goes through
`stream_or_complete(llm, request)`. Each non-empty `text_delta` is emitted at once as a
`TokenEvent`; the deltas, `tool_call`s, `usage` and `stop` are folded into one `LLMResponse`, so
the phase 06/07 semantics (guards, precheck, confirmation, savepoints, `AgentState`) are
unchanged.
- Text that precedes tool calls in the same response is streamed too: the loop cannot know a
  tool call follows until it arrives, and holding text back would defeat streaming. It stays in
  the assistant message sent back to the model, as before. The final `done.content` is still the
  text of the last model call (what is stored as the answer); nothing is emitted after `done`.
- Cost identity: a provider without streaming (the CLI adapters) falls back to `complete()`,
  and the loop charges that response's own `model`/`provider`. A streamed call carries no
  identity, so the loop takes the client's `model`/`provider` attributes
  (`InstrumentedLLMClient` has them), else `settings.llm_provider` and
  `settings.primary_model` (`fake-model` for the `fake` provider, as the factory does). A stream
  that ends without `usage` is charged as zero tokens and logged as `agent_stream_without_usage`.
- Limits after a streamed call (round 1): when the cost limit discards a final answer (or the
  step limit discards a response with tool calls), the tokens of that call were already sent;
  the `done{status: 'limit_reached'}` that follows replaces them with the limit message, which
  is what the chat service stores.
- Cancellation (`asyncio.CancelledError` while the model streams, or `GeneratorExit` when the
  consumer closes the stream) is not handled here: it propagates, and the chat service persists
  the partial answer (STR-07, LAC-16). `partial_text` is that partial: the text streamed by the
  model call in progress (or by the final answer just streamed). It is cleared when a call ends
  with tool calls (its text is a preamble, not glued to the answer that follows) and when a
  limit discards the call, so a discarded text is never stored as the partial answer.

Phase 10 output guardrail (TASK-107, DA-22, GRD-04, GRD-08): with an `output_filter` (the chat
service passes a `StreamOutputFilter` when `settings.guardrails_enabled`), every delta of a model
call goes through `feed`, and only the approved sentences it returns become `TokenEvent`s. At the
end of every model call that no limit discarded, `finish()` checks and releases the trailing text
(the tail of a preamble before tools, or of the final answer). A block, at any point of the turn,
ends it at the end of that model call with `done{status: 'blocked'}` and the standard message
(`GUARDRAIL_BLOCKED`): no tool of that response runs, no confirmation is asked and no model call
follows; it is logged as `guardrail_blocked{stage: 'output', reason}`. With a filter,
`partial_text` and the final `done.content` are the text the filter released for the current
model call (the concatenation of what `feed` and `finish` returned, which is the whole text when
nothing is blocked). A call discarded by a limit is not finished: its `done{limit_reached}`
replaces what was streamed, as before.
"""

import copy
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol, cast

from api.agent.events import (
    AgentEvent,
    ChatStatus,
    ConfirmationPrompt,
    DoneEvent,
    TokenEvent,
    ToolEndEvent,
    ToolStartEvent,
)
from api.agent.guards import CostBudget, LimitReason, RepeatDetector, StepBudget
from api.config import SUBSCRIPTION_PROVIDERS, Settings
from api.guardrails.stream_filter import FilterOutcome
from api.llm.client import (
    ChatMessage,
    LLMClient,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    SystemBlock,
    ToolCall,
    ToolSpec,
    Usage,
)
from api.llm.fake import FAKE_MODEL, FAKE_PROVIDER
from api.llm.pricing import UnknownModelPricing, estimate_cost
from api.llm.streaming import stream_or_complete
from api.messages import Lang, MessageKey, user_message
from api.observability.logging import log_event
from api.tools.registry import ToolContext, ToolErrorCode, ToolRegistry, ToolResult

Decision = Literal["confirm", "refuse"]

STATE_VERSION = 2
CUSTOMER_REFUSED_RESULT = "customer_refused"
REPEATED_CALL = "REPEATED_CALL"
REPEATED_CALL_ERROR = "repeated call"
_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
_NO_ACTIONS = "—"


@dataclass
class AgentState:
    """JSON-serialisable loop state (messages, steps, cost, refused calls, pending call).

    `data` holds only JSON types: `version`, the request (`system`, `tools`, `max_tokens`,
    `purpose`, `response_schema`), `messages` (role, content, tool_calls, tool_call_id), `steps`
    (model calls used), `usage` (accumulated token counts), `cost` (`spent` as a decimal string
    and `unknown_calls`, version 2), `repeat` (`RepeatDetector` state, version 2), `trace_ids`
    (trace id of the `run` and of each `resume`, oldest first, version 2),
    `refused_calls` (`[name, arguments]` refused in this request), `pending_call` (the
    destructive call waiting for confirmation),
    `remaining_calls` (calls of the same model step not processed yet), `language`, `trace_id`
    and `conversation_id`. It never carries the customer identity: `resume` takes it from the
    current session's `ToolContext`.
    """

    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentState":
        return cls(data=copy.deepcopy(data))


SuspendFn = Callable[[ToolCall, AgentState], Awaitable[ConfirmationPrompt]]


@dataclass(frozen=True)
class AgentTurn:
    request: LLMRequest  # built with `registry.specs()` as tools
    ctx: ToolContext
    suspend: SuspendFn
    conversation_id: uuid.UUID | None = None


AuditReader = Callable[[str], Awaitable[list[Any]]]


class OutputFilter(Protocol):
    """What the loop needs of `StreamOutputFilter` (CT-75)."""

    async def feed(self, delta: str) -> list[str]: ...

    async def finish(self) -> FilterOutcome: ...


# --- streams ---------------------------------------------------------------------------------


async def aclose_stream(stream: AsyncIterator[Any]) -> None:
    """`aclose()` the stream when it has one (async generators do)."""
    aclose = getattr(stream, "aclose", None)
    if callable(aclose):
        await cast(Callable[[], Awaitable[object]], aclose)()


@asynccontextmanager
async def closing_stream[T](stream: AsyncIterator[T]) -> AsyncIterator[AsyncIterator[T]]:
    """Close `stream` when the block exits, however it exits.

    Leaving an `async for` (a `break`, or `GeneratorExit` thrown at an outer `yield` by
    `aclose()`) does not close the generator being iterated: it would be finalised later, in
    another task. Every nested stream of the loop and the chat service is closed this way, so a
    cancellation reaches the provider stream and the service's cleanup right away (STR-91).
    """
    try:
        yield stream
    finally:
        await aclose_stream(stream)


# --- (de)serialisation -----------------------------------------------------------------------


def _call_to_dict(call: ToolCall) -> dict[str, Any]:
    return {"id": call.id, "name": call.name, "arguments": copy.deepcopy(call.arguments)}


def _call_from_dict(data: dict[str, Any]) -> ToolCall:
    return ToolCall(
        id=str(data["id"]), name=str(data["name"]), arguments=dict(data.get("arguments") or {})
    )


def _message_to_dict(message: ChatMessage) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": message.content,
        "tool_calls": [_call_to_dict(call) for call in message.tool_calls],
        "tool_call_id": message.tool_call_id,
    }


def _message_from_dict(data: dict[str, Any]) -> ChatMessage:
    return ChatMessage(
        role=data["role"],
        content=str(data.get("content", "")),
        tool_calls=[_call_from_dict(call) for call in data.get("tool_calls") or []],
        tool_call_id=data.get("tool_call_id"),
    )


def _canonical(name: str, arguments: dict[str, Any]) -> str:
    """Same tool and same arguments compare equal whatever the key order (TOOL-14)."""
    return json.dumps(
        [name, arguments], sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def _result_message(call: ToolCall, payload: dict[str, Any]) -> ChatMessage:
    return ChatMessage(
        role="tool",
        content=json.dumps(payload, ensure_ascii=False, default=str),
        tool_call_id=call.id,
    )


def _result_payload(result: ToolResult) -> dict[str, Any]:
    if result.ok:
        return {"ok": True, "content": result.content}
    payload: dict[str, Any] = {"ok": False, "error": result.error_code or ToolErrorCode.TOOL_FAILED}
    if result.content:
        payload["content"] = result.content
    return payload


def _error_payload(code: str) -> dict[str, Any]:
    return {"ok": False, "error": code}


def _repeated_payload() -> dict[str, Any]:
    return {"ok": False, "error": REPEATED_CALL_ERROR, "error_code": REPEATED_CALL}


def _log_decimal(value: Decimal) -> str:
    """Plain decimal text for logs: `0E-6` -> `0`, `0.30` -> `0.3`."""
    return f"{value.normalize():f}"


def _system_from(data: dict[str, Any]) -> list[SystemBlock]:
    return [
        SystemBlock(text=str(b["text"]), cache_breakpoint=bool(b["cache_breakpoint"]))
        for b in data.get("system") or []
    ]


def _tools_from(data: dict[str, Any]) -> list[ToolSpec]:
    return [
        ToolSpec(
            name=str(t["name"]),
            description=str(t["description"]),
            input_schema=dict(t["input_schema"]),
        )
        for t in data.get("tools") or []
    ]


def _cost_from(data: dict[str, Any], settings: Settings) -> CostBudget:
    # Version 1 (phase 06) has no `cost`: it starts empty.
    cost = data.get("cost") or {}
    return CostBudget(
        settings.agent_max_cost_usd,
        spent=Decimal(str(cost.get("spent", "0"))),
        unknown_calls=int(cost.get("unknown_calls", 0)),
    )


def _trace_ids_from(data: dict[str, Any]) -> list[str]:
    """Stored `trace_ids`; a state without them (v1, early v2) falls back to its `trace_id`.

    `resume` then adds the current request's trace id."""
    stored = data.get("trace_ids")
    candidates = stored if isinstance(stored, list) else [data.get("trace_id")]
    trace_ids: list[str] = []
    for value in candidates:
        if value and str(value) not in trace_ids:
            trace_ids.append(str(value))
    return trace_ids


def state_trace_ids(data: dict[str, Any]) -> list[str]:
    """Trace ids of the requests of a serialised `AgentState`, oldest first."""
    return _trace_ids_from(data)


@dataclass
class _Run:
    """Mutable working copy of `AgentState` for one `run`/`resume` call."""

    system: list[SystemBlock]
    tools: list[ToolSpec]
    max_tokens: int
    purpose: str
    response_schema: dict[str, Any] | None
    messages: list[ChatMessage]
    steps: StepBudget
    cost: CostBudget
    repeats: RepeatDetector
    usage: dict[str, int]
    refused_calls: list[tuple[str, dict[str, Any]]]
    pending_call: ToolCall | None
    remaining_calls: list[ToolCall]
    language: Lang
    trace_id: str
    trace_ids: list[str]
    conversation_id: uuid.UUID | None

    @classmethod
    def start(cls, turn: AgentTurn, settings: Settings) -> "_Run":
        request = turn.request
        return cls(
            system=list(request.system),
            tools=list(request.tools),
            max_tokens=request.max_tokens,
            purpose=request.purpose,
            response_schema=copy.deepcopy(request.response_schema),
            messages=list(request.messages),
            steps=StepBudget(settings.agent_max_steps),
            cost=CostBudget(settings.agent_max_cost_usd),
            repeats=RepeatDetector(),
            usage=dict.fromkeys(_USAGE_FIELDS, 0),
            refused_calls=[],
            pending_call=None,
            remaining_calls=[],
            language=turn.ctx.language,
            trace_id=turn.ctx.trace_id,
            trace_ids=[turn.ctx.trace_id],
            conversation_id=turn.conversation_id,
        )

    @classmethod
    def restore(cls, state: AgentState, settings: Settings) -> "_Run":
        data = state.to_dict()
        pending = data.get("pending_call")
        conversation_id = data.get("conversation_id")
        usage = data.get("usage") or {}
        return cls(
            system=_system_from(data),
            tools=_tools_from(data),
            max_tokens=int(data.get("max_tokens", 1024)),
            purpose=str(data.get("purpose", "chat")),
            response_schema=data.get("response_schema"),
            messages=[_message_from_dict(m) for m in data.get("messages") or []],
            steps=StepBudget(settings.agent_max_steps, used=int(data.get("steps", 0))),
            cost=_cost_from(data, settings),
            repeats=RepeatDetector.from_dict(data.get("repeat") or {}),
            usage={name: int(usage.get(name, 0)) for name in _USAGE_FIELDS},
            refused_calls=[
                (str(name), dict(args)) for name, args in data.get("refused_calls") or []
            ],
            pending_call=_call_from_dict(pending) if pending else None,
            remaining_calls=[_call_from_dict(c) for c in data.get("remaining_calls") or []],
            language=cast(Lang, data.get("language", "en")),
            trace_id=str(data.get("trace_id", "")),
            trace_ids=_trace_ids_from(data),
            conversation_id=uuid.UUID(conversation_id) if conversation_id else None,
        )

    def add_trace(self, trace_id: str) -> None:
        if trace_id and trace_id not in self.trace_ids:
            self.trace_ids.append(trace_id)

    def to_state(self) -> AgentState:
        return AgentState(
            data={
                "version": STATE_VERSION,
                "system": [
                    {"text": b.text, "cache_breakpoint": b.cache_breakpoint} for b in self.system
                ],
                "tools": [
                    {
                        "name": t.name,
                        "description": t.description,
                        "input_schema": copy.deepcopy(t.input_schema),
                    }
                    for t in self.tools
                ],
                "max_tokens": self.max_tokens,
                "purpose": self.purpose,
                "response_schema": copy.deepcopy(self.response_schema),
                "messages": [_message_to_dict(m) for m in self.messages],
                "steps": self.steps.used,
                "cost": {"spent": str(self.cost.spent), "unknown_calls": self.cost.unknown_calls},
                "repeat": self.repeats.to_dict(),
                "usage": dict(self.usage),
                "refused_calls": [[name, copy.deepcopy(args)] for name, args in self.refused_calls],
                "pending_call": _call_to_dict(self.pending_call) if self.pending_call else None,
                "remaining_calls": [_call_to_dict(c) for c in self.remaining_calls],
                "language": self.language,
                "trace_id": self.trace_id,
                "trace_ids": list(self.trace_ids),
                "conversation_id": str(self.conversation_id) if self.conversation_id else None,
            }
        )

    def request(self) -> LLMRequest:
        return LLMRequest(
            system=list(self.system),
            messages=list(self.messages),
            tools=list(self.tools),
            response_schema=self.response_schema,
            max_tokens=self.max_tokens,
            purpose=self.purpose,
        )

    def record_response(self, response: LLMResponse) -> None:
        for name in _USAGE_FIELDS:
            self.usage[name] += int(getattr(response.usage, name))
        self.messages.append(
            ChatMessage(
                role="assistant", content=response.text, tool_calls=list(response.tool_calls)
            )
        )
        self.remaining_calls = list(response.tool_calls)

    def was_refused(self, call: ToolCall) -> bool:
        key = _canonical(call.name, call.arguments)
        return any(_canonical(name, args) == key for name, args in self.refused_calls)


class _RecordingClient:
    """Delegates to `inner`; keeps the response of a `complete()` call (the fallback of
    `stream_or_complete`), so a non-streaming provider is charged with its own identity."""

    def __init__(self, inner: LLMClient) -> None:
        self.inner = inner
        self.response: LLMResponse | None = None

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.response = await self.inner.complete(request)
        return self.response

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self.inner.stream(request)


@dataclass
class _CallResult:
    response: LLMResponse = field(
        default_factory=lambda: LLMResponse(
            text="",
            tool_calls=[],
            usage=Usage(input_tokens=0, output_tokens=0),
            model="",
            provider="",
            stop_reason=None,
        )
    )


# --- loop ------------------------------------------------------------------------------------


class AgentLoop:
    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        settings: Settings,
        audit_reader: AuditReader | None = None,
        output_filter: OutputFilter | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.settings = settings
        self.audit_reader = audit_reader
        self.output_filter = output_filter
        self._streamed: list[str] = []
        self._resumed_settled = True

    @property
    def partial_text(self) -> str:
        """Text streamed so far by the current model call (see the module docstring)."""
        return "".join(self._streamed)

    @property
    def resumed_call_settled(self) -> bool:
        """False from `resume(..., 'confirm')` until the handler of the confirmed call has run
        (set right after it returns, before its `tool_end`). A refusal settles at once (no tool
        runs) and `run` has nothing to settle. The chat service commits a cancelled resume only
        once this is True, so a confirmation is never stored as confirmed without its tool."""
        return self._resumed_settled

    def run(self, turn: AgentTurn) -> AsyncIterator[AgentEvent]:
        self._streamed = []
        return self._drive(
            _Run.start(turn, self.settings), turn.ctx, turn.suspend, turn.conversation_id
        )

    def resume(
        self,
        state: AgentState,
        decision: Decision,
        ctx: ToolContext,
        *,
        suspend: SuspendFn,
        conversation_id: uuid.UUID | None = None,
    ) -> AsyncIterator[AgentEvent]:
        self._streamed = []
        self._resumed_settled = decision != "confirm"
        run = _Run.restore(state, self.settings)
        run.add_trace(ctx.trace_id)
        if run.pending_call is None:
            raise ValueError("agent state has no pending call to resume")
        if decision not in ("confirm", "refuse"):
            raise ValueError(f"unknown confirmation decision {decision!r}")
        return self._resume(run, decision, ctx, suspend, conversation_id or run.conversation_id)

    async def _resume(
        self,
        run: _Run,
        decision: Decision,
        ctx: ToolContext,
        suspend: SuspendFn,
        conversation_id: uuid.UUID | None,
    ) -> AsyncIterator[AgentEvent]:
        call = cast(ToolCall, run.pending_call)
        run.pending_call = None
        if decision == "confirm":
            # Identity comes from the current session's ctx, never from the stored state.
            async with closing_stream(self._execute(run, call, ctx, settles=True)) as executed:
                async for event in executed:
                    yield event
        else:
            run.refused_calls.append((call.name, copy.deepcopy(call.arguments)))
            run.messages.append(_result_message(call, _error_payload(CUSTOMER_REFUSED_RESULT)))
        async with closing_stream(self._drive(run, ctx, suspend, conversation_id)) as driven:
            async for event in driven:
                yield event

    async def _drive(
        self,
        run: _Run,
        ctx: ToolContext,
        suspend: SuspendFn,
        conversation_id: uuid.UUID | None,
    ) -> AsyncIterator[AgentEvent]:
        """Pending tool calls, then one model call, until a `done` (always the last event)."""
        while True:
            for step in (
                self._run_calls(run, ctx, suspend, conversation_id),
                self._model_step(run, ctx, conversation_id),
            ):
                async with closing_stream(step) as events:
                    async for event in events:
                        yield event
                        if isinstance(event, DoneEvent):
                            return

    async def _run_calls(
        self,
        run: _Run,
        ctx: ToolContext,
        suspend: SuspendFn,
        conversation_id: uuid.UUID | None,
    ) -> AsyncIterator[AgentEvent]:
        """Process `run.remaining_calls`; ends with a `done` when the turn stops here (repeat
        limit or a suspension for confirmation)."""
        while run.remaining_calls:
            call = run.remaining_calls.pop(0)
            # AGT-09/AGT-10: checked before any execution, precheck or suspension.
            outcome = self._repeat_outcome(run, call)
            if outcome == "terminate":
                yield await self._limit("repeat_limit", run, ctx, conversation_id)
                return
            if outcome == "answered":
                continue
            if not self.registry.is_destructive(call.name):
                async with closing_stream(self._execute(run, call, ctx)) as executed:
                    async for event in executed:
                        yield event
                continue
            if run.was_refused(call):
                # TOOL-14: no execution and no new confirmation for a refused repeat.
                run.messages.append(
                    _result_message(call, _error_payload(ToolErrorCode.ACTION_ALREADY_REFUSED))
                )
                continue
            # Read-only precheck: a call that would be refused anyway (another customer's or
            # a non-cancellable order, a non-eligible refund) goes back to the model without
            # a confirmation card. The handler re-checks atomically after confirmation.
            refusal = await self._guarded(call, ctx, self.registry.precheck)
            if refusal is not None:
                yield ToolStartEvent(call_id=call.id, tool=call.name, label_key=self._label(call))
                run.messages.append(_result_message(call, _result_payload(refusal)))
                yield ToolEndEvent(call_id=call.id, tool=call.name, ok=refusal.ok)
                continue
            # TOOL-05: suspend; the handler only runs from `resume(..., 'confirm')`.
            run.pending_call = call
            prompt = await suspend(call, run.to_state())
            yield self._done("awaiting_confirmation", ctx, conversation_id, confirmation=prompt)
            return

    async def _model_step(
        self, run: _Run, ctx: ToolContext, conversation_id: uuid.UUID | None
    ) -> AsyncIterator[AgentEvent]:
        """One model call (its tokens), guarded by the limits; ends with a `done` for a final
        answer or a limit, and without one when the response asks for tools."""
        reason = self._step(run)
        if reason is None:
            call_result = _CallResult()
            async with closing_stream(self._call_model(run.request(), call_result)) as model:
                async for event in model:
                    yield event
            response = call_result.response
            run.record_response(response)
            reason = self._after_call(run, response)
            if reason is None and self.output_filter is not None:
                outcome = await self.output_filter.finish()
                if outcome.blocked:
                    yield self._blocked(outcome, ctx, conversation_id)
                    return
                if outcome.released:
                    self._streamed.append(outcome.released)
                    yield TokenEvent(text=outcome.released)
            # With a filter, the answer is what it released (the whole text when nothing is
            # blocked); without one, the text of the call.
            content = self.partial_text if self.output_filter is not None else response.text
            if response.tool_calls or reason is not None:
                # A preamble before tools, or a call discarded by a limit: not a partial.
                self._streamed.clear()
            if reason is None and not response.tool_calls:
                yield self._done("completed", ctx, conversation_id, content=content)
                return
        if reason is not None:
            yield await self._limit(reason, run, ctx, conversation_id)

    async def _call_model(
        self, request: LLMRequest, result: "_CallResult"
    ) -> AsyncIterator[AgentEvent]:
        """Stream one model call: a `TokenEvent` per non-empty delta; the folded response is
        left in `result.response` once the stream ends."""
        recorder = _RecordingClient(self.llm)
        self._streamed.clear()
        parts: list[str] = []
        calls: list[ToolCall] = []
        usage: Usage | None = None
        stop_reason: str | None = None
        streamed_identity: tuple[str, str] | None = None
        async with closing_stream(stream_or_complete(recorder, request)) as source:
            async for event in source:
                if event.kind == "text_delta" and event.text:
                    parts.append(event.text)
                    for text in await self._release(event.text):
                        self._streamed.append(text)
                        yield TokenEvent(text=text)
                elif event.kind == "tool_call" and event.tool_call is not None:
                    calls.append(event.tool_call)
                elif event.kind == "usage" and event.usage is not None:
                    usage = event.usage
                    if event.model and event.provider:
                        streamed_identity = (event.model, event.provider)
                elif event.kind == "stop":
                    stop_reason = event.stop_reason
        if recorder.response is not None:
            # Fallback without streaming: the provider's own response, identity included.
            result.response = recorder.response
            return
        if usage is None:
            log_event("agent_stream_without_usage")
            usage = Usage(input_tokens=0, output_tokens=0)
        model, provider = streamed_identity or self._identity()
        result.response = LLMResponse(
            text="".join(parts),
            tool_calls=calls,
            usage=usage,
            model=model,
            provider=provider,
            stop_reason=stop_reason,
        )

    async def _release(self, delta: str) -> list[str]:
        """The pieces of `delta` that may be streamed: all of it without a filter, else the
        sentences the filter approved (possibly none yet)."""
        if self.output_filter is None:
            return [delta]
        return [text for text in await self.output_filter.feed(delta) if text]

    def _blocked(
        self, outcome: FilterOutcome, ctx: ToolContext, conversation_id: uuid.UUID | None
    ) -> DoneEvent:
        """The output guardrail blocked the answer: the standard message replaces all of it."""
        log_event("guardrail_blocked", stage="output", reason=outcome.reason)
        return self._done(
            "blocked",
            ctx,
            conversation_id,
            content=user_message(MessageKey.GUARDRAIL_BLOCKED, ctx.language),
        )

    def _identity(self) -> tuple[str, str]:
        """`(model, provider)` charged for a streamed call whose `usage` event did not carry the
        answering provider's identity (set by `InstrumentedLLMClient` since phase 12)."""
        provider = getattr(self.llm, "provider", None)
        if not isinstance(provider, str) or not provider:
            provider = str(self.settings.llm_provider)
        model = getattr(self.llm, "model", None)
        if not isinstance(model, str) or not model:
            model = self.settings.primary_model or (FAKE_MODEL if provider == FAKE_PROVIDER else "")
        return model, provider

    def _repeat_outcome(
        self, run: _Run, call: ToolCall
    ) -> Literal["proceed", "answered", "terminate"]:
        """`RepeatDetector` verdict for `call`. A `repeat_error` goes back to the model as
        `REPEATED_CALL` (`answered`), except for a refused destructive repeat, which keeps the
        TOOL-14 answer (`proceed`)."""
        verdict = run.repeats.check(call)
        if verdict == "ok":
            return "proceed"
        log_event(
            "agent_repeat_detected", tool=call.name, verdict=verdict, streak=run.repeats.streak
        )
        if verdict == "terminate":
            return "terminate"
        if self.registry.is_destructive(call.name) and run.was_refused(call):
            return "proceed"
        run.messages.append(_result_message(call, _repeated_payload()))
        return "answered"

    @staticmethod
    def _step(run: _Run) -> LimitReason | None:
        """Before a model call: a state already at the cost limit, or no step left."""
        if run.cost.spent >= run.cost.max_usd:
            return "cost_limit"
        if not run.steps.consume():
            return "step_limit"
        return None

    def _after_call(self, run: _Run, response: LLMResponse) -> LimitReason | None:
        """After a model call: the cost limit (even for a final answer, AGT-03/AGT-08), then
        tools asked by the call that used the last step, which never run (AGT-02)."""
        if not self._charge(run, response):
            return "cost_limit"
        if response.tool_calls and run.steps.used >= run.steps.max_steps:
            return "step_limit"
        return None

    @staticmethod
    def _charge(run: _Run, response: LLMResponse) -> bool:
        """Add the cost of one model call; False once the cost limit is reached (AGT-03)."""
        cost: Decimal | None
        if response.provider in SUBSCRIPTION_PROVIDERS:
            cost = None
            log_event("agent_cost_unknown", provider=response.provider, model=response.model)
        else:
            try:
                cost = estimate_cost(response.model, response.usage)
            except UnknownModelPricing:
                # Safe side (AS-2): a call we cannot price must not let the loop go on.
                log_event(
                    "agent_cost_unpriced_model", provider=response.provider, model=response.model
                )
                return False
        return run.cost.add(cost)

    async def _limit(
        self,
        reason: LimitReason,
        run: _Run,
        ctx: ToolContext,
        conversation_id: uuid.UUID | None,
    ) -> DoneEvent:
        """AGT-08: end without a new model call, listing the audited successful actions."""
        log_event(
            "agent_limit",
            reason=reason,
            steps=run.steps.used,
            cost_usd=_log_decimal(run.cost.spent),
            unknown_cost_calls=run.cost.unknown_calls,
        )
        return self._done(
            "limit_reached", ctx, conversation_id, content=await self._limit_text(run, ctx)
        )

    async def _execute(
        self, run: _Run, call: ToolCall, ctx: ToolContext, *, settles: bool = False
    ) -> AsyncIterator[AgentEvent]:
        """Run one tool; `settles` marks the confirmed call of a resume as executed."""
        yield ToolStartEvent(call_id=call.id, tool=call.name, label_key=self._label(call))
        result = await self._guarded(call, ctx, self.registry.execute)
        if settles:
            self._resumed_settled = True
        run.messages.append(_result_message(call, _result_payload(result)))
        yield ToolEndEvent(call_id=call.id, tool=call.name, ok=result.ok)

    def _label(self, call: ToolCall) -> str:
        definition = self.registry.get(call.name)
        return definition.label_key if definition else MessageKey.STEP_GENERIC.value

    @staticmethod
    async def _guarded[R: (ToolResult, ToolResult | None)](
        call: ToolCall, ctx: ToolContext, operation: Callable[[ToolCall, ToolContext], Awaitable[R]]
    ) -> R:
        """Run one tool step (precheck or handler) inside its own savepoint (AGT-04).

        An exception rolls back only that savepoint, so the caller's transaction stays usable
        and the effects of earlier tools survive; the model gets `TOOL_FAILED` without details.
        """
        try:
            async with ctx.session.begin_nested():
                return await operation(call, ctx)
        except Exception as exc:
            log_event("agent_tool_failed", tool=call.name, error_type=type(exc).__name__)
            return ToolResult(ok=False, error_code=ToolErrorCode.TOOL_FAILED)

    async def _limit_text(self, run: _Run, ctx: ToolContext) -> str:
        """Audited successful actions of every request of the turn, oldest request first."""
        names: list[str] = []
        if self.audit_reader is not None:
            trace_ids = list(run.trace_ids)
            if ctx.trace_id not in trace_ids:
                trace_ids.append(ctx.trace_id)
            for trace_id in trace_ids:
                rows = await self.audit_reader(trace_id)
                names.extend(str(getattr(row, "tool", row)) for row in rows)
        actions = ", ".join(names) if names else _NO_ACTIONS
        return user_message(MessageKey.AGENT_LIMIT_REACHED, ctx.language, actions=actions)

    @staticmethod
    def _done(
        status: ChatStatus,
        ctx: ToolContext,
        conversation_id: uuid.UUID | None,
        *,
        content: str = "",
        confirmation: ConfirmationPrompt | None = None,
    ) -> DoneEvent:
        return DoneEvent(
            status=status,
            conversation_id=conversation_id,
            trace_id=ctx.trace_id,
            language=ctx.language,
            content=content,
            confirmation=confirmation,
        )
