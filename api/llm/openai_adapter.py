"""OpenAI adapter, the secondary provider (TASK-114, DA-14, RES-05, CACHE-90, TRI-02).

Via the `openai` SDK with `max_retries=0` and `settings.llm_timeout_seconds`, model
`settings.secondary_model`, key `settings.openai_api_key`: `ToolSpec` -> `tools[type=function]`
(`parameters` = `input_schema`), `tool_calls` -> `ToolCall` (arguments parsed to dict),
`ChatMessage(role='tool')` -> `role='tool'` with `tool_call_id`, `response_schema` ->
`response_format` json_schema strict, system blocks joined as the system message, `stream()` of
text deltas / tool calls / usage, and the automatic cache read from
`usage.prompt_tokens_details.cached_tokens` (subtracted from `input_tokens`). 429/5xx/timeouts
-> recoverable `LLMError`; 400/401/403 -> non-recoverable.
"""

from collections.abc import AsyncIterator

from api.config import Settings
from api.llm.client import LLMRequest, LLMResponse, LLMStreamEvent

PROVIDER = "openai"


class OpenAIAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError("TASK-114")

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        raise NotImplementedError("TASK-114")
