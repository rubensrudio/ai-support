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
`ACTION_ALREADY_REFUSED` without a new confirmation (TOOL-14). Fixed safety cap of
`MAX_STEPS = 8` model calls in phase 06 (configurable limits are phase 07).

The final `done` carries `content` and `status='completed'`, `message_id=None`: the chat service
persists the answer and re-emits `done` with the message id.

Orchestrator additions to CT-45: `AgentTurn.suspend` and the keyword `suspend` of `resume`.

Round 1 additions: before suspending, the loop runs the tool's read-only
`registry.precheck(call, ctx)`; a refusal (or a precheck exception -> `TOOL_FAILED`) goes back to
the model as the tool result, with `tool_start`/`tool_end(ok=False)` and no confirmation card.
`resume(..., 'confirm')` still runs the handler, which re-checks atomically. Every precheck and
handler runs inside `ctx.session.begin_nested()`, so an exception undoes only that tool's effects
and the caller's transaction stays usable (AGT-04).
"""

import copy
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, cast

from api.agent.events import (
    AgentEvent,
    ChatStatus,
    ConfirmationPrompt,
    DoneEvent,
    ToolEndEvent,
    ToolStartEvent,
)
from api.config import Settings
from api.llm.client import (
    ChatMessage,
    LLMClient,
    LLMRequest,
    LLMResponse,
    SystemBlock,
    ToolCall,
    ToolSpec,
)
from api.messages import Lang, MessageKey, user_message
from api.observability.logging import log_event
from api.tools.registry import ToolContext, ToolErrorCode, ToolRegistry, ToolResult

MAX_STEPS = 8
Decision = Literal["confirm", "refuse"]

STATE_VERSION = 1
CUSTOMER_REFUSED_RESULT = "customer_refused"
_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
_NO_ACTIONS = "—"


@dataclass
class AgentState:
    """JSON-serialisable loop state (messages, steps, cost, refused calls, pending call).

    `data` holds only JSON types: `version`, the request (`system`, `tools`, `max_tokens`,
    `purpose`, `response_schema`), `messages` (role, content, tool_calls, tool_call_id), `steps`
    (model calls used), `usage` (accumulated token counts), `refused_calls` (`[name, arguments]`
    refused in this request), `pending_call` (the destructive call waiting for confirmation),
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


@dataclass
class _Run:
    """Mutable working copy of `AgentState` for one `run`/`resume` call."""

    system: list[SystemBlock]
    tools: list[ToolSpec]
    max_tokens: int
    purpose: str
    response_schema: dict[str, Any] | None
    messages: list[ChatMessage]
    steps: int
    usage: dict[str, int]
    refused_calls: list[tuple[str, dict[str, Any]]]
    pending_call: ToolCall | None
    remaining_calls: list[ToolCall]
    language: Lang
    trace_id: str
    conversation_id: uuid.UUID | None

    @classmethod
    def start(cls, turn: AgentTurn) -> "_Run":
        request = turn.request
        return cls(
            system=list(request.system),
            tools=list(request.tools),
            max_tokens=request.max_tokens,
            purpose=request.purpose,
            response_schema=copy.deepcopy(request.response_schema),
            messages=list(request.messages),
            steps=0,
            usage=dict.fromkeys(_USAGE_FIELDS, 0),
            refused_calls=[],
            pending_call=None,
            remaining_calls=[],
            language=turn.ctx.language,
            trace_id=turn.ctx.trace_id,
            conversation_id=turn.conversation_id,
        )

    @classmethod
    def restore(cls, state: AgentState) -> "_Run":
        data = state.to_dict()
        pending = data.get("pending_call")
        conversation_id = data.get("conversation_id")
        usage = data.get("usage") or {}
        return cls(
            system=[
                SystemBlock(text=str(b["text"]), cache_breakpoint=bool(b["cache_breakpoint"]))
                for b in data.get("system") or []
            ],
            tools=[
                ToolSpec(
                    name=str(t["name"]),
                    description=str(t["description"]),
                    input_schema=dict(t["input_schema"]),
                )
                for t in data.get("tools") or []
            ],
            max_tokens=int(data.get("max_tokens", 1024)),
            purpose=str(data.get("purpose", "chat")),
            response_schema=data.get("response_schema"),
            messages=[_message_from_dict(m) for m in data.get("messages") or []],
            steps=int(data.get("steps", 0)),
            usage={name: int(usage.get(name, 0)) for name in _USAGE_FIELDS},
            refused_calls=[
                (str(name), dict(args)) for name, args in data.get("refused_calls") or []
            ],
            pending_call=_call_from_dict(pending) if pending else None,
            remaining_calls=[_call_from_dict(c) for c in data.get("remaining_calls") or []],
            language=cast(Lang, data.get("language", "en")),
            trace_id=str(data.get("trace_id", "")),
            conversation_id=uuid.UUID(conversation_id) if conversation_id else None,
        )

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
                "steps": self.steps,
                "usage": dict(self.usage),
                "refused_calls": [[name, copy.deepcopy(args)] for name, args in self.refused_calls],
                "pending_call": _call_to_dict(self.pending_call) if self.pending_call else None,
                "remaining_calls": [_call_to_dict(c) for c in self.remaining_calls],
                "language": self.language,
                "trace_id": self.trace_id,
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
        self.steps += 1
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


# --- loop ------------------------------------------------------------------------------------


class AgentLoop:
    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        settings: Settings,
        audit_reader: AuditReader | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.settings = settings
        self.audit_reader = audit_reader

    def run(self, turn: AgentTurn) -> AsyncIterator[AgentEvent]:
        return self._drive(_Run.start(turn), turn.ctx, turn.suspend, turn.conversation_id)

    def resume(
        self,
        state: AgentState,
        decision: Decision,
        ctx: ToolContext,
        *,
        suspend: SuspendFn,
        conversation_id: uuid.UUID | None = None,
    ) -> AsyncIterator[AgentEvent]:
        run = _Run.restore(state)
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
            async for event in self._execute(run, call, ctx):
                yield event
        else:
            run.refused_calls.append((call.name, copy.deepcopy(call.arguments)))
            run.messages.append(_result_message(call, _error_payload(CUSTOMER_REFUSED_RESULT)))
        async for event in self._drive(run, ctx, suspend, conversation_id):
            yield event

    async def _drive(
        self,
        run: _Run,
        ctx: ToolContext,
        suspend: SuspendFn,
        conversation_id: uuid.UUID | None,
    ) -> AsyncIterator[AgentEvent]:
        while True:
            while run.remaining_calls:
                call = run.remaining_calls.pop(0)
                if not self.registry.is_destructive(call.name):
                    async for event in self._execute(run, call, ctx):
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
                    yield ToolStartEvent(
                        call_id=call.id, tool=call.name, label_key=self._label(call)
                    )
                    run.messages.append(_result_message(call, _result_payload(refusal)))
                    yield ToolEndEvent(call_id=call.id, tool=call.name, ok=refusal.ok)
                    continue
                # TOOL-05: suspend; the handler only runs from `resume(..., 'confirm')`.
                run.pending_call = call
                prompt = await suspend(call, run.to_state())
                yield self._done("awaiting_confirmation", ctx, conversation_id, confirmation=prompt)
                return

            if run.steps >= MAX_STEPS:
                yield self._done(
                    "limit_reached", ctx, conversation_id, content=await self._limit_text(ctx)
                )
                return

            response = await self.llm.complete(run.request())
            run.record_response(response)
            if not response.tool_calls:
                yield self._done("completed", ctx, conversation_id, content=response.text)
                return

    async def _execute(
        self, run: _Run, call: ToolCall, ctx: ToolContext
    ) -> AsyncIterator[AgentEvent]:
        yield ToolStartEvent(call_id=call.id, tool=call.name, label_key=self._label(call))
        result = await self._guarded(call, ctx, self.registry.execute)
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

    async def _limit_text(self, ctx: ToolContext) -> str:
        actions = _NO_ACTIONS
        if self.audit_reader is not None:
            rows = await self.audit_reader(ctx.trace_id)
            names = [str(getattr(row, "tool", row)) for row in rows]
            if names:
                actions = ", ".join(names)
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
