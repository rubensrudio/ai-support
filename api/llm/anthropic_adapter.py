"""Anthropic adapter (CT-6 over the `anthropic` SDK).

This is the only module that imports the Anthropic SDK. It converts `LLMRequest` into a Messages
API call and normalizes the response, token usage and errors. The SDK runs with `max_retries=0`:
retries belong to our resilience chain (DA-14). Structured output uses the SDK's native
`output_config.format` (`json_schema`), so the JSON comes back in `LLMResponse.text`.
The SDK (2.x) runs on `httpx2`, so an injected HTTP client must be an `httpx2.AsyncClient`.
"""

from collections.abc import AsyncIterator

import httpx2
from anthropic import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
    omit,
)
from anthropic.types import (
    Message,
    MessageParam,
    OutputConfigParam,
    TextBlockParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlockParam,
)

from api.config import MissingConfigError, Settings
from api.llm.client import (
    ChatMessage,
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    LLMTimeoutError,
    SystemBlock,
    ToolCall,
    ToolSpec,
    Usage,
)

PROVIDER = "anthropic"
RECOVERABLE_STATUSES = frozenset({408, 409, 429})


def is_recoverable_status(status: int) -> bool:
    """429, 408/409 and every 5xx (including Anthropic's 529 overloaded) are worth retrying."""
    return status in RECOVERABLE_STATUSES or status >= 500


def _system_blocks(system: list[SystemBlock]) -> list[TextBlockParam]:
    # cache_breakpoint is ignored until phase 11 (prompt cache).
    return [{"type": "text", "text": block.text} for block in system if block.text]


def _tool_params(tools: list[ToolSpec]) -> list[ToolParam]:
    return [
        {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
        for tool in tools
    ]


def _assistant_message(message: ChatMessage) -> MessageParam:
    if not message.tool_calls:
        return {"role": "assistant", "content": message.content}
    blocks: list[TextBlockParam | ToolUseBlockParam] = []
    if message.content:
        blocks.append({"type": "text", "text": message.content})
    blocks.extend(
        {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
        for call in message.tool_calls
    )
    return {"role": "assistant", "content": blocks}


def _messages(messages: list[ChatMessage]) -> list[MessageParam]:
    """Map our messages to Messages API turns.

    Consecutive `tool` messages answer one assistant turn, so they are merged into a single
    `user` turn of `tool_result` blocks, as the API requires.
    """
    converted: list[MessageParam] = []
    pending_results: list[ToolResultBlockParam] = []

    def flush_results() -> None:
        if pending_results:
            converted.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for message in messages:
        if message.role == "tool":
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id or "",
                    "content": message.content,
                }
            )
            continue
        flush_results()
        if message.role == "assistant":
            converted.append(_assistant_message(message))
        else:
            converted.append({"role": "user", "content": message.content})
    flush_results()
    return converted


def _require[T](value: object, kind: type[T], field: str) -> T:
    """The SDK builds response models without validation; reject fields of the wrong type."""
    if not isinstance(value, kind) or isinstance(value, bool) and kind is int:
        raise ValueError(f"malformed response field: {field}")
    return value


def _to_response(message: Message) -> LLMResponse:
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in _require(message.content, list, "content"):
        if block.type == "text":
            texts.append(_require(block.text, str, "text"))
        elif block.type == "tool_use":
            calls.append(
                ToolCall(
                    id=_require(block.id, str, "tool_use.id"),
                    name=_require(block.name, str, "tool_use.name"),
                    arguments=dict(_require(block.input, dict, "tool_use.input")),
                )
            )
    usage = message.usage
    return LLMResponse(
        text="".join(texts),
        tool_calls=calls,
        usage=Usage(
            input_tokens=_require(usage.input_tokens, int, "usage.input_tokens"),
            output_tokens=_require(usage.output_tokens, int, "usage.output_tokens"),
            cache_read_tokens=_require(usage.cache_read_input_tokens or 0, int, "usage"),
            cache_write_tokens=_require(usage.cache_creation_input_tokens or 0, int, "usage"),
        ),
        model=_require(message.model, str, "model"),
        provider=PROVIDER,
        stop_reason=message.stop_reason,
    )


def _malformed_response(exc: Exception) -> LLMError:
    return LLMError(
        f"Anthropic API returned a malformed response ({type(exc).__name__})",
        recoverable=False,
    )


class AnthropicAdapter:
    """`LLMClient` over the Anthropic Messages API."""

    def __init__(
        self,
        settings: Settings,
        model: str | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        key = settings.anthropic_api_key
        if key is None or not key.get_secret_value():
            raise MissingConfigError("ANTHROPIC_API_KEY")
        resolved_model = model or settings.primary_model
        if not resolved_model:
            raise MissingConfigError("ATLAS_PRIMARY_MODEL")
        self.model = resolved_model
        self._client = AsyncAnthropic(
            api_key=key.get_secret_value(),
            max_retries=0,
            timeout=settings.llm_timeout_seconds,
            http_client=http_client,
        )

    @property
    def max_retries(self) -> int:
        return self._client.max_retries

    @property
    def timeout_seconds(self) -> float | None:
        timeout = self._client.timeout
        return timeout if isinstance(timeout, float | int | None) else timeout.read

    async def complete(self, request: LLMRequest) -> LLMResponse:
        system = _system_blocks(request.system)
        tools = _tool_params(request.tools)
        messages = _messages(request.messages)
        output_config: OutputConfigParam | None = None
        if request.response_schema is not None:
            output_config = {"format": {"type": "json_schema", "schema": request.response_schema}}
        try:
            message = await self._client.messages.create(
                model=self.model,
                max_tokens=request.max_tokens,
                messages=messages,
                system=system if system else omit,
                tools=tools if tools else omit,
                output_config=output_config if output_config is not None else omit,
            )
        # Errors are re-raised `from None`: the SDK exception carries the HTTP request (with the
        # API key header) and the raw body, which must never reach logs (FND-90).
        except APITimeoutError:
            raise LLMTimeoutError("Anthropic API call timed out") from None
        except APIStatusError as exc:
            status = exc.status_code
            raise LLMError(
                f"Anthropic API returned HTTP {status}",
                recoverable=is_recoverable_status(status),
                status=status,
            ) from None
        except APIConnectionError:
            raise LLMError("Anthropic API connection failed", recoverable=True) from None
        except APIError as exc:
            # Any other SDK error (e.g. response validation): name the type only.
            raise LLMError(
                f"Anthropic API error ({type(exc).__name__})", recoverable=False
            ) from None
        except ValueError as exc:
            # A 200 whose body is not JSON (JSONDecodeError) or fails SDK validation; the
            # exception text quotes the body, so only its type is kept.
            raise _malformed_response(exc) from None
        try:
            return _to_response(message)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            # A 200 that parsed but is not a Message (wrong shape, null fields, bad blocks).
            raise _malformed_response(exc) from None

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        raise NotImplementedError("Anthropic streaming is implemented in phase 9")
