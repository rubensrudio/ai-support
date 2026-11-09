"""Streaming with a non-streaming fallback (phase 09, STR-01; user decision for CLI providers).

`stream_or_complete(llm, request)` yields the provider's `LLMStreamEvent`s. When the provider
does not stream (`stream()` raises `NotImplementedError`, as the subscription CLI adapters do),
it falls back to `complete()` and synthesises the same event sequence from the response: one
`text_delta` with the whole text (if any), one `tool_call` per call, `usage`, `stop`. The
fallback only happens before the first event; a `NotImplementedError` after events were
yielded propagates. Other errors (`LLMError`, cancellation) always propagate. Closing this
generator early also closes the provider stream.
"""

from collections.abc import AsyncIterator

from api.llm.client import LLMClient, LLMRequest, LLMResponse, LLMStreamEvent


def events_from_response(response: LLMResponse) -> list[LLMStreamEvent]:
    """The stream-shaped events of a complete response."""
    events: list[LLMStreamEvent] = []
    if response.text:
        events.append(LLMStreamEvent(kind="text_delta", text=response.text))
    events.extend(LLMStreamEvent(kind="tool_call", tool_call=call) for call in response.tool_calls)
    events.append(LLMStreamEvent(kind="usage", usage=response.usage))
    events.append(LLMStreamEvent(kind="stop", stop_reason=response.stop_reason))
    return events


async def stream_or_complete(llm: LLMClient, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
    try:
        source = llm.stream(request)
        first = await anext(source)
    except NotImplementedError:
        for event in events_from_response(await llm.complete(request)):
            yield event
        return
    except StopAsyncIteration:
        return
    try:
        yield first
        async for event in source:
            yield event
    finally:
        # Leaving `async for` does not close `source`: close it now, so an early consumer exit
        # (disconnect, cancellation) stops the provider call instead of waiting for the GC.
        aclose = getattr(source, "aclose", None)
        if callable(aclose):
            await aclose()
