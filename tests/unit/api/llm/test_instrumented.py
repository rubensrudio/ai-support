import asyncio
import gc
import logging
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr

from api.config import MissingConfigError, Settings
from api.llm.client import (
    ChatMessage,
    LLMClient,
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    LLMTimeoutError,
    SystemBlock,
    Usage,
)
from api.llm.factory import build_llm_client
from api.llm.fake import FAKE_MODEL, FAKE_PROVIDER, FakeLLMClient
from api.llm.instrumented import InstrumentedLLMClient
from api.llm.pricing import UnknownModelPricing
from api.observability.logging import LOGGER_NAME, bind_trace_id, reset_trace_id

COST = Decimal("0.001234")


@pytest.fixture(autouse=True)
def fixed_price(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Usage]]:
    calls: list[tuple[str, Usage]] = []

    def fake_estimate(model: str, usage: Usage) -> Decimal:
        calls.append((model, usage))
        return COST

    monkeypatch.setattr("api.llm.instrumented.estimate_cost", fake_estimate)
    return calls


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def records() -> Iterator[list[logging.LogRecord]]:
    logger = logging.getLogger(LOGGER_NAME)
    handler = _ListHandler()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield handler.records
    logger.removeHandler(handler)
    logger.setLevel(previous_level)


class _NoPricing(UnknownModelPricing):
    """Raised by the patched pricing; independent of the real constructor signature."""

    def __init__(self) -> None:
        LookupError.__init__(self, "no pricing")


def _events(records: list[logging.LogRecord], name: str) -> list[logging.LogRecord]:
    return [r for r in records if getattr(r, "event", None) == name]


def _request() -> LLMRequest:
    return LLMRequest(
        system=[SystemBlock("You are a support agent.")],
        messages=[ChatMessage(role="user", content="Where is my order?")],
        purpose="chat",
    )


def _response() -> LLMResponse:
    return LLMResponse(
        text="It shipped.",
        tool_calls=[],
        usage=Usage(input_tokens=100, output_tokens=20, cache_read_tokens=5, cache_write_tokens=2),
        model=FAKE_MODEL,
        provider=FAKE_PROVIDER,
        stop_reason="end_turn",
    )


def _client(*script: LLMResponse | LLMError) -> InstrumentedLLMClient:
    return InstrumentedLLMClient(
        FakeLLMClient(script=list(script)), model=FAKE_MODEL, provider=FAKE_PROVIDER
    )


def _field(record: logging.LogRecord, name: str) -> Any:
    return getattr(record, name)


async def test_successful_call_emits_one_llm_call_log(
    records: list[logging.LogRecord], fixed_price: list[tuple[str, Usage]]
) -> None:
    client = _client(_response())
    token = bind_trace_id("trace-abc")
    try:
        response = await client.complete(_request())
    finally:
        reset_trace_id(token)

    assert response == _response()
    logs = _events(records, "llm_call")
    assert len(logs) == 1
    log = logs[0]
    assert _field(log, "trace_id") == "trace-abc"
    assert _field(log, "model") == FAKE_MODEL
    assert _field(log, "provider") == FAKE_PROVIDER
    assert _field(log, "purpose") == "chat"
    assert _field(log, "input_tokens") == 100
    assert _field(log, "output_tokens") == 20
    assert _field(log, "cache_read_tokens") == 5
    assert _field(log, "cache_write_tokens") == 2
    assert _field(log, "context_tokens") > 0
    assert _field(log, "cost_usd") == COST
    assert _field(log, "latency_ms") >= 0
    assert _field(log, "outcome") == "ok"
    assert fixed_price == [(FAKE_MODEL, _response().usage)]


async def test_failed_call_logs_error_and_reraises(records: list[logging.LogRecord]) -> None:
    error = LLMError("overloaded", recoverable=True, status=529)
    client = _client(error)

    with pytest.raises(LLMError) as caught:
        await client.complete(_request())

    assert caught.value is error
    logs = _events(records, "llm_call")
    assert len(logs) == 1
    log = logs[0]
    assert _field(log, "outcome") == "error"
    assert _field(log, "error_type") == "LLMError"
    assert _field(log, "status") == 529
    assert _field(log, "recoverable") is True
    assert _field(log, "input_tokens") == 0
    assert _field(log, "output_tokens") == 0
    assert _field(log, "context_tokens") > 0
    assert _field(log, "trace_id")


async def test_unknown_pricing_keeps_response_and_never_logs_zero_cost(
    records: list[logging.LogRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_price(model: str, usage: Usage) -> Decimal:
        raise _NoPricing()

    monkeypatch.setattr("api.llm.instrumented.estimate_cost", no_price)
    client = _client(_response())

    response = await client.complete(_request())

    assert response == _response()
    call_log = _events(records, "llm_call")[0]
    assert _field(call_log, "cost_usd") is None
    assert _field(call_log, "pricing_error") == "unknown_model"
    assert _field(call_log, "outcome") == "ok"
    missing = _events(records, "pricing_missing")
    assert len(missing) == 1
    assert _field(missing[0], "model") == FAKE_MODEL


async def test_broken_pricing_config_keeps_response_without_cost(
    records: list[logging.LogRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(model: str, usage: Usage) -> Decimal:
        raise ValueError("pricing file is not valid JSON")

    monkeypatch.setattr("api.llm.instrumented.estimate_cost", broken)
    client = _client(_response())

    response = await client.complete(_request())

    assert response == _response()
    call_log = _events(records, "llm_call")[0]
    assert _field(call_log, "cost_usd") is None
    assert _field(call_log, "pricing_error") == "pricing_unavailable"
    assert len(_events(records, "pricing_error")) == 1


async def test_stream_passes_events_and_logs_usage(records: list[logging.LogRecord]) -> None:
    client = _client(_response())

    events = [event async for event in client.stream(_request())]

    assert "".join(e.text for e in events if e.kind == "text_delta") == "It shipped."
    logs = _events(records, "llm_call")
    assert len(logs) == 1
    assert _field(logs[0], "outcome") == "ok"
    assert _field(logs[0], "input_tokens") == 100
    assert _field(logs[0], "cost_usd") == COST


async def test_stream_error_logs_and_reraises(records: list[logging.LogRecord]) -> None:
    client = _client(LLMError("boom", recoverable=False, status=400))

    with pytest.raises(LLMError, match="boom"):
        async for _ in client.stream(_request()):
            pass

    logs = _events(records, "llm_call")
    assert len(logs) == 1
    assert _field(logs[0], "outcome") == "error"


def _settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_factory_fake_provider_needs_no_key() -> None:
    client = build_llm_client(_settings(llm_provider="fake", anthropic_api_key=None))

    assert isinstance(client, InstrumentedLLMClient)
    assert isinstance(client, LLMClient)
    assert client.model == FAKE_MODEL
    assert client.provider == FAKE_PROVIDER
    assert isinstance(client.inner, FakeLLMClient)


def test_factory_anthropic_without_key_raises_missing_config() -> None:
    settings = _settings(llm_provider="anthropic", anthropic_api_key=None, primary_model="m")
    with pytest.raises(MissingConfigError, match="ANTHROPIC_API_KEY"):
        build_llm_client(settings)


def test_factory_anthropic_with_key_builds_instrumented_adapter() -> None:
    from api.llm.anthropic_adapter import AnthropicAdapter

    settings = _settings(
        llm_provider="anthropic",
        anthropic_api_key=SecretStr("sk-ant-test"),
        primary_model="claude-test",
    )
    client = build_llm_client(settings)

    assert isinstance(client, InstrumentedLLMClient)
    assert client.model == "claude-test"
    assert client.provider == "anthropic"
    assert isinstance(client.inner, AnthropicAdapter)


@pytest.mark.parametrize(
    "error",
    [
        LLMTimeoutError("timed out"),
        LLMError("connection failed", recoverable=True),
        LLMError("overloaded", recoverable=True, status=529),
        LLMError("bad gateway", recoverable=True, status=502),
        LLMError("fake script exhausted", recoverable=False),
        LLMError("odd", recoverable=False, status=500),
        RuntimeError("unexpected"),
    ],
)
async def test_failure_that_may_be_billed_logs_unknown_cost(
    records: list[logging.LogRecord], error: Exception
) -> None:
    client = InstrumentedLLMClient(_RaisingClient(error), model=FAKE_MODEL, provider=FAKE_PROVIDER)

    with pytest.raises(type(error)):
        await client.complete(_request())

    log = _events(records, "llm_call")[0]
    assert _field(log, "outcome") == "error"
    assert _field(log, "cost_usd") is None
    assert _field(log, "pricing_error") == "cost_unknown"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_rejected_request_logs_zero_cost(
    records: list[logging.LogRecord], status: int
) -> None:
    client = _client(LLMError("rejected", recoverable=False, status=status))

    with pytest.raises(LLMError):
        await client.complete(_request())

    log = _events(records, "llm_call")[0]
    assert _field(log, "cost_usd") == Decimal("0")
    assert not hasattr(log, "pricing_error")


class _RaisingClient:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise self.error

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[LLMStreamEvent]:
        raise self.error
        yield  # pragma: no cover


class _EndlessStream:
    """Streams one usage event, then text forever (waiting `delay` before each chunk, like a
    provider); records whether it was closed."""

    def __init__(self, delay: float = 0.0) -> None:
        self.closed = False
        self.delay = delay

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise AssertionError("not used")

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[LLMStreamEvent]:
        try:
            yield LLMStreamEvent(kind="usage", usage=Usage(input_tokens=50, output_tokens=3))
            while True:
                await asyncio.sleep(self.delay)
                yield LLMStreamEvent(kind="text_delta", text="word ")
        finally:
            self.closed = True


def test_stream_doubles_satisfy_protocol() -> None:
    assert isinstance(_EndlessStream(), LLMClient)


async def test_stream_aclose_logs_exactly_one_cancelled_call(
    records: list[logging.LogRecord],
) -> None:
    inner = _EndlessStream()
    client = InstrumentedLLMClient(inner, model=FAKE_MODEL, provider=FAKE_PROVIDER)
    stream = client.stream(_request())

    async for event in stream:
        if event.kind == "text_delta":
            break
    await stream.aclose()  # type: ignore[attr-defined]
    await stream.aclose()  # type: ignore[attr-defined]

    logs = _events(records, "llm_call")
    assert len(logs) == 1
    assert _field(logs[0], "outcome") == "cancelled"
    assert _field(logs[0], "input_tokens") == 50
    assert _field(logs[0], "output_tokens") == 3
    assert _field(logs[0], "cost_usd") == COST
    assert inner.closed is True


async def test_stream_break_without_aclose_logs_exactly_one_call(
    records: list[logging.LogRecord],
) -> None:
    client = InstrumentedLLMClient(_EndlessStream(), model=FAKE_MODEL, provider=FAKE_PROVIDER)

    async for event in client.stream(_request()):
        if event.kind == "text_delta":
            break
    gc.collect()
    for _ in range(5):
        await asyncio.sleep(0)

    logs = _events(records, "llm_call")
    assert len(logs) == 1
    assert _field(logs[0], "outcome") == "cancelled"


async def test_stream_cancelled_before_usage_logs_unknown_cost(
    records: list[logging.LogRecord],
) -> None:
    client = _client(_response())
    stream = client.stream(_request())

    first = await stream.__anext__()
    assert first.kind == "text_delta"
    await stream.aclose()  # type: ignore[attr-defined]

    logs = _events(records, "llm_call")
    assert len(logs) == 1
    assert _field(logs[0], "outcome") == "cancelled"
    assert _field(logs[0], "cost_usd") is None
    assert _field(logs[0], "pricing_error") == "cost_unknown"


async def test_stream_task_cancellation_logs_exactly_one_call(
    records: list[logging.LogRecord],
) -> None:
    inner = _EndlessStream(delay=10.0)
    client = InstrumentedLLMClient(inner, model=FAKE_MODEL, provider=FAKE_PROVIDER)
    started = asyncio.Event()

    async def consume() -> None:
        async for _ in client.stream(_request()):
            started.set()

    task = asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    logs = _events(records, "llm_call")
    assert len(logs) == 1
    assert _field(logs[0], "outcome") == "cancelled"
    assert _field(logs[0], "input_tokens") == 50
    assert inner.closed is True


class _SlowClient:
    """`complete` waits like a provider call, so a task can be cancelled mid-call."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.entered.set()
        await asyncio.sleep(10)
        raise AssertionError("not reached")

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        raise AssertionError("not used")


async def test_complete_cancelled_logs_exactly_one_call_and_reraises(
    records: list[logging.LogRecord],
) -> None:
    inner = _SlowClient()
    client = InstrumentedLLMClient(inner, model=FAKE_MODEL, provider=FAKE_PROVIDER)

    task = asyncio.create_task(client.complete(_request()))
    await inner.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    logs = _events(records, "llm_call")
    assert len(logs) == 1
    assert _field(logs[0], "outcome") == "cancelled"
    assert _field(logs[0], "cost_usd") is None
    assert _field(logs[0], "pricing_error") == "cost_unknown"
    assert _field(logs[0], "input_tokens") == 0
    assert _field(logs[0], "context_tokens") > 0


@pytest.mark.parametrize("provider", ["claude_cli", "codex_cli"])
async def test_subscription_provider_skips_pricing(
    provider: str, records: list[logging.LogRecord], fixed_price: list[tuple[str, Usage]]
) -> None:
    client = InstrumentedLLMClient(
        FakeLLMClient(script=[_response()]), model="claude-subscription", provider=provider
    )

    response = await client.complete(_request())

    assert response == _response()
    assert fixed_price == []
    assert _events(records, "pricing_missing") == []
    logs = _events(records, "llm_call")
    assert len(logs) == 1
    assert _field(logs[0], "provider") == provider
    assert _field(logs[0], "outcome") == "ok"
    assert _field(logs[0], "input_tokens") == 100
    assert _field(logs[0], "cost_usd") is None
    assert _field(logs[0], "pricing_error") == "subscription"


async def test_subscription_provider_error_keeps_cost_unknown(
    records: list[logging.LogRecord],
) -> None:
    client = InstrumentedLLMClient(
        FakeLLMClient(script=[LLMTimeoutError("timed out")]),
        model="codex-subscription",
        provider="codex_cli",
    )

    with pytest.raises(LLMTimeoutError):
        await client.complete(_request())

    logs = _events(records, "llm_call")
    assert len(logs) == 1
    assert _field(logs[0], "outcome") == "error"
    assert _field(logs[0], "cost_usd") is None
    assert _field(logs[0], "pricing_error") == "cost_unknown"
