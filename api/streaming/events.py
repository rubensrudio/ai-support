"""SSE encoding of agent events (CT-50, STR-01, STR-05, STR-90).

- `encode_sse(event)`: `event: <type>\\ndata: <json>\\n\\n` (UTF-8), `<json>` = the event's
  `model_dump(mode='json')` on one line (no newline inside `data`).
- `guard_terminal(events)`: passes events through and guarantees that the stream ends with
  exactly one terminal event: at most one `done`, always last; nothing after an `error` (an
  `error` ends the stream and no `done` follows it); events after a `done` are dropped.
"""

from collections.abc import AsyncIterator

from api.agent.events import AgentEvent


def encode_sse(event: AgentEvent) -> bytes:
    raise NotImplementedError("TASK-093")


def guard_terminal(events: AsyncIterator[AgentEvent]) -> AsyncIterator[AgentEvent]:
    raise NotImplementedError("TASK-093")
