"""Ledger of the estimated cost of every LLM call (TASK-142, CT-67, DEMO-03, AS-2).

`record_llm_call` commits in its own session, outside the `db_session` transaction, so every
test binds a unique trace id, reads its rows by that id and deletes them at the end.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from api.config import get_settings
from api.db.engine import get_sessionmaker
from api.limits.ledger import (
    COST_QUANTUM,
    COST_UNKNOWN_EVENT,
    LEDGER_FAILED_EVENT,
    record_llm_call,
)
from api.limits.models import LlmCall
from api.llm.client import (
    ChatMessage,
    LLMClient,
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    SystemBlock,
    Usage,
)
from api.llm.fake import FAKE_PROVIDER, FakeLLMClient
from api.llm.instrumented import InstrumentedLLMClient
from api.llm.pricing import estimate_cost, load_pricing_table
from api.llm.tokens import count_messages
from api.observability.logging import LOGGER_NAME, bind_trace_id, reset_trace_id

pytestmark = pytest.mark.integration

PRICED_MODEL = "ledger-priced-model"
UNPRICED_MODEL = "ledger-unpriced-model"
USAGE = Usage(input_tokens=1200, output_tokens=345, cache_read_tokens=60, cache_write_tokens=7)


def _stored(cost: Decimal) -> Decimal:
    """`estimate_cost` rounded up to the 6 decimal places of `llm_calls.cost_usd`."""
    return cost.quantize(COST_QUANTUM, rounding=ROUND_CEILING)


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def records() -> Iterator[list[logging.LogRecord]]:
    logger = logging.getLogger(LOGGER_NAME)
    handler = _ListHandler()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield handler.records
    logger.removeHandler(handler)
    logger.setLevel(previous_level)


@pytest.fixture
def ledger_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, migrated_database: str
) -> Iterator[None]:
    """Ledger on and a pricing file with one priced model (the default one prices `fake-model`
    at 0, which could not tell a real cost from a silent zero)."""
    pricing = tmp_path / "pricing.json"
    pricing.write_text(
        json.dumps(
            {
                PRICED_MODEL: {
                    "input": 3,
                    "output": 15,
                    "cache_read": 0.3,
                    "cache_write": 3.75,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ATLAS_PRICING_FILE", str(pricing))
    monkeypatch.setenv("ATLAS_LLM_LEDGER_ENABLED", "true")
    get_settings.cache_clear()
    load_pricing_table.cache_clear()
    yield
    get_settings.cache_clear()
    load_pricing_table.cache_clear()


@pytest.fixture
async def trace_id(migrated_database: str) -> AsyncIterator[str]:
    """A unique trace id bound for the test; its ledger rows are deleted afterwards."""
    value = f"ledger-test-{uuid.uuid4().hex}"
    token = bind_trace_id(value)
    try:
        yield value
    finally:
        reset_trace_id(token)
        async with get_sessionmaker()() as session:
            await session.execute(delete(LlmCall).where(LlmCall.trace_id == value))
            await session.commit()


async def _rows(trace_id: str) -> list[LlmCall]:
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(LlmCall).where(LlmCall.trace_id == trace_id).order_by(LlmCall.id)
        )
        return list(result.scalars().all())


def _events(records: list[logging.LogRecord], name: str) -> list[logging.LogRecord]:
    return [r for r in records if getattr(r, "event", None) == name]


def _request() -> LLMRequest:
    return LLMRequest(
        system=[SystemBlock("You are a support agent.")],
        messages=[ChatMessage(role="user", content="Where is my order?")],
        purpose="chat",
    )


def _response(model: str) -> LLMResponse:
    return LLMResponse(
        text="It shipped yesterday.",
        tool_calls=[],
        usage=USAGE,
        model=model,
        provider=FAKE_PROVIDER,
        stop_reason="end_turn",
    )


def _client(model: str, *script: LLMResponse | LLMError) -> InstrumentedLLMClient:
    return InstrumentedLLMClient(
        FakeLLMClient(script=list(script)), model=model, provider=FAKE_PROVIDER
    )


async def _drain(client: InstrumentedLLMClient) -> list[LLMStreamEvent]:
    return [event async for event in client.stream(_request())]


@pytest.mark.usefixtures("ledger_on")
async def test_complete_records_one_row_with_the_estimated_cost(trace_id: str) -> None:
    exact = estimate_cost(PRICED_MODEL, USAGE)
    expected = _stored(exact)
    # The usage is picked so the exact cost has more than 6 decimals and must be rounded up.
    assert Decimal(0) < exact < expected
    client = _client(PRICED_MODEL, _response(PRICED_MODEL), _response(PRICED_MODEL))

    first = await client.complete(_request())
    await client.complete(_request())

    assert first.text == "It shipped yesterday."
    rows = await _rows(trace_id)
    assert len(rows) == 2
    for row in rows:
        assert row.provider == FAKE_PROVIDER
        assert row.model == PRICED_MODEL
        assert row.cost_usd == expected
        assert row.created_at.tzinfo is not None


@pytest.mark.usefixtures("ledger_on")
async def test_finished_stream_records_one_row_with_the_estimated_cost(trace_id: str) -> None:
    client = _client(PRICED_MODEL, _response(PRICED_MODEL))

    events = await _drain(client)

    assert "".join(e.text or "" for e in events if e.kind == "text_delta") == (
        "It shipped yesterday."
    )
    rows = await _rows(trace_id)
    assert len(rows) == 1
    assert rows[0].cost_usd == _stored(estimate_cost(PRICED_MODEL, USAGE))
    assert rows[0].model == PRICED_MODEL


@pytest.mark.usefixtures("ledger_on")
async def test_unknown_price_records_zero_and_warns(
    trace_id: str, records: list[logging.LogRecord]
) -> None:
    client = _client(UNPRICED_MODEL, _response(UNPRICED_MODEL), _response(UNPRICED_MODEL))

    await client.complete(_request())
    await _drain(client)

    rows = await _rows(trace_id)
    assert [row.cost_usd for row in rows] == [Decimal(0), Decimal(0)]
    warnings = _events(records, COST_UNKNOWN_EVENT)
    assert len(warnings) == 2
    assert all(w.levelno == logging.WARNING for w in warnings)
    assert getattr(warnings[0], "provider") == FAKE_PROVIDER  # noqa: B009
    assert getattr(warnings[0], "model") == UNPRICED_MODEL  # noqa: B009


@pytest.mark.usefixtures("ledger_on")
async def test_subscription_provider_records_zero(trace_id: str) -> None:
    client = InstrumentedLLMClient(
        FakeLLMClient(script=[_response(PRICED_MODEL)]), model=PRICED_MODEL, provider="claude_cli"
    )

    await client.complete(_request())

    rows = await _rows(trace_id)
    assert [(row.provider, row.cost_usd) for row in rows] == [("claude_cli", Decimal(0))]


def _input_estimate(model: str) -> Decimal:
    """The conservative cost of a call billed without usage: the request's locally counted
    input tokens at the model's input price, rounded up like every ledger row."""
    request = _request()
    tokens = count_messages(request.system, request.messages)
    return _stored(estimate_cost(model, Usage(input_tokens=tokens, output_tokens=0)))


@pytest.mark.usefixtures("ledger_on")
async def test_failure_after_sending_records_the_input_estimate(trace_id: str) -> None:
    expected = _input_estimate(PRICED_MODEL)
    assert expected > 0
    client = _client(
        PRICED_MODEL,
        LLMError("overloaded", recoverable=True, status=529),
        LLMError("connection reset", recoverable=True),
    )

    with pytest.raises(LLMError):
        await client.complete(_request())
    with pytest.raises(LLMError):
        await _drain(client)

    assert [row.cost_usd for row in await _rows(trace_id)] == [expected, expected]


@pytest.mark.usefixtures("ledger_on")
async def test_rejected_call_records_nothing(trace_id: str) -> None:
    client = _client(
        PRICED_MODEL,
        LLMError("HTTP 400", recoverable=False, status=400),
        LLMError("HTTP 400", recoverable=False, status=400),
    )

    with pytest.raises(LLMError):
        await client.complete(_request())
    with pytest.raises(LLMError):
        await _drain(client)

    assert await _rows(trace_id) == []


class _FailsBeforeSending:
    """Local request validation fails before anything reaches the provider."""

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise ValueError("tool schema is not valid JSON")

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[LLMStreamEvent]:
        raise ValueError("tool schema is not valid JSON")
        yield LLMStreamEvent(kind="stop")  # pragma: no cover - makes this an async generator


@pytest.mark.usefixtures("ledger_on")
async def test_local_failure_before_sending_records_nothing(trace_id: str) -> None:
    inner: LLMClient = _FailsBeforeSending()
    client = InstrumentedLLMClient(inner, model=PRICED_MODEL, provider=FAKE_PROVIDER)

    with pytest.raises(ValueError):
        await client.complete(_request())
    with pytest.raises(ValueError):
        await _drain(client)

    assert await _rows(trace_id) == []


class _Hangs:
    """A provider call in flight that never answers until cancelled."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[LLMStreamEvent]:
        # Text first, `usage` only at the end (as Anthropic and OpenAI send it).
        yield LLMStreamEvent(kind="text_delta", text="It ")
        self.started.set()
        await asyncio.Event().wait()
        yield LLMStreamEvent(kind="usage", usage=USAGE)  # pragma: no cover


@pytest.mark.usefixtures("ledger_on")
async def test_cancelled_complete_records_the_input_estimate(trace_id: str) -> None:
    inner = _Hangs()
    client = InstrumentedLLMClient(inner, model=PRICED_MODEL, provider=FAKE_PROVIDER)

    task = asyncio.create_task(client.complete(_request()))
    await inner.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [row.cost_usd for row in await _rows(trace_id)] == [_input_estimate(PRICED_MODEL)]


@pytest.mark.usefixtures("ledger_on")
async def test_stream_stopped_before_usage_records_the_input_estimate(trace_id: str) -> None:
    client = _client(PRICED_MODEL, _response(PRICED_MODEL))

    stream = client.stream(_request())
    async for event in stream:
        assert event.kind == "text_delta"
        break
    await stream.aclose()  # type: ignore[attr-defined]

    assert [row.cost_usd for row in await _rows(trace_id)] == [_input_estimate(PRICED_MODEL)]


@pytest.mark.usefixtures("ledger_on")
async def test_cancelled_stream_before_usage_records_the_input_estimate(trace_id: str) -> None:
    inner = _Hangs()
    client = InstrumentedLLMClient(inner, model=PRICED_MODEL, provider=FAKE_PROVIDER)

    task = asyncio.create_task(_drain(client))
    await inner.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [row.cost_usd for row in await _rows(trace_id)] == [_input_estimate(PRICED_MODEL)]


class _NoUsage:
    """A stream that ends normally without a `usage` event."""

    async def complete(self, request: LLMRequest) -> LLMResponse:  # pragma: no cover
        raise NotImplementedError

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[LLMStreamEvent]:
        yield LLMStreamEvent(kind="text_delta", text="It shipped.")
        yield LLMStreamEvent(kind="stop", stop_reason="end_turn")


@pytest.mark.usefixtures("ledger_on")
async def test_finished_stream_without_usage_records_the_input_estimate(trace_id: str) -> None:
    inner: LLMClient = _NoUsage()
    client = InstrumentedLLMClient(inner, model=PRICED_MODEL, provider=FAKE_PROVIDER)

    events = await _drain(client)

    assert [e.kind for e in events] == ["text_delta", "stop"]
    assert [row.cost_usd for row in await _rows(trace_id)] == [_input_estimate(PRICED_MODEL)]


@pytest.mark.usefixtures("ledger_on")
async def test_no_usage_and_unknown_price_records_zero_and_warns(
    trace_id: str, records: list[logging.LogRecord]
) -> None:
    client = _client(UNPRICED_MODEL, LLMError("overloaded", recoverable=True, status=529))

    with pytest.raises(LLMError):
        await client.complete(_request())

    assert [row.cost_usd for row in await _rows(trace_id)] == [Decimal(0)]
    warnings = _events(records, COST_UNKNOWN_EVENT)
    assert [(getattr(w, "provider"), getattr(w, "model")) for w in warnings] == [  # noqa: B009
        (FAKE_PROVIDER, UNPRICED_MODEL)
    ]


class _FailsAfterUsage:
    """A stream that reports usage and then breaks: the provider already billed the tokens."""

    async def complete(self, request: LLMRequest) -> LLMResponse:  # pragma: no cover
        raise NotImplementedError

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[LLMStreamEvent]:
        yield LLMStreamEvent(kind="text_delta", text="It ")
        yield LLMStreamEvent(kind="usage", usage=USAGE)
        raise LLMError("connection reset", recoverable=True)


@pytest.mark.usefixtures("ledger_on")
async def test_stream_failing_after_usage_records_the_billed_cost(trace_id: str) -> None:
    inner: LLMClient = _FailsAfterUsage()
    client = InstrumentedLLMClient(inner, model=PRICED_MODEL, provider=FAKE_PROVIDER)

    with pytest.raises(LLMError):
        await _drain(client)

    rows = await _rows(trace_id)
    assert [row.cost_usd for row in rows] == [_stored(estimate_cost(PRICED_MODEL, USAGE))]


@pytest.mark.usefixtures("ledger_on")
async def test_stream_stopped_early_after_usage_records_the_cost(trace_id: str) -> None:
    client = _client(PRICED_MODEL, _response(PRICED_MODEL))

    stream = client.stream(_request())
    async for event in stream:
        if event.kind == "usage":
            break
    await stream.aclose()  # type: ignore[attr-defined]

    rows = await _rows(trace_id)
    assert [row.cost_usd for row in rows] == [_stored(estimate_cost(PRICED_MODEL, USAGE))]


@pytest.mark.usefixtures("ledger_on")
async def test_database_down_keeps_the_answer_and_logs_the_error_class(
    trace_id: str, records: list[logging.LogRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_sessionmaker() -> object:
        raise ConnectionRefusedError("db host 10.0.0.1 refused secret-value")

    client = _client(PRICED_MODEL, _response(PRICED_MODEL), _response(PRICED_MODEL))
    with monkeypatch.context() as patch:
        patch.setattr("api.limits.ledger.get_sessionmaker", broken_sessionmaker)
        response = await client.complete(_request())
        events = await _drain(client)

    assert response.text == "It shipped yesterday."
    assert [e.kind for e in events][-2:] == ["usage", "stop"]
    failures = _events(records, LEDGER_FAILED_EVENT)
    assert len(failures) == 2
    for failure in failures:
        assert failure.levelno == logging.ERROR
        assert getattr(failure, "error_type") == "ConnectionRefusedError"  # noqa: B009
        dumped = json.dumps({k: str(v) for k, v in vars(failure).items()})
        assert "secret-value" not in dumped
        assert PRICED_MODEL not in dumped
        assert str(estimate_cost(PRICED_MODEL, USAGE)) not in dumped
    assert await _rows(trace_id) == []


async def test_ledger_off_records_nothing(trace_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_LLM_LEDGER_ENABLED", "false")
    get_settings.cache_clear()
    try:
        client = _client("fake-model", _response("fake-model"), _response("fake-model"))
        await client.complete(_request())
        await _drain(client)
    finally:
        get_settings.cache_clear()

    assert await _rows(trace_id) == []


@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity"), Decimal("-0.01")])
async def test_bad_cost_value_only_logs_the_error_class(
    trace_id: str, records: list[logging.LogRecord], bad: Decimal
) -> None:
    await record_llm_call(
        trace_id=trace_id,
        provider=FAKE_PROVIDER,
        model=PRICED_MODEL,
        cost_usd=bad,
        created_at=datetime.now(UTC),
    )

    failures = _events(records, LEDGER_FAILED_EVENT)
    assert [getattr(f, "error_type") for f in failures] == ["ValueError"]  # noqa: B009
    assert await _rows(trace_id) == []
