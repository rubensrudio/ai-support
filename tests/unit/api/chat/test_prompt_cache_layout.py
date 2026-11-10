"""Cache-ordered prompt layout with a stable prefix (CACHE-01, CACHE-02, DA-23), TASK-110.

The provider reads a request as tools -> system blocks -> messages. In `cache_ordered` the
tools and the system blocks up to the one marked `cache_breakpoint` form the stable prefix: it
must be byte-identical across conversations and reply languages, with no date, time, language
or customer data. The reply language and the history summary come right after it, in a
variable system block, followed by the history and the last user turn (documents + question).
`naive` is the "before" of demo 11: the current date/time and the language at the top of the
system, no breakpoint, so the prefix changes on every call.
"""

import json
from dataclasses import asdict
from datetime import UTC, datetime

import pytest

from api.chat.context import HistoryWindow, summary_block
from api.chat.prompt_builder import (
    LANGUAGE_NAMES,
    UNTRUSTED_DOCUMENTS_RULE,
    build_base_request,
    build_request,
    grounded_system_prompt,
    language_instruction,
)
from api.llm.client import ChatMessage, LLMRequest, ToolSpec
from api.messages import Lang
from api.prompts import load_prompt
from api.rag.retriever import RetrievedChunk

TOOLS = [
    ToolSpec(
        name="get_order",
        description="Look up one order of the customer.",
        input_schema={"type": "object", "properties": {"order_id": {"type": "integer"}}},
    ),
    ToolSpec(name="list_orders", description="List the customer's orders.", input_schema={}),
]
EMPTY = HistoryWindow(summary=None, messages=[], tokens=0, summarized_upto=None)
WITH_SUMMARY = HistoryWindow(
    summary="The customer is called Ana and has order #1234.",
    messages=[ChatMessage("user", "Oi"), ChatMessage("assistant", "Olá, Ana.")],
    tokens=42,
    summarized_upto=7,
)
FIXED_NOW = datetime(2026, 11, 9, 12, 30, tzinfo=UTC)


def _chunk(n: int) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=300 + n,
        document_title=f"Document {n}",
        doc_key=f"doc-{n}",
        section="Billing",
        page=None,
        language="pt",
        content=f"Content of chunk number {n}.",
        score=1.0 / n,
    )


def _build(
    *,
    language: Lang = "en",
    history: HistoryWindow = EMPTY,
    chunks: list[RetrievedChunk] | None = None,
    user_message: str = "How do I cancel my plan?",
    layout: str = "cache_ordered",
    now: datetime = FIXED_NOW,
) -> LLMRequest:
    return build_request(
        language=language,
        history=history,
        chunks=[] if chunks is None else chunks,
        user_message=user_message,
        tools=TOOLS,
        layout=layout,  # type: ignore[arg-type]
        clock=lambda: now,
    )


def _stable_prefix(request: LLMRequest) -> bytes:
    """Tools and the system blocks up to the last `cache_breakpoint`, serialized."""
    marks = [n for n, block in enumerate(request.system) if block.cache_breakpoint]
    assert marks, "cache_ordered must mark the end of the stable prefix"
    blocks = request.system[: marks[-1] + 1]
    payload = {
        "tools": [asdict(tool) for tool in request.tools],
        "system": [asdict(block) for block in blocks],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


# --- cache_ordered --------------------------------------------------------------------------


def test_stable_prefix_is_byte_identical_across_conversations_and_languages() -> None:
    pt = _build(
        language="pt",
        history=WITH_SUMMARY,
        chunks=[_chunk(1), _chunk(2)],
        user_message="Como cancelo meu plano?",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    en = _build(language="en", history=EMPTY, chunks=[])

    assert _stable_prefix(pt) == _stable_prefix(en)
    assert pt.tools == TOOLS
    assert en.tools == TOOLS


@pytest.mark.parametrize("language", ["en", "pt"])
@pytest.mark.parametrize("history", [EMPTY, WITH_SUMMARY])
def test_breakpoint_is_only_on_the_last_stable_block(
    language: Lang, history: HistoryWindow
) -> None:
    request = _build(language=language, history=history, chunks=[_chunk(1)])

    assert [block.cache_breakpoint for block in request.system] == [True, False]
    assert request.system[0].text == grounded_system_prompt()
    assert UNTRUSTED_DOCUMENTS_RULE in request.system[0].text


@pytest.mark.parametrize(("language", "other"), [("en", "pt"), ("pt", "en")])
def test_reply_language_is_outside_the_stable_prefix(language: Lang, other: Lang) -> None:
    request = _build(language=language, history=WITH_SUMMARY, chunks=[_chunk(1)])
    prefix = _stable_prefix(request).decode("utf-8")

    for name in LANGUAGE_NAMES.values():
        assert name not in prefix
    assert "{language}" not in prefix
    variable = request.system[1].text
    assert variable.startswith(language_instruction(language))
    assert f"Always reply in {LANGUAGE_NAMES[language]}," in variable
    assert LANGUAGE_NAMES[other] not in variable


def test_summary_goes_in_the_variable_block_after_the_language() -> None:
    request = _build(history=WITH_SUMMARY, chunks=[_chunk(1)])

    summary = summary_block("The customer is called Ana and has order #1234.").text
    assert request.system[1].text == f"{language_instruction('en')}\n\n{summary}"
    assert "Ana" not in _stable_prefix(request).decode("utf-8")
    assert [(m.role, m.content) for m in request.messages[:-1]] == [
        ("user", "Oi"),
        ("assistant", "Olá, Ana."),
    ]


def test_without_summary_the_variable_block_is_only_the_language() -> None:
    request = _build()

    assert request.system[1].text == language_instruction("en")


def test_documents_and_question_come_after_the_history_in_the_last_user_turn() -> None:
    request = _build(history=WITH_SUMMARY, chunks=[_chunk(1), _chunk(2)])

    last = request.messages[-1]
    assert last.role == "user"
    assert last.content.index('<untrusted_document index="1"') < last.content.index(
        "<customer_question>"
    )
    for block in request.system:
        assert "<untrusted_document index=" not in block.text


def test_stable_prefix_has_no_date_or_time() -> None:
    prefix = _stable_prefix(_build()).decode("utf-8")

    assert FIXED_NOW.isoformat() not in prefix
    assert "2026" not in prefix


def test_language_instruction_names_the_reply_language() -> None:
    assert "Always reply in English," in language_instruction("en")
    assert "Always reply in Brazilian Portuguese," in language_instruction("pt")
    assert "{language}" not in language_instruction("pt")


def test_grounded_system_prompt_keeps_the_fixed_rules() -> None:
    prompt = grounded_system_prompt()

    assert "{language}" not in prompt
    assert "Always reply in" not in prompt
    assert "[n]" in prompt
    assert "Orders and actions:" in prompt
    assert "don't know" in prompt
    assert prompt.endswith(f"{UNTRUSTED_DOCUMENTS_RULE}\n")


def test_system_file_has_no_language_section() -> None:
    prompt = load_prompt("system")

    assert "{" not in prompt
    assert "Always reply in" not in prompt
    assert grounded_system_prompt() == f"{prompt.strip()}\n\n{UNTRUSTED_DOCUMENTS_RULE}\n"


# --- request without retrieval (phase-02 path of the chat service) --------------------------


def _base(
    *,
    language: Lang = "en",
    history: HistoryWindow = EMPTY,
    layout: str = "cache_ordered",
    now: datetime = FIXED_NOW,
) -> LLMRequest:
    return build_base_request(
        language=language,
        history=history,
        tools=TOOLS,
        layout=layout,  # type: ignore[arg-type]
        clock=lambda: now,
    )


def test_base_request_has_the_same_stable_prefix_as_the_rag_request() -> None:
    base = _base(language="pt", history=WITH_SUMMARY)
    rag = _build(language="en", chunks=[_chunk(1)])

    assert _stable_prefix(base) == _stable_prefix(rag)
    assert [block.cache_breakpoint for block in base.system] == [True, False]


def test_base_request_puts_language_and_summary_after_the_prefix() -> None:
    request = _base(language="pt", history=WITH_SUMMARY)

    summary = summary_block("The customer is called Ana and has order #1234.").text
    assert request.system[1].text == f"{language_instruction('pt')}\n\n{summary}"
    assert request.messages == WITH_SUMMARY.messages
    assert request.tools == TOOLS
    assert request.purpose == "chat"
    assert _base().system[1].text == language_instruction("en")


def test_base_request_naive_puts_date_time_and_language_on_top() -> None:
    first = _base(layout="naive", history=WITH_SUMMARY)
    second = _base(layout="naive", history=WITH_SUMMARY, now=datetime(2026, 11, 9, tzinfo=UTC))

    top = first.system[0].text
    assert top.startswith(f"Current date and time (UTC): {FIXED_NOW.isoformat()}\n")
    assert top.endswith(f"{language_instruction('en')}\n\n{grounded_system_prompt()}")
    assert first.system[1] == summary_block(WITH_SUMMARY.summary or "")
    assert not any(block.cache_breakpoint for block in first.system)
    assert first.system[0].text != second.system[0].text


# --- naive ----------------------------------------------------------------------------------


def test_naive_puts_date_time_and_language_on_top_without_breakpoint() -> None:
    request = _build(layout="naive", language="pt", history=WITH_SUMMARY, chunks=[_chunk(1)])

    top = request.system[0].text
    assert top.startswith(f"Current date and time (UTC): {FIXED_NOW.isoformat()}\n")
    assert top.index(FIXED_NOW.isoformat()) < top.index("Always reply in Brazilian Portuguese,")
    assert top.index("Always reply in") < top.index(grounded_system_prompt())
    assert not any(block.cache_breakpoint for block in request.system)
    assert request.system[1].text == summary_block(WITH_SUMMARY.summary or "").text


def test_naive_differs_between_two_calls_at_different_instants() -> None:
    first = _build(layout="naive", now=datetime(2026, 11, 9, 12, 0, tzinfo=UTC))
    second = _build(layout="naive", now=datetime(2026, 11, 9, 12, 0, 1, tzinfo=UTC))

    assert first.system[0].text != second.system[0].text
    assert first.messages == second.messages


def test_naive_uses_the_current_utc_time_by_default() -> None:
    before = datetime.now(UTC)
    request = build_request(
        language="en",
        history=EMPTY,
        chunks=[],
        user_message="Hi",
        tools=[],
        layout="naive",
    )
    after = datetime.now(UTC)

    stamp = request.system[0].text.splitlines()[0].removeprefix("Current date and time (UTC): ")
    assert before <= datetime.fromisoformat(stamp) <= after
