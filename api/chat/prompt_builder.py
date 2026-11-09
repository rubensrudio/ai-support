"""LLM request builder with numbered knowledge-base chunks (CT-31). Skeleton: TASK-048."""

from typing import Literal

from api.chat.context import HistoryWindow
from api.llm.client import LLMRequest, ToolSpec
from api.messages import Lang
from api.rag.retriever import RetrievedChunk


def build_request(
    *,
    language: Lang,
    history: HistoryWindow,
    chunks: list[RetrievedChunk],
    user_message: str,
    tools: list[ToolSpec],
    layout: Literal["naive", "cache_ordered"],
) -> LLMRequest:
    raise NotImplementedError("TASK-048")
