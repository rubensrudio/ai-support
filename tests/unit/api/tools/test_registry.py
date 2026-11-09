"""Tool registry (TASK-068, TOOL-02, TOOL-03, TOOL-12, TOOL-18, TOOL-94, AGT-91)."""

import uuid
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.llm.client import ToolCall, ToolSpec
from api.tools.registry import (
    IDENTITY_FIELDS,
    InvalidToolDefinition,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolRegistry,
    ToolResult,
)


class LookupArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int = Field(ge=1)


class NoteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str
    body: str = ""


class RecordingHandler:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ToolContext]] = []

    async def __call__(self, args: Any, ctx: ToolContext) -> ToolResult:
        self.calls.append((args, ctx))
        return ToolResult(ok=True, content={"echo": args.model_dump()})


def _definition(
    name: str = "lookup",
    args_model: type[BaseModel] = LookupArgs,
    *,
    destructive: bool = False,
    handler: RecordingHandler | None = None,
    precheck: RecordingHandler | None = None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"{name} description",
        args_model=args_model,
        destructive=destructive,
        handler=handler or RecordingHandler(),
        precheck=precheck,
    )


class RefusingPrecheck(RecordingHandler):
    async def __call__(self, args: Any, ctx: ToolContext) -> ToolResult:
        self.calls.append((args, ctx))
        return ToolResult(
            ok=False, error_code=ToolErrorCode.ORDER_NOT_CANCELLABLE, content={"order_id": 7}
        )


def _ctx() -> ToolContext:
    return ToolContext(
        customer_id=uuid.uuid4(),
        customer_email="alex@example.com",
        trace_id="trace-1",
        session=cast(AsyncSession, object()),
        language="en",
        now=datetime(2026, 11, 9, tzinfo=UTC),
    )


def _call(name: str, arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(id="call-1", name=name, arguments=arguments)


# --- specs / lookup (TOOL-02) ---------------------------------------------------------------


def test_specs_use_the_args_model_json_schema_in_registration_order() -> None:
    registry = ToolRegistry()
    registry.register(_definition("lookup", LookupArgs))
    registry.register(_definition("note", NoteArgs))

    assert registry.specs() == [
        ToolSpec(
            name="lookup",
            description="lookup description",
            input_schema=LookupArgs.model_json_schema(),
        ),
        ToolSpec(
            name="note",
            description="note description",
            input_schema=NoteArgs.model_json_schema(),
        ),
    ]
    assert registry.names() == ["lookup", "note"]


def test_get_returns_the_definition_or_none() -> None:
    registry = ToolRegistry()
    definition = _definition()
    registry.register(definition)

    assert registry.get("lookup") is definition
    assert registry.get("missing") is None


def test_is_destructive_reflects_the_definition_and_is_false_for_unknown_tools() -> None:
    registry = ToolRegistry()
    registry.register(_definition("lookup", destructive=False))
    registry.register(_definition("cancel", destructive=True))

    assert registry.is_destructive("cancel") is True
    assert registry.is_destructive("lookup") is False
    assert registry.is_destructive("missing") is False


# --- registration rules (TOOL-03, TOOL-18) --------------------------------------------------


@pytest.mark.parametrize("field_name", sorted(IDENTITY_FIELDS))
def test_register_rejects_an_identity_field(field_name: str) -> None:
    args_model = type(
        f"Args_{field_name}",
        (BaseModel,),
        {
            "__annotations__": {field_name: str},
            "model_config": ConfigDict(extra="forbid"),
        },
    )
    registry = ToolRegistry()

    with pytest.raises(InvalidToolDefinition, match=field_name):
        registry.register(_definition("leaky", args_model))
    assert registry.names() == []


def test_register_rejects_customer_id() -> None:
    class CustomerArgs(BaseModel):
        model_config = ConfigDict(extra="forbid")
        order_id: int
        customer_id: uuid.UUID

    with pytest.raises(InvalidToolDefinition, match="customer_id"):
        ToolRegistry().register(_definition("leaky", CustomerArgs))


@pytest.mark.parametrize("field_name", sorted(IDENTITY_FIELDS))
def test_register_rejects_an_identity_field_in_a_nested_model(field_name: str) -> None:
    inner = type(
        f"Inner_{field_name}",
        (BaseModel,),
        {
            "__annotations__": {field_name: str},
            "model_config": ConfigDict(extra="forbid"),
        },
    )

    class Outer(BaseModel):
        model_config = ConfigDict(extra="forbid")
        items: list[inner] | None = None  # type: ignore[valid-type]

    with pytest.raises(InvalidToolDefinition, match=field_name):
        ToolRegistry().register(_definition("leaky", Outer))


def test_register_rejects_an_identity_field_exposed_through_an_alias() -> None:
    class AliasArgs(BaseModel):
        model_config = ConfigDict(extra="forbid")
        target: str = Field(alias="recipient")

    with pytest.raises(InvalidToolDefinition, match="recipient"):
        ToolRegistry().register(_definition("leaky", AliasArgs))


def test_register_rejects_an_identity_field_with_different_casing() -> None:
    class CamelArgs(BaseModel):
        model_config = ConfigDict(extra="forbid")
        customerId: str  # the camelCase spelling is the point of the test

    with pytest.raises(InvalidToolDefinition, match="customerId"):
        ToolRegistry().register(_definition("leaky", CamelArgs))


@pytest.mark.parametrize("extra", [None, "ignore", "allow"])
def test_register_rejects_args_model_without_extra_forbid(extra: str | None) -> None:
    config = ConfigDict() if extra is None else ConfigDict(extra=extra)  # type: ignore[typeddict-item]
    args_model = type(
        "LooseArgs",
        (BaseModel,),
        {"__annotations__": {"order_id": int}, "model_config": config},
    )

    with pytest.raises(InvalidToolDefinition, match="extra"):
        ToolRegistry().register(_definition("loose", args_model))


def test_register_rejects_a_nested_model_without_extra_forbid() -> None:
    class LooseInner(BaseModel):
        sku: str

    class Outer(BaseModel):
        model_config = ConfigDict(extra="forbid")
        item: LooseInner

    with pytest.raises(InvalidToolDefinition, match="extra"):
        ToolRegistry().register(_definition("loose", Outer))


@pytest.mark.parametrize("name", ["", "   "])
def test_register_rejects_an_empty_name(name: str) -> None:
    with pytest.raises(InvalidToolDefinition):
        ToolRegistry().register(_definition(name))


def test_register_rejects_a_duplicate_name() -> None:
    registry = ToolRegistry()
    first = _definition("lookup")
    registry.register(first)

    with pytest.raises(InvalidToolDefinition, match="lookup"):
        registry.register(_definition("lookup", NoteArgs))
    assert registry.get("lookup") is first


# --- execute (TOOL-94, AGT-91) ----------------------------------------------------------------


async def test_execute_unknown_tool_returns_unknown_tool() -> None:
    result = await ToolRegistry().execute(_call("delete_everything", {}), _ctx())

    assert result == ToolResult(
        ok=False,
        error_code=ToolErrorCode.UNKNOWN_TOOL,
        content={"tool": "delete_everything"},
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"order_id": "not-a-number"},
        {"order_id": 0},
        {"order_id": 7, "customer_id": "00000000-0000-0000-0000-000000000000"},
    ],
)
async def test_execute_invalid_arguments_does_not_call_the_handler(
    arguments: dict[str, Any],
) -> None:
    handler = RecordingHandler()
    registry = ToolRegistry()
    registry.register(_definition(handler=handler))

    result = await registry.execute(_call("lookup", arguments), _ctx())

    assert result.ok is False
    assert result.error_code == ToolErrorCode.INVALID_ARGUMENTS
    assert handler.calls == []
    errors = result.content["errors"]
    assert isinstance(errors, list) and errors
    for error in errors:
        assert set(error) == {"loc", "msg", "type"}
        assert isinstance(error["loc"], list)
        assert isinstance(error["msg"], str)


async def test_execute_invalid_arguments_do_not_echo_the_input_value() -> None:
    registry = ToolRegistry()
    registry.register(_definition())

    result = await registry.execute(_call("lookup", {"order_id": "secret-value-xyz"}), _ctx())

    assert result.error_code == ToolErrorCode.INVALID_ARGUMENTS
    assert "secret-value-xyz" not in repr(result.content)


async def test_execute_extra_customer_id_is_rejected_as_extra_forbidden() -> None:
    handler = RecordingHandler()
    registry = ToolRegistry()
    registry.register(_definition(handler=handler))

    result = await registry.execute(
        _call("lookup", {"order_id": 7, "customer_id": str(uuid.uuid4())}), _ctx()
    )

    assert result.error_code == ToolErrorCode.INVALID_ARGUMENTS
    assert {"loc": ["customer_id"], "type": "extra_forbidden"}.items() <= {
        "loc": result.content["errors"][0]["loc"],
        "type": result.content["errors"][0]["type"],
    }.items()
    assert handler.calls == []


async def test_execute_passes_validated_args_and_the_context_to_the_handler() -> None:
    handler = RecordingHandler()
    registry = ToolRegistry()
    registry.register(_definition(handler=handler))
    ctx = _ctx()

    result = await registry.execute(_call("lookup", {"order_id": "42"}), ctx)

    assert result == ToolResult(ok=True, content={"echo": {"order_id": 42}})
    assert len(handler.calls) == 1
    args, received_ctx = handler.calls[0]
    assert isinstance(args, LookupArgs)
    assert args.order_id == 42
    assert received_ctx is ctx


async def test_execute_does_not_swallow_handler_exceptions() -> None:
    async def boom(args: Any, ctx: ToolContext) -> ToolResult:
        raise RuntimeError("handler exploded")

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="boom",
            description="boom",
            args_model=LookupArgs,
            destructive=False,
            handler=boom,
        )
    )

    with pytest.raises(RuntimeError, match="handler exploded"):
        await registry.execute(_call("boom", {"order_id": 1}), _ctx())


# --- precheck (read-only pre-validation before asking for confirmation) ----------------------


async def test_precheck_unknown_tool_returns_the_same_result_as_execute() -> None:
    registry = ToolRegistry()
    call = _call("delete_everything", {})

    result = await registry.precheck(call, _ctx())

    assert result == ToolResult(
        ok=False,
        error_code=ToolErrorCode.UNKNOWN_TOOL,
        content={"tool": "delete_everything"},
    )
    assert result == await registry.execute(call, _ctx())


async def test_precheck_invalid_arguments_returns_the_same_result_and_calls_nothing() -> None:
    handler = RecordingHandler()
    precheck = RecordingHandler()
    registry = ToolRegistry()
    registry.register(_definition(handler=handler, precheck=precheck))
    call = _call("lookup", {"order_id": 0, "customer_id": str(uuid.uuid4())})

    result = await registry.precheck(call, _ctx())

    assert result is not None
    assert result.ok is False
    assert result.error_code == ToolErrorCode.INVALID_ARGUMENTS
    assert result == await registry.execute(call, _ctx())
    assert precheck.calls == []
    assert handler.calls == []


async def test_precheck_without_a_precheck_function_returns_none() -> None:
    handler = RecordingHandler()
    registry = ToolRegistry()
    registry.register(_definition(handler=handler))

    assert await registry.precheck(_call("lookup", {"order_id": 7}), _ctx()) is None
    assert handler.calls == []


async def test_precheck_ok_returns_none_after_passing_validated_args_and_ctx() -> None:
    handler = RecordingHandler()
    precheck = RecordingHandler()
    registry = ToolRegistry()
    registry.register(_definition(handler=handler, precheck=precheck))
    ctx = _ctx()

    assert await registry.precheck(_call("lookup", {"order_id": "7"}), ctx) is None
    assert len(precheck.calls) == 1
    args, received_ctx = precheck.calls[0]
    assert isinstance(args, LookupArgs)
    assert args.order_id == 7
    assert received_ctx is ctx
    assert handler.calls == []


async def test_precheck_failure_returns_the_precheck_result_without_calling_the_handler() -> None:
    handler = RecordingHandler()
    precheck = RefusingPrecheck()
    registry = ToolRegistry()
    registry.register(_definition(handler=handler, precheck=precheck))

    result = await registry.precheck(_call("lookup", {"order_id": 7}), _ctx())

    assert result == ToolResult(
        ok=False, error_code=ToolErrorCode.ORDER_NOT_CANCELLABLE, content={"order_id": 7}
    )
    assert len(precheck.calls) == 1
    assert handler.calls == []


async def test_precheck_does_not_swallow_precheck_exceptions() -> None:
    async def boom(args: Any, ctx: ToolContext) -> ToolResult:
        raise RuntimeError("precheck exploded")

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="boom",
            description="boom",
            args_model=LookupArgs,
            destructive=True,
            handler=RecordingHandler(),
            precheck=boom,
        )
    )

    with pytest.raises(RuntimeError, match="precheck exploded"):
        await registry.precheck(_call("boom", {"order_id": 1}), _ctx())


def test_precheck_defaults_to_none() -> None:
    definition = ToolDefinition(
        name="lookup",
        description="d",
        args_model=LookupArgs,
        destructive=False,
        handler=RecordingHandler(),
    )

    assert definition.precheck is None
