"""Shared types of the demo limit contract tests (unique module name, imported bare)."""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from api.llm.client import LLMRequest, LLMResponse, LLMStreamEvent, Usage
from api.llm.fake import split_deltas

PERSONA_PASSWORD = "limits-contract-pass-7a3c"  # noqa: S105 - fictitious test credential
OPERATOR_KEY = "limits-contract-operator-key-91fe"
RATE_LIMIT_PER_HOUR = 3
DAILY_CAP_USD = Decimal("2.00")
CHAT_ANSWER = "Happy to help with that."
VALID_TRIAGE: dict[str, Any] = {
    "categoria": "reembolso",
    "prioridade": "alta",
    "sentimento": "negativo",
    "precisa_humano": True,
    "resumo": "Customer asks for a refund of a damaged order.",
}


class CountingFakeLLM:
    """Answers every call with the same text and records each request (`calls`)."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.requests: list[LLMRequest] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    def _response(self) -> LLMResponse:
        return LLMResponse(
            text=self._text,
            tool_calls=[],
            usage=Usage(input_tokens=20, output_tokens=10),
            model="fake-model",
            provider="fake",
            stop_reason="end_turn",
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return self._response()

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        self.requests.append(request)
        response = self._response()
        for chunk in split_deltas(response.text):
            yield LLMStreamEvent(kind="text_delta", text=chunk)
        yield LLMStreamEvent(kind="usage", usage=response.usage)
        yield LLMStreamEvent(kind="stop", stop_reason=response.stop_reason)


@dataclass(frozen=True)
class Persona:
    id: uuid.UUID
    username: str
    language: str
    headers: dict[str, str]


@dataclass
class FakeLLMs:
    chat: CountingFakeLLM
    triage: CountingFakeLLM

    @property
    def calls(self) -> int:
        return self.chat.calls + self.triage.calls


def start_of_utc_day(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


SpendFn = Callable[[Decimal, datetime], Awaitable[None]]
CountFn = Callable[[uuid.UUID], Awaitable[int]]
