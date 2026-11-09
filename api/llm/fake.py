"""Scripted fake LLM adapter (CT-6).

`FakeLLMClient` replays a script of responses and errors, one item per call, and records every
request it receives. It backs every test and the `--provider fake` mode of the demos, so it never
touches the network and never imports a provider SDK.
"""

import re
from collections import deque
from collections.abc import AsyncIterator

from api.llm.client import LLMError, LLMRequest, LLMResponse, LLMStreamEvent

FAKE_MODEL = "fake-model"
FAKE_PROVIDER = "fake"

_DELTA_PATTERN = re.compile(r"\s*\S+\s*|\s+")


def split_deltas(text: str) -> list[str]:
    """Split text into word-sized chunks whose concatenation is exactly `text`."""
    return _DELTA_PATTERN.findall(text)


class FakeLLMClient:
    """Replays `script` in order. An `LLMError` item is raised on its call."""

    def __init__(self, script: list[LLMResponse | LLMError]) -> None:
        self._script: deque[LLMResponse | LLMError] = deque(script)
        self.requests: list[LLMRequest] = []

    def enqueue(self, *items: LLMResponse | LLMError) -> None:
        """Append items to the end of the script."""
        self._script.extend(items)

    @property
    def remaining(self) -> int:
        return len(self._script)

    def _next(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self._script:
            raise LLMError("fake script exhausted", recoverable=False)
        item = self._script.popleft()
        if isinstance(item, LLMError):
            raise item
        return item

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return self._next(request)

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        response = self._next(request)
        for chunk in split_deltas(response.text):
            yield LLMStreamEvent(kind="text_delta", text=chunk)
        for call in response.tool_calls:
            yield LLMStreamEvent(kind="tool_call", tool_call=call)
        yield LLMStreamEvent(kind="usage", usage=response.usage)
        yield LLMStreamEvent(kind="stop", stop_reason=response.stop_reason)
