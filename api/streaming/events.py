"""SSE encoding of agent events (CT-50, STR-01, STR-05, STR-90).

- `encode_sse(event)`: `event: <type>\\ndata: <json>\\n\\n` (UTF-8), `<json>` = the event's
  `model_dump_json()`: compact, on one line (newlines inside strings are JSON-escaped, so `data`
  never spans lines) and with non-ASCII characters kept as UTF-8 instead of `\\uXXXX` escapes.
- `guard_terminal(events)`: passes events through and guarantees that the stream ends with
  exactly one terminal event: at most one `done`, always last; nothing after an `error` (an
  `error` ends the stream and no `done` follows it); events after a `done` are dropped.

  The guard stops pulling from the source as soon as it forwards the first terminal event and
  closes the source (`aclose()`, when the source has one) so its cleanup runs right away. The
  source is also closed when the consumer closes the guard early or an exception propagates.

  If the source ends without a terminal event, the guard does NOT invent a `done`: it cannot
  know the status, trace id or content a `done` must carry. The caller (the SSE route) decides
  what to emit in that case, typically an `error`.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import cast

from api.agent.events import AgentEvent

_TERMINAL_TYPES = frozenset({"done", "error"})


def encode_sse(event: AgentEvent) -> bytes:
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n".encode()


async def guard_terminal(events: AsyncIterator[AgentEvent]) -> AsyncIterator[AgentEvent]:
    try:
        async for event in events:
            yield event
            if event.type in _TERMINAL_TYPES:
                return
    finally:
        await _close(events)


async def _close(events: AsyncIterator[AgentEvent]) -> None:
    aclose = getattr(events, "aclose", None)
    if callable(aclose):
        await cast(Callable[[], Awaitable[object]], aclose)()
