"""Tool registry (CT-40, TOOL-02, TOOL-03, TOOL-12, TOOL-18, TOOL-94, AGT-91).

The JSON schema sent to the model comes from the same Pydantic `args_model` that validates the
call (`args_model.model_json_schema()`), so they never drift (TOOL-02). Every args model is
`extra='forbid'`; `register` rejects any args model with an identity field
(`IDENTITY_FIELDS`): the customer comes only from `ToolContext`, built from the server session
(TOOL-03, TOOL-18). `execute` never raises for a bad call: an unknown tool returns
`UNKNOWN_TOOL`, arguments that fail validation return `INVALID_ARGUMENTS` with the validation
messages in `content` and the handler is not called (TOOL-94). Handler errors are returned as
results by the agent loop (AGT-04), not here.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.llm.client import ToolCall, ToolSpec
from api.messages import Lang

IDENTITY_FIELDS: frozenset[str] = frozenset({"customer_id", "user_id", "email", "to", "recipient"})


class ToolErrorCode:
    """`ToolResult.error_code` values returned to the model (never shown raw to the customer)."""

    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"  # missing or owned by another customer (TOOL-90)
    ORDER_NOT_CANCELLABLE = "ORDER_NOT_CANCELLABLE"  # TOOL-91, TOOL-92, TOOL-99
    REFUND_NOT_ELIGIBLE = "REFUND_NOT_ELIGIBLE"  # TOOL-15
    ALREADY_REFUNDED = "ALREADY_REFUNDED"  # TOOL-93
    SUBSCRIPTION_NOT_FOUND = "SUBSCRIPTION_NOT_FOUND"
    ACTION_ALREADY_REFUSED = "ACTION_ALREADY_REFUSED"  # TOOL-14 (agent loop)
    CUSTOMER_REFUSED = "CUSTOMER_REFUSED"  # TOOL-13 (agent loop, `customer_refused` result)
    TOOL_FAILED = "TOOL_FAILED"  # unexpected handler exception (agent loop, AGT-04)


class InvalidToolDefinition(ValueError):
    """The definition breaks a registry rule (identity field, extra not forbidden, duplicate)."""


@dataclass(frozen=True)
class ToolContext:
    """Per-call context built by the server from the authenticated session (never the model)."""

    customer_id: uuid.UUID
    customer_email: str
    trace_id: str
    session: AsyncSession
    language: Lang
    now: datetime


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None


Handler = Callable[[Any, ToolContext], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    args_model: type[BaseModel]
    destructive: bool
    handler: Handler
    label_key: str = "STEP_GENERIC"  # `api.messages.MessageKey` shown in `tool_start`


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        raise NotImplementedError("TASK-068")

    def get(self, name: str) -> ToolDefinition | None:
        raise NotImplementedError("TASK-068")

    def names(self) -> list[str]:
        raise NotImplementedError("TASK-068")

    def specs(self) -> list[ToolSpec]:
        raise NotImplementedError("TASK-068")

    def is_destructive(self, name: str) -> bool:
        raise NotImplementedError("TASK-068")

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        raise NotImplementedError("TASK-068")
