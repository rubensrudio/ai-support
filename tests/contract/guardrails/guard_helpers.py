"""Shared helpers for the guardrail contract tests (Phase 10, AS-3).

Written from the contracts only (spec GRD-01/04/06/07/08/91/93, OBS-91; plan CT-52..CT-55,
CT-75, DA-22 and the stub docstrings of `api/guardrails/`). The implementations of the chat
service, the agent loop and the guards are deliberately not used as a source.

`RoutingFakeLLM` is the single `LLMClient` handed to `ChatService`. It sends the chat calls
(the agent loop and the plain chat, `purpose="chat"`) to a scripted `FakeLLMClient`, and every
other call (the topic classifier of the output guard, which uses a structured classification
with the `topic_check` prompt) to `TopicClassifier`, whose answer the test chooses. Routing by
request shape keeps the tests independent of the order in which the service calls the two.
"""

import json
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.chat.models import Conversation, Message
from api.llm.client import LLMError, LLMRequest, LLMResponse, LLMStreamEvent, ToolCall, Usage
from api.llm.fake import FakeLLMClient
from api.messages import Lang, MessageKey, user_message
from api.observability.logging import LOGGER_NAME, JsonFormatter
from api.tools.models import AuditLog, Order

# --- PII samples (all fictitious) -------------------------------------------------------------

CARD = "4111 1111 1111 1111"  # Visa test number, valid Luhn
CARD_DIGITS = "4111111111111111"
CPF = "529.982.247-25"  # valid check digits
CPF_DIGITS = "52998224725"
VISITOR_EMAIL = "visitor.real.person@example.org"
PHONE_US = "(415) 555-0132"
PHONE_BR = "(11) 91234-5678"

RAW_PII = (CARD, CARD_DIGITS, "4111-1111-1111-1111", CPF, CPF_DIGITS, VISITOR_EMAIL, PHONE_US)
RAW_PII_PT = (CARD, CARD_DIGITS, CPF, CPF_DIGITS, VISITOR_EMAIL, PHONE_BR, "91234-5678")
MARKERS = ("[CARD]", "[ID]", "[EMAIL]", "[PHONE]")


def blocked_message(lang: Lang) -> str:
    return user_message(MessageKey.GUARDRAIL_BLOCKED, lang)


# --- Scripted LLM -----------------------------------------------------------------------------


def text_response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        usage=Usage(input_tokens=10, output_tokens=5),
        model="fake-model",
        provider="fake",
        stop_reason="end_turn",
    )


def tool_response(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(
        text="",
        tool_calls=list(calls),
        usage=Usage(input_tokens=10, output_tokens=5),
        model="fake-model",
        provider="fake",
        stop_reason="tool_use",
    )


TopicMode = Literal["on_topic", "off_topic", "error"]


class TopicClassifier:
    """Answers every guard classification call: on topic, off topic, or a provider failure."""

    def __init__(self) -> None:
        self.mode: TopicMode = "on_topic"
        self.requests: list[LLMRequest] = []

    def answer(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self.mode == "error":
            raise LLMError("simulated guardrail classifier outage", recoverable=False)
        on_topic = self.mode == "on_topic"
        reason = "Customer support answer." if on_topic else "Not about customer support."
        return text_response(json.dumps({"on_topic": on_topic, "reason": reason}))


def _request_text(request: LLMRequest) -> str:
    parts = [block.text for block in request.system]
    parts.extend(message.content for message in request.messages)
    return "\n".join(parts)


def is_guard_request(request: LLMRequest) -> bool:
    """A call of the output guard's classifier rather than of the chat/agent."""
    if request.purpose not in {"chat", "summarize"} or request.response_schema is not None:
        return True
    text = _request_text(request)
    return "<answer_start>" in text or "stays on topic" in text


class RoutingFakeLLM:
    """`LLMClient` that routes chat calls to `chat` and guard calls to `classifier`."""

    def __init__(self) -> None:
        self.chat = FakeLLMClient([])
        self.classifier = TopicClassifier()

    def script(self, *items: LLMResponse | LLMError) -> None:
        self.chat.enqueue(*items)

    @property
    def all_requests(self) -> list[LLMRequest]:
        return [*self.chat.requests, *self.classifier.requests]

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if is_guard_request(request):
            return self.classifier.answer(request)
        return await self.chat.complete(request)

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        if is_guard_request(request):
            return self._classifier_stream(request)
        return self.chat.stream(request)

    async def _classifier_stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        response = self.classifier.answer(request)
        yield LLMStreamEvent(kind="text_delta", text=response.text)
        yield LLMStreamEvent(kind="usage", usage=response.usage)
        yield LLMStreamEvent(kind="stop", stop_reason=response.stop_reason)


def request_dump(requests: list[LLMRequest]) -> str:
    """Every text an LLM request carried (system, messages, tool calls and tool results)."""
    parts: list[str] = []
    for request in requests:
        parts.append(_request_text(request))
        for message in request.messages:
            parts.extend(json.dumps(call.arguments) for call in message.tool_calls)
    return "\n".join(parts)


# --- SSE --------------------------------------------------------------------------------------


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """`(event, data)` pairs of an SSE body."""
    parsed: list[tuple[str, dict[str, Any]]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        lines = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line)
        parsed.append((lines["event"], json.loads(lines["data"])))
    return parsed


def streamed_tokens(events: list[tuple[str, dict[str, Any]]]) -> str:
    return "".join(str(data.get("text", "")) for name, data in events if name == "token")


def terminal(events: list[tuple[str, dict[str, Any]]]) -> tuple[str, dict[str, Any]]:
    assert events, "the stream sent no event"
    return events[-1]


# --- Database ---------------------------------------------------------------------------------


async def conversation_messages(session: AsyncSession, conversation_id: uuid.UUID) -> list[Message]:
    statement = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.id)
        .execution_options(populate_existing=True)
    )
    return list((await session.execute(statement)).scalars())


async def customer_messages(session: AsyncSession, customer_id: uuid.UUID) -> list[Message]:
    statement = (
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.customer_id == customer_id)
        .order_by(Message.id)
        .execution_options(populate_existing=True)
    )
    return list((await session.execute(statement)).scalars())


async def audit_rows(session: AsyncSession, customer_id: uuid.UUID) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.customer_id == customer_id).order_by(AuditLog.id)
    return list((await session.execute(statement)).scalars())


async def order_status(session: AsyncSession, order_id: int) -> str:
    statement = (
        select(Order.status).where(Order.id == order_id).execution_options(populate_existing=True)
    )
    return str((await session.execute(statement)).scalar_one())


# --- Logs -------------------------------------------------------------------------------------


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []
        self._formatter = JsonFormatter()

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self._formatter.format(record))


@contextmanager
def capture_atlas_logs() -> Iterator[list[str]]:
    """Every record of the `atlas` logger, rendered as the JSON line production writes."""
    logger = logging.getLogger(LOGGER_NAME)
    handler = _ListHandler()
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler.lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
