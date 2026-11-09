"""Provider-independent LLM client contract (CT-6, DA-5).

Every adapter (Anthropic, OpenAI, fake) and every decorator (instrumented, retry, breaker,
chaos, fallback) implements `LLMClient`. Callers never import a provider SDK.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Role = Literal["user", "assistant", "tool"]
StreamEventKind = Literal["text_delta", "tool_call", "usage", "stop"]


@dataclass(frozen=True)
class SystemBlock:
    text: str
    cache_breakpoint: bool = False


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class LLMRequest:
    system: list[SystemBlock]
    messages: list[ChatMessage]
    tools: list[ToolSpec] = field(default_factory=list)
    response_schema: dict[str, Any] | None = None
    max_tokens: int = 1024
    purpose: str = "chat"


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    model: str
    provider: str
    stop_reason: str | None


@dataclass(frozen=True)
class LLMStreamEvent:
    kind: StreamEventKind
    text: str = ""
    tool_call: ToolCall | None = None
    usage: Usage | None = None
    stop_reason: str | None = None


class LLMError(Exception):
    """Provider failure. `recoverable` marks errors worth retrying (429, 5xx, timeout)."""

    def __init__(self, message: str, recoverable: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.recoverable = recoverable
        self.status = status


class LLMTimeoutError(LLMError):
    def __init__(self, message: str = "LLM call timed out", status: int | None = None) -> None:
        super().__init__(message, recoverable=True, status=status)


@runtime_checkable
class LLMClient(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse: ...

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]: ...
