"""Sentence-buffered streaming output filter (TASK-104, CT-75, DA-22, GRD-04, GRD-08)."""

import json
import uuid

from api.config import Settings
from api.guardrails.output_guard import (
    GUARD_FAILURE,
    OFF_TOPIC,
    OTHER_CUSTOMER_EMAIL,
    UNFULFILLED_PROMISE,
    OutputContext,
    OutputGuard,
)
from api.guardrails.stream_filter import FilterOutcome, StreamOutputFilter
from api.llm.client import LLMError, LLMResponse, Usage
from api.llm.fake import FakeLLMClient, split_deltas
from api.messages import Lang


def _verdict(on_topic: bool) -> LLMResponse:
    return LLMResponse(
        text=json.dumps({"on_topic": on_topic, "reason": "classifier reason"}),
        tool_calls=[],
        usage=Usage(input_tokens=10, output_tokens=5),
        model="fake-model",
        provider="fake",
        stop_reason="end_turn",
    )


def _filter(
    *script: LLMResponse | LLMError, language: Lang = "en", successful_tools: set[str] | None = None
) -> tuple[StreamOutputFilter, FakeLLMClient]:
    llm = FakeLLMClient(script=list(script))
    guard = OutputGuard(llm, Settings(_env_file=None))  # type: ignore[call-arg]
    ctx = OutputContext(
        customer_id=uuid.uuid4(),
        own_order_ids={1042},
        own_email="ana.souza@example.com",
        successful_tools=set() if successful_tools is None else successful_tools,
        question="Where is my order?",
        language=language,
    )
    return StreamOutputFilter(guard, ctx), llm


async def _feed_all(stream: StreamOutputFilter, deltas: list[str]) -> list[list[str]]:
    return [await stream.feed(delta) for delta in deltas]


def _chars(text: str) -> list[str]:
    return list(text)


# --- release order and trailing text ----------------------------------------------------------


async def test_approved_sentences_are_released_in_order_and_finish_releases_the_rest() -> None:
    stream, llm = _filter(_verdict(True))
    text = "Your order #1042 shipped today. It should arrive soon! Anything else I can do"

    outputs = await _feed_all(stream, split_deltas(text))
    outcome = await stream.finish()

    released = [sentence for batch in outputs for sentence in batch]
    assert released == ["Your order #1042 shipped today. ", "It should arrive soon! "]
    assert outcome == FilterOutcome(blocked=False, released="Anything else I can do")
    assert "".join(released) + outcome.released == text
    assert len(llm.requests) == 1


async def test_sentence_is_held_until_its_end_is_seen() -> None:
    stream, _ = _filter(_verdict(True))

    assert await stream.feed("Your order #1042 shipped") == []
    assert await stream.feed(" today and arrives Friday.") == []
    assert await stream.feed(" It") == ["Your order #1042 shipped today and arrives Friday. "]


async def test_topic_is_checked_once_with_a_long_first_sentence() -> None:
    stream, llm = _filter(_verdict(True))
    text = "Your order #1042 left our warehouse this morning. It arrives Friday. Thanks. "

    outputs = await _feed_all(stream, split_deltas(text))
    await stream.finish()

    [request] = llm.requests
    content = request.messages[0].content
    assert "Your order #1042 left our warehouse this morning." in content
    assert "Where is my order?" in content
    assert "It arrives Friday" not in content
    released = [sentence for batch in outputs for sentence in batch]
    assert released[0] == "Your order #1042 left our warehouse this morning. "


async def test_short_opener_is_held_and_topic_checked_with_the_second_sentence() -> None:
    stream, llm = _filter(_verdict(True))

    first = await stream.feed("Sure! ")
    second = await stream.feed("Your order shipped. More")

    assert first == []
    assert second == ["Sure! ", "Your order shipped. "]
    [request] = llm.requests
    assert "Sure! Your order shipped." in request.messages[0].content


async def test_courtesy_opener_before_off_topic_text_is_never_released() -> None:
    stream, llm = _filter(_verdict(False))

    outputs = await _feed_all(stream, split_deltas("Claro! Aqui vai um poema sobre gatos. "))
    outcome = await stream.finish()

    assert outputs == [[] for _ in outputs]
    assert outcome == FilterOutcome(blocked=True, released="", reason=OFF_TOPIC)
    assert "Aqui vai um poema sobre gatos." in llm.requests[0].messages[0].content


async def test_single_short_answer_is_topic_checked_on_finish() -> None:
    stream, llm = _filter(_verdict(True))

    assert await stream.feed("Sure! ") == []
    outcome = await stream.finish()

    assert outcome == FilterOutcome(blocked=False, released="Sure! ")
    assert len(llm.requests) == 1


async def test_double_newline_ends_a_sentence() -> None:
    stream, _ = _filter(_verdict(True))

    released = await stream.feed("Here is everything I found about your orders:\n\n- Order #1042")

    assert released == ["Here is everything I found about your orders:\n\n"]
    assert await stream.finish() == FilterOutcome(blocked=False, released="- Order #1042")


# --- blocking -----------------------------------------------------------------------------------


async def test_no_fragment_of_a_blocked_sentence_is_released() -> None:
    stream, _ = _filter(_verdict(True))
    text = (
        "Your order #1042 shipped today and arrives Friday. "
        "Bruno at bruno.lima@example.com also asked. "
        "Your refund policy is 30 days. More text"
    )

    outputs = await _feed_all(stream, _chars(text))
    outcome = await stream.finish()

    released = "".join(sentence for batch in outputs for sentence in batch)
    assert released == "Your order #1042 shipped today and arrives Friday. "
    assert "Bruno" not in released
    assert "bruno.lima" not in released
    assert outcome == FilterOutcome(blocked=True, released="", reason=OTHER_CUSTOMER_EMAIL)


async def test_feed_returns_nothing_after_the_first_block() -> None:
    stream, _ = _filter(_verdict(True))

    opening = "Your order #1042 is still pending payment today. "
    first = await stream.feed(opening + "I have cancelled your order. ")
    after = await stream.feed("Your order shipped today. And more. ")

    assert first == [opening]
    assert after == []
    assert await stream.finish() == FilterOutcome(
        blocked=True, released="", reason=UNFULFILLED_PROMISE
    )


async def test_promise_with_a_successful_tool_is_released() -> None:
    stream, _ = _filter(_verdict(True), successful_tools={"cancel_order"})

    released = await stream.feed("I have cancelled your order. It will not be charged. ")

    assert released == ["I have cancelled your order. ", "It will not be charged. "]


async def test_trailing_text_without_punctuation_is_checked_on_finish() -> None:
    stream, _ = _filter(_verdict(True))

    opening = "Your order #1042 is still pending payment today. "
    released = await stream.feed(opening + "I have cancelled your order")
    outcome = await stream.finish()

    assert released == [opening]
    assert outcome == FilterOutcome(blocked=True, released="", reason=UNFULFILLED_PROMISE)


async def test_off_topic_first_sentence_blocks_everything() -> None:
    stream, _ = _filter(_verdict(False))

    outputs = await _feed_all(stream, split_deltas("Roses are red. Violets are blue. "))

    assert outputs == [[] for _ in outputs]
    assert await stream.finish() == FilterOutcome(blocked=True, released="", reason=OFF_TOPIC)


async def test_classifier_failure_blocks_fail_closed() -> None:
    stream, _ = _filter(LLMError("down", recoverable=True, status=503))

    assert await stream.feed("Your order shipped today. ") == []
    assert await stream.finish() == FilterOutcome(blocked=True, released="", reason=GUARD_FAILURE)


async def test_answer_without_any_sentence_end_is_topic_checked_on_finish() -> None:
    stream, llm = _filter(_verdict(False))

    assert await stream.feed("Let me tell you a joke") == []
    outcome = await stream.finish()

    assert outcome == FilterOutcome(blocked=True, released="", reason=OFF_TOPIC)
    assert len(llm.requests) == 1


async def test_empty_answer_is_not_blocked_and_calls_no_classifier() -> None:
    stream, llm = _filter()

    assert await stream.feed("") == []
    assert await stream.finish() == FilterOutcome(blocked=False, released="")
    assert llm.requests == []


# --- sentence boundaries ------------------------------------------------------------------------


async def test_dots_inside_amounts_abbreviations_and_lists_do_not_split() -> None:
    stream, _ = _filter(_verdict(True))
    text = (
        "Your plan costs $12.50 per month, e.g. the U.S. price. "
        "Steps:\n1. Open the app.\n2. Tap Orders. "
        "Done"
    )

    outputs = await _feed_all(stream, _chars(text))
    outcome = await stream.finish()

    released = [sentence for batch in outputs for sentence in batch]
    assert released == [
        "Your plan costs $12.50 per month, e.g. the U.S. price. ",
        "Steps:\n1. Open the app.\n",
        "2. Tap Orders. ",
    ]
    assert outcome.released == "Done"


async def test_portuguese_sentences_and_ellipsis() -> None:
    stream, _ = _filter(_verdict(True), language="pt")
    text = "Seu pedido #1042 saiu hoje… Chega em 3 dias úteis, p.ex. sexta. Algo mais?\nObrigado"

    outputs = await _feed_all(stream, split_deltas(text))
    outcome = await stream.finish()

    released = [sentence for batch in outputs for sentence in batch]
    assert released == [
        "Seu pedido #1042 saiu hoje… ",
        "Chega em 3 dias úteis, p.ex. sexta. ",
        "Algo mais?\n",
    ]
    assert outcome == FilterOutcome(blocked=False, released="Obrigado")


async def test_portuguese_refund_promise_is_blocked() -> None:
    stream, _ = _filter(_verdict(True), language="pt")

    released = await stream.feed("Tudo certo. Seu reembolso foi emitido hoje. Mais algo? ")

    assert released == []
    assert (await stream.finish()).reason == UNFULFILLED_PROMISE
