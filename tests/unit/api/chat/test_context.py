"""History window with summarisation (TASK-030, CHAT-06, CHAT-08)."""

import uuid

import pytest

from api.chat.context import (
    LLMSummarizer,
    build_history,
    summary_block,
)
from api.chat.models import Message
from api.llm.client import LLMResponse, SystemBlock, Usage
from api.llm.fake import FakeLLMClient
from api.llm.tokens import count_messages, count_tokens
from api.prompts import load_prompt

CONVERSATION_ID = uuid.uuid4()


def _message(message_id: int, content: str, role: str = "user") -> Message:
    return Message(
        id=message_id,
        conversation_id=CONVERSATION_ID,
        role=role,
        content=content,
        status="complete",
        language="en",
        trace_id="t",
    )


def _long_text(index: int, target_tokens: int = 300) -> str:
    words = [f"topic{index} detail{n} about the subscription plan." for n in range(80)]
    text = " ".join(words)
    while count_tokens(text) > target_tokens:
        text = text.rsplit(" ", 1)[0]
    return text


def _conversation(count: int, target_tokens: int = 300) -> list[Message]:
    return [
        _message(i + 1, _long_text(i, target_tokens), "user" if i % 2 == 0 else "assistant")
        for i in range(count)
    ]


class RecordingSummarizer:
    def __init__(self, text: str = "The customer asked about plans and refunds.") -> None:
        self.text = text
        self.calls: list[tuple[str | None, list[int]]] = []

    async def __call__(self, previous_summary: str | None, messages: list[Message]) -> str:
        self.calls.append((previous_summary, [m.id for m in messages]))
        return self.text


async def test_long_history_fits_budget_and_keeps_last_message() -> None:
    messages = _conversation(60)
    assert 280 <= count_tokens(messages[-1].content) <= 300
    summarizer = RecordingSummarizer()

    window = await build_history(messages, None, 8000, summarizer)

    assert window.tokens <= 8000
    assert window.summary
    assert window.messages[-1].content == messages[-1].content
    assert window.messages[-1].role == messages[-1].role
    assert len(summarizer.calls) == 1
    folded = summarizer.calls[0][1]
    assert window.summarized_upto == folded[-1]
    kept = len(window.messages)
    assert folded == [m.id for m in messages[: 60 - kept]]
    assert [m.content for m in window.messages] == [m.content for m in messages[60 - kept :]]
    system = [summary_block(window.summary)]
    assert window.tokens == count_messages(system, window.messages)


async def test_previous_summary_is_folded_into_new_summary() -> None:
    summarizer = RecordingSummarizer()

    window = await build_history(_conversation(60), "Earlier: name is Rubens.", 8000, summarizer)

    assert summarizer.calls[0][0] == "Earlier: name is Rubens."
    assert window.tokens <= 8000


async def test_history_within_budget_does_not_call_summarizer() -> None:
    messages = _conversation(4)
    summarizer = RecordingSummarizer()

    window = await build_history(messages, "Old summary.", 8000, summarizer)

    assert summarizer.calls == []
    assert window.summary == "Old summary."
    assert window.summarized_upto is None
    assert [m.content for m in window.messages] == [m.content for m in messages]
    assert window.tokens == count_messages([summary_block("Old summary.")], window.messages)


async def test_full_mode_never_summarizes() -> None:
    messages = _conversation(60)
    summarizer = RecordingSummarizer()

    window = await build_history(messages, None, 8000, summarizer, mode="full")

    assert summarizer.calls == []
    assert window.summary is None
    assert window.summarized_upto is None
    assert len(window.messages) == 60
    assert window.tokens > 8000


async def test_oversized_summary_is_trimmed_to_budget() -> None:
    summarizer = RecordingSummarizer(text=" ".join(["very long summary"] * 5000))

    window = await build_history(_conversation(60), None, 8000, summarizer)

    assert window.tokens <= 8000
    assert window.summary


async def test_empty_history() -> None:
    window = await build_history([], None, 8000, RecordingSummarizer())

    assert window.messages == []
    assert window.summary is None
    assert window.tokens == 0


def _response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        usage=Usage(input_tokens=1, output_tokens=1),
        model="fake-model",
        provider="fake",
        stop_reason="end_turn",
    )


async def test_llm_summarizer_uses_summarize_prompt_and_purpose() -> None:
    fake = FakeLLMClient([_response("  Customer Rubens wants a refund.  ")])
    summarizer = LLMSummarizer(fake)

    text = await summarizer(
        "Earlier summary.",
        [_message(1, "My name is Rubens"), _message(2, "Hi Rubens", "assistant")],
    )

    assert text == "Customer Rubens wants a refund."
    request = fake.requests[0]
    assert request.purpose == "summarize"
    assert request.system == [SystemBlock(load_prompt("summarize"))]
    assert request.tools == []
    transcript = request.messages[0].content
    assert "Earlier summary." in transcript
    assert "My name is Rubens" in transcript
    assert "Hi Rubens" in transcript


def test_summarize_prompt_exists() -> None:
    assert "summar" in load_prompt("summarize").lower()


@pytest.mark.parametrize("mode", ["summarized", "full"])
async def test_tokens_match_window(mode: str) -> None:
    messages = _conversation(3)
    window = await build_history(messages, None, 8000, RecordingSummarizer(), mode=mode)

    assert window.tokens == count_messages([], window.messages)
