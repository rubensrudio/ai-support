"""History window with summarisation of older messages (CT-22). Skeleton: TASK-030."""

from dataclasses import dataclass
from typing import Protocol

from api.chat.models import Message
from api.llm.client import ChatMessage


class Summarizer(Protocol):
    async def __call__(self, previous_summary: str | None, messages: list[Message]) -> str: ...


@dataclass(frozen=True)
class HistoryWindow:
    summary: str | None
    messages: list[ChatMessage]
    tokens: int
    summarized_upto: int | None
    """Id of the last message folded into `summary`, or `None` when nothing was summarised."""


async def build_history(
    messages: list[Message],
    summary: str | None,
    budget_tokens: int,
    summarizer: Summarizer,
    *,
    mode: str = "summarized",
) -> HistoryWindow:
    """`mode="full"` (`ATLAS_HISTORY_MODE=full`) sends everything and never summarises."""
    raise NotImplementedError("TASK-030")
