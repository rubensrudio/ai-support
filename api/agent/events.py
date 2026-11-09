"""Agent events (CT-45): the single stream type every chat path yields.

A discriminated union on `type`. Phase 01 only emits `done` and `error`; `token`, `tool_*` and
`citation` are produced from phases 03, 06 and 09 on. The SSE encoder (CT-50) serialises these
models as they are, so field names are part of the public contract.
"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

ChatStatus = Literal["completed", "awaiting_confirmation", "blocked", "degraded", "limit_reached"]


class _Event(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ConfirmationPrompt(_Event):
    id: UUID
    tool: str
    target: str
    amount_cents: int | None = None
    currency: Literal["USD"] | None = None


class TokenEvent(_Event):
    type: Literal["token"] = "token"
    text: str


class ToolStartEvent(_Event):
    type: Literal["tool_start"] = "tool_start"
    call_id: str
    tool: str
    label_key: str


class ToolEndEvent(_Event):
    type: Literal["tool_end"] = "tool_end"
    call_id: str
    tool: str
    ok: bool


class CitationEvent(_Event):
    type: Literal["citation"] = "citation"
    index: int
    document: str
    section: str | None = None
    page: int | None = None


class DoneEvent(_Event):
    """Terminal success event. `content` carries the full answer for non-streaming callers."""

    type: Literal["done"] = "done"
    status: ChatStatus
    message_id: int | None = None
    conversation_id: UUID | None = None
    trace_id: str
    language: Literal["pt", "en"]
    content: str = ""
    confirmation: ConfirmationPrompt | None = None


class ErrorEvent(_Event):
    type: Literal["error"] = "error"
    code: str
    message: str
    trace_id: str


AgentEvent = Annotated[
    TokenEvent | ToolStartEvent | ToolEndEvent | CitationEvent | DoneEvent | ErrorEvent,
    Field(discriminator="type"),
]
