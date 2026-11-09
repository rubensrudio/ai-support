"""SSE encoder and terminal guard (TASK-093, CT-50, STR-01, STR-05, STR-90)."""

import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from typing import cast

from api.agent.events import (
    AgentEvent,
    CitationEvent,
    DoneEvent,
    ErrorEvent,
    TokenEvent,
    ToolEndEvent,
    ToolStartEvent,
)
from api.streaming.events import encode_sse, guard_terminal

_CONVERSATION_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _done(content: str = "ok") -> DoneEvent:
    return DoneEvent(
        status="completed",
        message_id=7,
        conversation_id=_CONVERSATION_ID,
        trace_id="t-1",
        language="pt",
        content=content,
    )


def _error() -> ErrorEvent:
    return ErrorEvent(code="llm_unavailable", message="Falha temporária", trace_id="t-1")


class _Source:
    """Async iterator over fixed events that records how far it was consumed and if closed."""

    def __init__(self, events: list[AgentEvent]) -> None:
        self._events = events
        self.pulled = 0
        self.closed = False

    def __aiter__(self) -> "_Source":
        return self

    async def __anext__(self) -> AgentEvent:
        if self.closed or self.pulled >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self.pulled]
        self.pulled += 1
        return event

    async def aclose(self) -> None:
        self.closed = True


async def _collect(events: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    return [event async for event in guard_terminal(events)]


# --- encode_sse (STR-01) ----------------------------------------------------------------------


def test_encode_token_byte_by_byte_with_accents_and_quotes() -> None:
    encoded = encode_sse(TokenEvent(text='Olá, "mundo" ção'))

    assert encoded == (
        'event: token\ndata: {"type":"token","text":"Olá, \\"mundo\\" ção"}\n\n'.encode()
    )


def test_encode_done_byte_by_byte_with_accents_and_quotes() -> None:
    encoded = encode_sse(_done(content='Pedido "#42" está a caminho'))

    assert (
        encoded
        == (
            "event: done\n"
            'data: {"type":"done","status":"completed","message_id":7,'
            '"conversation_id":"00000000-0000-0000-0000-000000000001","trace_id":"t-1",'
            '"language":"pt","content":"Pedido \\"#42\\" está a caminho","confirmation":null}'
            "\n\n"
        ).encode()
    )


def test_encode_keeps_multiline_text_on_a_single_data_line() -> None:
    encoded = encode_sse(TokenEvent(text="linha 1\nlinha 2\r\n"))

    header, data, *rest = encoded.split(b"\n")
    assert header == b"event: token"
    assert data.startswith(b"data: ")
    assert rest == [b"", b""]
    assert encoded.endswith(b"\n\n")
    assert encoded.count(b"\n\n") == 1


def test_encode_uses_the_event_type_for_every_kind() -> None:
    events: list[AgentEvent] = [
        TokenEvent(text="a"),
        ToolStartEvent(call_id="c1", tool="get_order", label_key="tool.get_order"),
        ToolEndEvent(call_id="c1", tool="get_order", ok=True),
        CitationEvent(index=1, document="Política de trocas"),
        _done(),
        _error(),
    ]

    for event in events:
        assert encode_sse(event).startswith(f"event: {event.type}\ndata: ".encode())


def test_concatenated_events_form_one_block_each() -> None:
    events: list[AgentEvent] = [TokenEvent(text="a"), TokenEvent(text="b"), _done()]
    stream = b"".join(encode_sse(e) for e in events)

    blocks = stream.split(b"\n\n")
    assert blocks[-1] == b""
    assert [block.split(b"\n")[0] for block in blocks[:-1]] == [
        b"event: token",
        b"event: token",
        b"event: done",
    ]


# --- guard_terminal (STR-05, STR-90) ----------------------------------------------------------


async def test_guard_passes_through_a_well_formed_stream() -> None:
    events: list[AgentEvent] = [TokenEvent(text="a"), TokenEvent(text="b"), _done()]

    assert await _collect(_Source(events)) == events


async def test_guard_drops_events_after_done_and_emits_done_once() -> None:
    first_done = _done(content="first")
    source = _Source([TokenEvent(text="a"), first_done, TokenEvent(text="late"), _done("second")])

    result = await _collect(source)

    assert result == [TokenEvent(text="a"), first_done]
    assert [e.type for e in result].count("done") == 1


async def test_guard_drops_everything_after_error_and_never_emits_done() -> None:
    error = _error()
    source = _Source([TokenEvent(text="a"), error, TokenEvent(text="late"), _done()])

    result = await _collect(source)

    assert result == [TokenEvent(text="a"), error]
    assert all(e.type != "done" for e in result)


async def test_guard_stops_consuming_and_closes_the_source_after_a_terminal() -> None:
    source = _Source([TokenEvent(text="a"), _error(), TokenEvent(text="late"), _done()])

    await _collect(source)

    assert source.pulled == 2
    assert source.closed is True


async def test_guard_closes_the_source_after_done() -> None:
    source = _Source([_done(), TokenEvent(text="late")])

    await _collect(source)

    assert source.pulled == 1
    assert source.closed is True


async def test_guard_closes_an_async_generator_source() -> None:
    finalized: list[bool] = []

    async def gen() -> AsyncIterator[AgentEvent]:
        try:
            yield TokenEvent(text="a")
            yield _done()
            yield TokenEvent(text="late")
        finally:
            finalized.append(True)

    result = await _collect(gen())

    assert [e.type for e in result] == ["token", "done"]
    assert finalized == [True]


async def test_guard_does_not_invent_done_when_source_ends_without_terminal() -> None:
    source = _Source([TokenEvent(text="a"), TokenEvent(text="b")])

    result = await _collect(source)

    assert result == [TokenEvent(text="a"), TokenEvent(text="b")]


async def test_guard_on_empty_source_yields_nothing() -> None:
    assert await _collect(_Source([])) == []


async def test_guard_closes_the_source_when_the_consumer_closes_early() -> None:
    source = _Source([TokenEvent(text="a"), TokenEvent(text="b"), _done()])
    guarded = cast(AsyncGenerator[AgentEvent], guard_terminal(source))

    assert await anext(guarded) == TokenEvent(text="a")
    await guarded.aclose()

    assert source.pulled == 1
    assert source.closed is True
