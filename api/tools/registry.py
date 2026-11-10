"""Tool registry (CT-40, TOOL-02, TOOL-03, TOOL-12, TOOL-18, TOOL-94, AGT-91).

The JSON schema sent to the model comes from the same Pydantic `args_model` that validates the
call (`args_model.model_json_schema()`), so they never drift (TOOL-02). Every args model is
`extra='forbid'`; `register` rejects any args model with an identity field
(`IDENTITY_FIELDS`): the customer comes only from `ToolContext`, built from the server session
(TOOL-03, TOOL-18). `execute` never raises for a bad call: an unknown tool returns
`UNKNOWN_TOOL`, arguments that fail validation return `INVALID_ARGUMENTS` with the validation
messages in `content` and the handler is not called (TOOL-94). Handler errors are returned as
results by the agent loop (AGT-04), not here.

Phase 14 (TASK-133, OBS-01): `execute` is one `tool` span (CT-62) with the tool name, the
arguments (JSON, masked by `start_span`) and the result (`ok`, `error_code`, and the content
masked as a whole, then cut at `TOOL_CONTENT_MAX_CHARS`). A handler exception keeps
propagating; the span records its type only.
"""

import json
import re
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, get_args

from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel, PydanticUserError, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from api.guardrails.pii import mask_pii
from api.llm.client import ToolCall, ToolSpec
from api.messages import Lang
from api.observability.tracing import start_span

IDENTITY_FIELDS: frozenset[str] = frozenset({"customer_id", "user_id", "email", "to", "recipient"})
TOOL_CONTENT_MAX_CHARS = 2000


def _mask(text: str) -> str:
    return mask_pii(text).text


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


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
    # Read-only check run before asking the customer to confirm a destructive call: same
    # signature as `handler`; `ok=False` short-circuits the confirmation with that result.
    precheck: Handler | None = None


_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _normalise_field_name(name: str) -> str:
    """`customerId`, `Customer-ID` and `customer_id` all compare as `customer_id`."""
    return _CAMEL_BOUNDARY.sub("_", name).replace("-", "_").lower()


def _is_identity_field(name: str) -> bool:
    return _normalise_field_name(name) in IDENTITY_FIELDS


def _nested_models(annotation: Any) -> Iterator[type[BaseModel]]:
    """Every `BaseModel` subclass reachable from a type annotation (list[X], X | None, ...)."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
        return
    for argument in get_args(annotation):
        yield from _nested_models(argument)


def _all_models(root: type[BaseModel]) -> list[type[BaseModel]]:
    """`root` and every sub-model used by its fields, recursively (cycles visited once)."""
    seen: list[type[BaseModel]] = []
    pending = [root]
    while pending:
        model = pending.pop()
        if model in seen:
            continue
        seen.append(model)
        for info in model.model_fields.values():
            pending.extend(_nested_models(info.annotation))
    return seen


def _schema_property_names(node: Any) -> Iterator[str]:
    """Every property name anywhere in a JSON schema, `$defs` included (alias spelling)."""
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            yield from (key for key in properties if isinstance(key, str))
        for value in node.values():
            yield from _schema_property_names(value)
    elif isinstance(node, list):
        for item in node:
            yield from _schema_property_names(item)


def _validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Validation messages for the model, without the input value or docs URL (TOOL-94)."""
    return [
        {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
        for error in exc.errors(include_url=False, include_context=False, include_input=False)
    ]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        name = definition.name
        if not name or not name.strip():
            raise InvalidToolDefinition("tool name must not be empty")
        if name in self._tools:
            raise InvalidToolDefinition(f"tool {name!r} is already registered")

        models = _all_models(definition.args_model)
        for model in models:
            if model.model_config.get("extra") != "forbid":
                raise InvalidToolDefinition(
                    f"tool {name!r}: args model {model.__name__} must set extra='forbid'"
                )

        try:
            schema = definition.args_model.model_json_schema()
        except PydanticUserError as exc:
            raise InvalidToolDefinition(f"tool {name!r}: args model has no JSON schema") from exc

        field_names = {field for model in models for field in model.model_fields}
        field_names.update(_schema_property_names(schema))
        leaked = sorted(field for field in field_names if _is_identity_field(field))
        if leaked:
            raise InvalidToolDefinition(
                f"tool {name!r}: args model has identity fields {leaked}; "
                "the customer comes only from ToolContext"
            )

        self._tools[name] = definition

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=definition.name,
                description=definition.description,
                input_schema=definition.args_model.model_json_schema(),
            )
            for definition in self._tools.values()
        ]

    def is_destructive(self, name: str) -> bool:
        definition = self._tools.get(name)
        return definition is not None and definition.destructive

    def _resolve(self, call: ToolCall) -> tuple[ToolDefinition, BaseModel] | ToolResult:
        """The definition and validated args, or the `UNKNOWN_TOOL`/`INVALID_ARGUMENTS` result."""
        definition = self._tools.get(call.name)
        if definition is None:
            return ToolResult(
                ok=False, error_code=ToolErrorCode.UNKNOWN_TOOL, content={"tool": call.name}
            )
        try:
            args = definition.args_model.model_validate(call.arguments)
        except ValidationError as exc:
            return ToolResult(
                ok=False,
                error_code=ToolErrorCode.INVALID_ARGUMENTS,
                content={"errors": _validation_errors(exc)},
            )
        return definition, args

    async def precheck(self, call: ToolCall, ctx: ToolContext) -> ToolResult | None:
        """Read-only pre-validation before a confirmation prompt.

        Unknown tool / invalid arguments -> the same result `execute` would return (neither the
        precheck nor the handler runs). No precheck, or a precheck that returns `ok=True` ->
        `None` (go ahead and ask). A precheck that returns `ok=False` -> that result. Precheck
        exceptions propagate to the agent loop.
        """
        resolved = self._resolve(call)
        if isinstance(resolved, ToolResult):
            return resolved
        definition, args = resolved
        if definition.precheck is None:
            return None
        result = await definition.precheck(args, ctx)
        return None if result.ok else result

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        failure: Exception | None = None
        with start_span(
            "tool",
            **{"atlas.tool.name": call.name, "atlas.tool.arguments": _json(call.arguments)},
        ) as span:
            try:
                result = await self._execute(call, ctx)
            except Exception as exc:
                # Raised after the span ends: only the type is recorded, never the message.
                span.set_attribute("error.type", type(exc).__name__)
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                failure = exc
            else:
                span.set_attribute("atlas.tool.ok", result.ok)
                if result.error_code is not None:
                    span.set_attribute("atlas.tool.error_code", result.error_code)
                # `set_attribute` does not mask: the whole content is masked here, then cut, so
                # PII split by the cut cannot escape the mask (OBS-91).
                span.set_attribute(
                    "atlas.tool.result", _mask(_json(result.content))[:TOOL_CONTENT_MAX_CHARS]
                )
        if failure is not None:
            raise failure
        return result

    async def _execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        resolved = self._resolve(call)
        if isinstance(resolved, ToolResult):
            return resolved
        definition, args = resolved
        return await definition.handler(args, ctx)
