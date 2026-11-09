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
"""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from api.agent.events import AgentEvent, ConfirmationPrompt
from api.config import Settings
from api.llm.client import LLMClient, LLMRequest, ToolCall
from api.tools.registry import ToolContext, ToolRegistry

MAX_STEPS = 8
Decision = Literal["confirm", "refuse"]


@dataclass
class AgentState:
    """JSON-serialisable loop state (messages, steps, cost, refused calls, pending call)."""

    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError("TASK-076")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentState":
        raise NotImplementedError("TASK-076")


SuspendFn = Callable[[ToolCall, AgentState], Awaitable[ConfirmationPrompt]]


@dataclass(frozen=True)
class AgentTurn:
    request: LLMRequest  # built with `registry.specs()` as tools
    ctx: ToolContext
    suspend: SuspendFn
    conversation_id: uuid.UUID | None = None


AuditReader = Callable[[str], Awaitable[list[Any]]]


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
        raise NotImplementedError("TASK-076")

    def resume(
        self,
        state: AgentState,
        decision: Decision,
        ctx: ToolContext,
        *,
        suspend: SuspendFn,
        conversation_id: uuid.UUID | None = None,
    ) -> AsyncIterator[AgentEvent]:
        raise NotImplementedError("TASK-076")
