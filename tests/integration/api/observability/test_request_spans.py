"""Spans of one chat request (TASK-133, OBS-01, OBS-91, CT-62).

A chat request with a scripted fake that calls a tool, with retrieval and guardrails on, must
export `request`, `guardrail`, `retrieval`, `llm` and `tool` spans that share the request trace
id (`X-Trace-Id`), carry `langfuse.session.id = conversation_id` and, on `llm`, the token and
cost attributes. A card number typed by the customer only appears masked. A failing primary
provider adds a `fallback` span.
"""

import asyncio
import contextvars
import json
import uuid
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.auth.deps import CurrentCustomer, require_customer
from api.auth.models import Customer
from api.chat.models import Message
from api.chat.service import ChatService, get_chat_service
from api.config import Settings, get_settings
from api.llm.circuit_breaker import CircuitBreaker
from api.llm.client import (
    ChatMessage,
    LLMClient,
    LLMError,
    LLMRequest,
    LLMResponse,
    SystemBlock,
    ToolCall,
    Usage,
)
from api.llm.fake import FAKE_MODEL, FAKE_PROVIDER, FakeLLMClient
from api.llm.fallback import FallbackLLMClient
from api.llm.instrumented import PROMPT_ATTRIBUTE_MAX_CHARS, InstrumentedLLMClient
from api.main import create_app
from api.messages import Lang
from api.observability.logging import get_trace_id
from api.observability.tracing import (
    ATTR_COST,
    ATTR_FALLBACK,
    ATTR_INPUT_TOKENS,
    ATTR_MODEL,
    ATTR_OUTPUT_TOKENS,
    ATTR_SESSION_ID,
)
from api.rag import retriever as retriever_module
from api.rag.fulltext import ScoredChunk
from api.rag.retriever import Retriever, register_mode
from api.tools.models import Order, OrderStatus
from api.tools.registry import (
    TOOL_CONTENT_MAX_CHARS,
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
)

pytestmark = pytest.mark.integration

CARD = "4111 1111 1111 1111"
SCORE = 0.87
PATHS = ["/chat", "/chat/stream"]


def _response(text: str = "", calls: list[ToolCall] | None = None) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=list(calls or []),
        usage=Usage(input_tokens=10, output_tokens=5, cache_read_tokens=2),
        model=FAKE_MODEL,
        provider=FAKE_PROVIDER,
        stop_reason="tool_use" if calls else "end_turn",
    )


def _settings(**overrides: object) -> Settings:
    values: dict[str, Any] = {"llm_provider": "fake", "retrieval_mode": "fulltext"}
    values.update(overrides)
    return get_settings().model_copy(update=values)


def _principal(customer: Customer) -> CurrentCustomer:
    return CurrentCustomer(
        id=customer.id,
        email=customer.email,
        display_name=customer.display_name,
        language="en",
    )


@pytest.fixture
def scripted_search(monkeypatch: pytest.MonkeyPatch, chunk_id: int) -> None:
    """`fulltext` returns the seeded chunk with a fixed score (isolated registry)."""
    monkeypatch.setattr(retriever_module, "_REGISTRY", dict(retriever_module._REGISTRY))

    async def search(session: AsyncSession, query: str, language: Lang, k: int) -> list[Any]:
        return [ScoredChunk(chunk_id=chunk_id, score=SCORE)]

    register_mode("fulltext", search)


def _app(service: ChatService, customer: Customer) -> FastAPI:
    app = create_app()
    app.dependency_overrides[require_customer] = lambda: _principal(customer)
    app.dependency_overrides[get_chat_service] = lambda: service
    return app


async def _post(app: FastAPI, path: str, message: str) -> tuple[httpx.Response, str]:
    """POST the message; returns the response and the conversation id."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post(path, json={"message": message})
    assert response.status_code == 200, response.text
    if path == "/chat":
        return response, str(response.json()["conversation_id"])
    for block in response.text.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line)
        if lines.get("event") == "start":
            return response, str(json.loads(lines["data"])["conversation_id"])
    raise AssertionError(f"no start event in {response.text!r}")


def _named(spans: list[ReadableSpan], name: str) -> list[ReadableSpan]:
    return [span for span in spans if span.name == name]


def _attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


def _strings(spans: list[ReadableSpan]) -> list[str]:
    values: list[str] = []
    for span in spans:
        for value in _attrs(span).values():
            items = value if isinstance(value, tuple | list) else [value]
            values.extend(str(item) for item in items)
    return values


@pytest.mark.parametrize("path", PATHS)
async def test_chat_request_exports_child_spans_with_one_trace_id(
    path: str,
    exporter: InMemorySpanExporter,
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    customer: Customer,
    order: Order,
    chunk_id: int,
    scripted_search: None,
) -> None:
    call = ToolCall(id="call-get", name="get_order", arguments={"order_id": order.id})
    fake = FakeLLMClient([_response(calls=[call]), _response("It has shipped.")])
    llm = InstrumentedLLMClient(fake, FAKE_MODEL, FAKE_PROVIDER)
    settings = _settings()
    retriever = Retriever(session_factory, None, settings)
    service = ChatService(llm, settings, db_session, retriever=retriever)

    response, conversation_id = await _post(
        _app(service, customer), path, f"Where is my order #{order.id}? My card is {CARD}."
    )

    spans = list(exporter.get_finished_spans())
    trace_id = response.headers["x-trace-id"]
    names = {span.name for span in spans}
    assert {"request", "guardrail", "retrieval", "llm", "tool"} <= names
    assert {format(span.context.trace_id, "032x") for span in spans} == {trace_id}
    [request] = _named(spans, "request")
    assert request.parent is None
    assert all(span.parent is not None for span in spans if span is not request)

    for span in _named(spans, "llm") + _named(spans, "tool"):
        assert _attrs(span)[ATTR_SESSION_ID] == conversation_id

    chat_llm = [span for span in _named(spans, "llm") if _attrs(span)[ATTR_INPUT_TOKENS] == 10]
    assert len(chat_llm) == 2
    for span in chat_llm:
        attrs = _attrs(span)
        assert attrs[ATTR_MODEL] == FAKE_MODEL
        assert attrs["gen_ai.system"] == FAKE_PROVIDER
        assert attrs[ATTR_OUTPUT_TOKENS] == 5
        assert attrs["gen_ai.usage.cache_read_input_tokens"] == 2
        assert attrs["gen_ai.usage.cache_creation_input_tokens"] == 0
        assert isinstance(attrs[ATTR_COST], float) and attrs[ATTR_COST] >= 0
        assert attrs["gen_ai.usage.cost"] == attrs[ATTR_COST]
        assert attrs["atlas.latency_ms"] >= 0
        assert attrs["atlas.outcome"] == "ok"
        assert "[CARD]" in attrs["gen_ai.prompt"]

    [retrieval] = _named(spans, "retrieval")
    assert _attrs(retrieval)["atlas.retrieval.mode"] == "fulltext"
    assert list(_attrs(retrieval)["atlas.retrieval.chunk_ids"]) == [chunk_id]
    assert list(_attrs(retrieval)["atlas.retrieval.scores"]) == [SCORE]

    [tool] = _named(spans, "tool")
    assert _attrs(tool)["atlas.tool.name"] == "get_order"
    assert json.loads(_attrs(tool)["atlas.tool.arguments"]) == {"order_id": order.id}
    assert _attrs(tool)["atlas.tool.ok"] is True

    kinds = {_attrs(span)["atlas.guardrail.type"] for span in _named(spans, "guardrail")}
    assert "input" in kinds
    assert all("atlas.guardrail.verdict" in _attrs(s) for s in _named(spans, "guardrail"))

    assert not any(CARD in value for value in _strings(spans))
    stored = (
        await db_session.execute(
            select(Message.content).where(
                Message.conversation_id == uuid.UUID(conversation_id), Message.role == "user"
            )
        )
    ).scalar_one()
    assert CARD not in stored and "[CARD]" in stored
    message_trace_ids = (
        await db_session.execute(
            select(Message.trace_id).where(Message.conversation_id == uuid.UUID(conversation_id))
        )
    ).scalars()
    assert set(message_trace_ids) == {trace_id}


class _Failing(FakeLLMClient):
    """Primary provider that is down (recoverable error on every call)."""

    def __init__(self) -> None:
        super().__init__([LLMError("primary down", recoverable=True, status=503)] * 4)


def _breaker(name: str) -> CircuitBreaker:
    return CircuitBreaker(name, failure_threshold=5, reset_seconds=30)


@pytest.mark.parametrize("path", PATHS)
async def test_failing_primary_exports_a_fallback_span(
    path: str,
    exporter: InMemorySpanExporter,
    db_session: AsyncSession,
    customer: Customer,
) -> None:
    secondary = FakeLLMClient([_response("Hello there.")])
    chain: list[tuple[str, LLMClient, CircuitBreaker]] = [
        ("primary", InstrumentedLLMClient(_Failing(), FAKE_MODEL, "primary"), _breaker("p")),
        ("secondary", InstrumentedLLMClient(secondary, FAKE_MODEL, "secondary"), _breaker("s")),
    ]
    settings = _settings(retrieval_mode="none", guardrails_enabled=False)
    service = ChatService(FallbackLLMClient(chain), settings, db_session)

    response, conversation_id = await _post(_app(service, customer), path, "Hello, how are you?")

    spans = list(exporter.get_finished_spans())
    trace_id = response.headers["x-trace-id"]
    assert {format(span.context.trace_id, "032x") for span in spans} == {trace_id}
    [fallback] = _named(spans, "fallback")
    assert _attrs(fallback)[ATTR_FALLBACK] == "primary->secondary"
    assert _attrs(fallback)["atlas.fallback.reason"] == "LLMError"
    assert _attrs(fallback)[ATTR_SESSION_ID] == conversation_id
    outcomes = {
        _attrs(span)["gen_ai.system"]: _attrs(span)["atlas.outcome"]
        for span in _named(spans, "llm")
    }
    assert outcomes == {"primary": "error", "secondary": "ok"}


async def test_resumed_confirmation_spans_follow_the_new_request(
    exporter: InMemorySpanExporter,
    db_session: AsyncSession,
    customer: Customer,
) -> None:
    order = Order(
        customer_id=customer.id,
        status=OrderStatus.PAID.value,
        total_cents=4990,
        currency="USD",
        items=[{"sku": "box-coffee", "name": "Coffee box", "qty": 1}],
    )
    db_session.add(order)
    await db_session.flush()
    call = ToolCall(id="call-cancel", name="cancel_order", arguments={"order_id": order.id})
    fake = FakeLLMClient([_response(calls=[call]), _response(f"Order #{order.id} is cancelled.")])
    llm = InstrumentedLLMClient(fake, FAKE_MODEL, FAKE_PROVIDER)
    service = ChatService(llm, _settings(retrieval_mode="none"), db_session)
    app = _app(service, customer)

    asked, conversation_id = await _post(app, "/chat", f"Please cancel my order #{order.id}")
    waiting = asked.json()
    assert waiting["status"] == "awaiting_confirmation"
    exporter.clear()

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        done = await http.post(
            f"/chat/confirmations/{waiting['confirmation']['id']}", json={"decision": "confirm"}
        )
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["status"] == "completed"

    trace_id = done.headers["x-trace-id"]
    assert trace_id != asked.headers["x-trace-id"]
    spans = list(exporter.get_finished_spans())
    assert {"request", "tool", "llm"} <= {span.name for span in spans}
    assert {format(span.context.trace_id, "032x") for span in spans} == {trace_id}
    [tool] = _named(spans, "tool")
    assert _attrs(tool)["atlas.tool.name"] == "cancel_order"
    for span in _named(spans, "llm") + _named(spans, "tool"):
        assert _attrs(span)[ATTR_SESSION_ID] == conversation_id
    stored_trace = (
        await db_session.execute(select(Message.trace_id).where(Message.id == body["message_id"]))
    ).scalar_one()
    assert stored_trace == trace_id


def _boundary_text(limit: int, prefix: int, pii: str) -> str:
    """Filler that makes `pii` start a few characters before `limit` in the serialized value."""
    return "a " * ((limit - prefix - 14) // 2) + f" {pii} trailing words"


async def test_prompt_pii_across_the_cut_is_masked(exporter: InMemorySpanExporter) -> None:
    email = "alex.silva@example.com"
    prefix = len('{"system": ["')
    request = LLMRequest(
        system=[SystemBlock(text=_boundary_text(PROMPT_ATTRIBUTE_MAX_CHARS, prefix, email))],
        messages=[ChatMessage(role="user", content=f"card {CARD}")],
    )
    llm = InstrumentedLLMClient(FakeLLMClient([_response("ok")]), FAKE_MODEL, FAKE_PROVIDER)

    await llm.complete(request)

    [span] = _named(list(exporter.get_finished_spans()), "llm")
    prompt = str(_attrs(span)["gen_ai.prompt"])
    assert len(prompt) <= PROMPT_ATTRIBUTE_MAX_CHARS
    assert "alex.silva" not in prompt and "@exa" not in prompt
    assert "[EMAIL]" in prompt


class _NoteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def test_tool_result_pii_across_the_cut_is_masked(exporter: InMemorySpanExporter) -> None:
    prefix = len('{"note": "')
    note = _boundary_text(TOOL_CONTENT_MAX_CHARS, prefix, CARD)

    async def handler(args: Any, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, content={"note": note})

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="note", description="d", args_model=_NoteArgs, destructive=False, handler=handler
        )
    )
    ctx = cast(ToolContext, None)

    await registry.execute(ToolCall(id="c", name="note", arguments={}), ctx)

    [span] = _named(list(exporter.get_finished_spans()), "tool")
    result = str(_attrs(span)["atlas.tool.result"])
    assert len(result) <= TOOL_CONTENT_MAX_CHARS
    assert "4111" not in result
    assert "[CARD]" in result


async def test_llm_span_outside_a_request_shares_the_log_trace_id(
    exporter: InMemorySpanExporter, caplog: pytest.LogCaptureFixture
) -> None:
    llm = InstrumentedLLMClient(FakeLLMClient([_response("ok")]), FAKE_MODEL, FAKE_PROVIDER)
    request = LLMRequest(system=[], messages=[ChatMessage(role="user", content="hi")])

    async def call() -> str:
        await llm.complete(request)
        return get_trace_id()

    # A fresh context: no request trace id is bound yet.
    caller_trace_id = await asyncio.create_task(call(), context=contextvars.Context())

    [span] = _named(list(exporter.get_finished_spans()), "llm")
    assert format(span.context.trace_id, "032x") == caller_trace_id
    logged = [r for r in caplog.records if getattr(r, "event", None) == "llm_call"]
    assert [getattr(r, "trace_id", None) for r in logged] == [caller_trace_id]
