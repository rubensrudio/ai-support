"""History window with summarisation of older messages (CT-22, TASK-030).

`build_history` receives the messages that are not folded into `summary` yet (the service
passes the ones after `Conversation.summary_upto_message_id`) and returns what goes to the LLM:

- everything fits in `budget_tokens` (summary + messages) → sent as is, no summariser call;
- otherwise the most recent messages that fit in `budget_tokens - SUMMARY_RESERVE` stay intact
  and the older ones, together with the previous summary, are summarised (`summarizer`). The
  summary is trimmed so that `tokens` (summary block + recent messages) ≤ `budget_tokens`;
- `mode="full"` (`ATLAS_HISTORY_MODE=full`, the "before" of demo_02, DA-16) sends every message
  and never summarises, even above the budget.

The last message is always kept intact. Tokens are counted with `api.llm.tokens` over the
exact summary block the service sends (`summary_block`), so the count matches the request.
The budget holds whenever the last message alone fits in it (`ATLAS_MAX_MESSAGE_CHARS` keeps a
single message far below the default 8000 tokens).
"""

from dataclasses import dataclass
from typing import Protocol

from api.chat.models import Message
from api.llm.client import ChatMessage, LLMClient, LLMRequest, Role, SystemBlock
from api.llm.tokens import count_messages
from api.prompts import load_prompt

SUMMARY_HEADER = "Summary of the earlier part of this conversation:\n"
SUMMARY_MAX_TOKENS = 1000
"""Output cap of the default summariser and upper bound of the reserve kept for the summary."""

_ROLES: dict[str, Role] = {"user": "user", "assistant": "assistant"}
_SPEAKERS = {"user": "Customer", "assistant": "Assistant"}


class Summarizer(Protocol):
    async def __call__(self, previous_summary: str | None, messages: list[Message]) -> str: ...


@dataclass(frozen=True)
class HistoryWindow:
    summary: str | None
    messages: list[ChatMessage]
    tokens: int
    summarized_upto: int | None
    """Id of the last message folded into `summary`, or `None` when nothing was summarised."""


def summary_block(summary: str) -> SystemBlock:
    """The extra system block that carries the summary in the LLM request."""
    return SystemBlock(SUMMARY_HEADER + summary)


def _tokens(summary: str | None, messages: list[ChatMessage]) -> int:
    system = [summary_block(summary)] if summary else []
    return count_messages(system, messages)


def _chat_messages(messages: list[Message]) -> list[tuple[Message, ChatMessage]]:
    """User and assistant rows as LLM messages. Tool rows join the history from phase 06 on."""
    return [
        (message, ChatMessage(_ROLES[message.role], message.content))
        for message in messages
        if message.role in _ROLES
    ]


def _summary_tokens(summary: str | None) -> int:
    return count_messages([summary_block(summary)], []) if summary else 0


def _trim_summary(summary: str, room_tokens: int) -> str | None:
    """Longest prefix of `summary` whose block takes at most `room_tokens`."""
    summary = summary.strip()
    if not summary:
        return None
    if _summary_tokens(summary) <= room_tokens:
        return summary
    low, high = 0, len(summary)
    while low < high:
        middle = (low + high + 1) // 2
        if _summary_tokens(summary[:middle]) <= room_tokens:
            low = middle
        else:
            high = middle - 1
    trimmed = summary[:low].rstrip()
    return trimmed or None


async def build_history(
    messages: list[Message],
    summary: str | None,
    budget_tokens: int,
    summarizer: Summarizer,
    *,
    mode: str = "summarized",
) -> HistoryWindow:
    """`mode="full"` (`ATLAS_HISTORY_MODE=full`) sends everything and never summarises."""
    pairs = _chat_messages(messages)
    chat = [pair[1] for pair in pairs]

    if mode == "full":
        return HistoryWindow(None, chat, _tokens(None, chat), None)

    total = _tokens(summary, chat)
    if total <= budget_tokens:
        return HistoryWindow(summary, chat, total, None)

    # Token counts are additive per block and per message, so count each one once.
    sizes = [count_messages([], [message]) for message in chat]
    reserve = min(SUMMARY_MAX_TOKENS, budget_tokens // 4)
    keep = 1 if chat else 0
    recent_tokens = sum(sizes[len(sizes) - keep :])
    while keep < len(chat) and recent_tokens + sizes[-(keep + 1)] <= budget_tokens - reserve:
        keep += 1
        recent_tokens += sizes[-keep]
    recent = chat[len(chat) - keep :]
    older = [pair[0] for pair in pairs[: len(pairs) - keep]]
    room = budget_tokens - recent_tokens

    if not older:
        # Only the stored summary is too large (e.g. the budget was lowered): trim it.
        trimmed = _trim_summary(summary or "", room)
        return HistoryWindow(trimmed, recent, _tokens(trimmed, recent), None)

    new_summary = _trim_summary(await summarizer(summary, older), room)
    return HistoryWindow(new_summary, recent, _tokens(new_summary, recent), older[-1].id)


class LLMSummarizer:
    """Default `Summarizer`: one LLM call with `prompts/summarize.md`, `purpose="summarize"`."""

    def __init__(self, llm: LLMClient, max_tokens: int = SUMMARY_MAX_TOKENS) -> None:
        self.llm = llm
        self.max_tokens = max_tokens

    async def __call__(self, previous_summary: str | None, messages: list[Message]) -> str:
        request = LLMRequest(
            system=[SystemBlock(load_prompt("summarize"))],
            messages=[ChatMessage("user", _transcript(previous_summary, messages))],
            max_tokens=self.max_tokens,
            purpose="summarize",
        )
        response = await self.llm.complete(request)
        return response.text.strip()


def _transcript(previous_summary: str | None, messages: list[Message]) -> str:
    lines = [f"Previous summary:\n{previous_summary or '(none)'}", "", "Messages:"]
    for message in messages:
        speaker = _SPEAKERS.get(message.role, message.role)
        lines.append(f"{speaker}: {message.content}")
    return "\n".join(lines)
