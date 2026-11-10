"""Prompt cache markers and cache token accounting (TASK-111: CACHE-01, CACHE-03, CACHE-90).

The Anthropic adapter runs over `httpx2.MockTransport`: no network call is ever made.
"""

import json
import logging
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from pydantic import SecretStr

from api.chat.context import HistoryWindow
from api.chat.prompt_builder import build_request
from api.config import Settings, get_settings
from api.llm.anthropic_adapter import MAX_CACHE_BREAKPOINTS, AnthropicAdapter
from api.llm.client import ChatMessage, LLMRequest, LLMStreamEvent, SystemBlock, Usage
from api.llm.instrumented import InstrumentedLLMClient
from api.llm.pricing import estimate_cost, load_pricing_table
from api.observability.logging import LOGGER_NAME

API_HOST = "api.anthropic.com"
FAKE_KEY = "sk-ant-test-0123456789-secret"
MODEL = "claude-test-model"
EPHEMERAL = {"type": "ephemeral"}

# USD per million tokens. Cache reads cost a tenth of an input token, writes 1.25x.
PRICES: dict[str, Any] = {
    MODEL: {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75},
}

CACHED_USAGE: dict[str, Any] = {
    "input_tokens": 100,
    "output_tokens": 20,
    "cache_read_input_tokens": 2000,
    "cache_creation_input_tokens": 400,
}
# 100*3 + 20*15 + 2000*0.3 + 400*3.75 = 300 + 300 + 600 + 1500 = 2700 per million.
CACHED_COST = Decimal("0.0027")

Handler = Callable[[httpx.Request], httpx.Response]


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        llm_provider="anthropic",
        anthropic_api_key=SecretStr(FAKE_KEY),
        primary_model=MODEL,
        llm_timeout_seconds=5.0,
    )


def _adapter(handler: Handler, captured: list[httpx.Request] | None = None) -> AnthropicAdapter:
    sink = captured if captured is not None else []

    def guarded(request: httpx.Request) -> httpx.Response:
        if request.url.host != API_HOST:
            raise AssertionError(f"unexpected network call to {request.url.host}")
        sink.append(request)
        return handler(request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(guarded))
    return AnthropicAdapter(_settings(), http_client=http_client)


def _request(system: list[SystemBlock]) -> LLMRequest:
    return LLMRequest(
        system=system,
        messages=[ChatMessage(role="user", content="Where is order 42?")],
    )


def _payload(request: httpx.Request) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(request.content)
    return data


def _message_body(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": [{"type": "text", "text": "On its way."}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": usage,
    }


def _ok(usage: dict[str, Any]) -> Handler:
    body = _message_body(usage)
    return lambda _: httpx.Response(200, json=body)


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _stream_body(start_usage: dict[str, Any], delta_usage: dict[str, Any]) -> str:
    message = {**_message_body(start_usage), "content": [], "stop_reason": None}
    events = [
        _sse("message_start", {"type": "message_start", "message": message}),
        _sse(
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        ),
        _sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "On its way."},
            },
        ),
        _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": delta_usage,
            },
        ),
        _sse("message_stop", {"type": "message_stop"}),
    ]
    return "".join(events)


def _stream_ok(start_usage: dict[str, Any], delta_usage: dict[str, Any]) -> Handler:
    body = _stream_body(start_usage, delta_usage)
    return lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=body.encode()
    )


async def _stream_usage(adapter: AnthropicAdapter, request: LLMRequest) -> Usage:
    events: list[LLMStreamEvent] = [event async for event in adapter.stream(request)]
    usages = [event.usage for event in events if event.kind == "usage"]
    assert len(usages) == 1
    usage = usages[0]
    assert usage is not None
    return usage


def _cache_controls(request: httpx.Request) -> list[object]:
    """`cache_control` of every system block, in order (None when absent)."""
    return [block.get("cache_control") for block in _payload(request)["system"]]


def _breakpoints_outside_system(payload: dict[str, Any]) -> int:
    text = json.dumps({key: value for key, value in payload.items() if key != "system"})
    return text.count("cache_control")


@pytest.fixture
def pricing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps(PRICES), encoding="utf-8")
    monkeypatch.setenv("ATLAS_PRICING_FILE", str(path))
    get_settings.cache_clear()
    load_pricing_table.cache_clear()
    yield path
    get_settings.cache_clear()
    load_pricing_table.cache_clear()


STABLE = SystemBlock("You are a support agent. Reply in English.", cache_breakpoint=True)
VARIABLE = SystemBlock("Today is 2026-11-09.")


# --- cache_control markers (CACHE-01) -----------------------------------------------------


async def test_complete_marks_only_the_breakpoint_block() -> None:
    captured: list[httpx.Request] = []
    adapter = _adapter(_ok(CACHED_USAGE), captured)

    await adapter.complete(_request([STABLE, VARIABLE]))

    assert _cache_controls(captured[0]) == [EPHEMERAL, None]
    assert _breakpoints_outside_system(_payload(captured[0])) == 0


async def test_stream_marks_only_the_breakpoint_block() -> None:
    captured: list[httpx.Request] = []
    adapter = _adapter(_stream_ok(CACHED_USAGE, {"output_tokens": 20}), captured)

    await _stream_usage(adapter, _request([STABLE, VARIABLE]))

    assert _payload(captured[0])["stream"] is True
    assert _cache_controls(captured[0]) == [EPHEMERAL, None]
    assert _breakpoints_outside_system(_payload(captured[0])) == 0


async def test_cache_ordered_request_has_one_breakpoint_on_the_stable_prompt() -> None:
    """`cache_ordered` (real `build_request` output): only the grounded system prompt is a
    breakpoint; the language and summary block after it is variable and gets no marker."""
    captured: list[httpx.Request] = []
    adapter = _adapter(_ok(CACHED_USAGE), captured)
    history = HistoryWindow(
        summary="The customer asked about order 42.",
        messages=[],
        tokens=0,
        summarized_upto=None,
    )
    request = build_request(
        language="en",
        history=history,
        chunks=[],
        user_message="Where is order 42?",
        tools=[],
        layout="cache_ordered",
    )

    await adapter.complete(request)

    assert _cache_controls(captured[0]) == [EPHEMERAL, None]
    assert captured[0].content.decode().count("cache_control") == 1


async def test_no_breakpoint_sends_no_cache_control() -> None:
    captured: list[httpx.Request] = []
    adapter = _adapter(_ok(CACHED_USAGE), captured)

    await adapter.complete(_request([SystemBlock("You are a support agent."), VARIABLE]))

    assert "cache_control" not in captured[0].content.decode()


async def test_at_most_four_breakpoints_keeping_the_last_ones() -> None:
    """The API rejects more than 4 breakpoints; the last ones cover the longest prefix."""
    captured: list[httpx.Request] = []
    adapter = _adapter(_ok(CACHED_USAGE), captured)
    blocks = [SystemBlock(f"Policy {n}.", cache_breakpoint=True) for n in range(6)]

    await adapter.complete(_request(blocks))

    assert MAX_CACHE_BREAKPOINTS == 4
    assert _cache_controls(captured[0]) == [None, None] + [EPHEMERAL] * 4


async def test_breakpoint_on_an_empty_block_moves_to_the_previous_block() -> None:
    """An empty block is not sent; its breakpoint still ends the same stable prefix."""
    captured: list[httpx.Request] = []
    adapter = _adapter(_ok(CACHED_USAGE), captured)
    blocks = [SystemBlock("You are a support agent."), SystemBlock("", cache_breakpoint=True)]

    await adapter.complete(_request([*blocks, VARIABLE]))

    assert _cache_controls(captured[0]) == [EPHEMERAL, None]


async def test_breakpoint_on_a_leading_empty_block_is_dropped() -> None:
    captured: list[httpx.Request] = []
    adapter = _adapter(_ok(CACHED_USAGE), captured)

    await adapter.complete(_request([SystemBlock("", cache_breakpoint=True), VARIABLE]))

    assert "cache_control" not in captured[0].content.decode()


# --- cache token accounting (CACHE-03) ----------------------------------------------------


async def test_complete_reads_cache_tokens_and_prices_them(pricing_file: Path) -> None:
    adapter = _adapter(_ok(CACHED_USAGE))

    response = await adapter.complete(_request([STABLE, VARIABLE]))

    assert response.usage == Usage(
        input_tokens=100, output_tokens=20, cache_read_tokens=2000, cache_write_tokens=400
    )
    assert estimate_cost(MODEL, response.usage) == CACHED_COST


async def test_stream_reads_cache_tokens_and_prices_them(pricing_file: Path) -> None:
    adapter = _adapter(_stream_ok(CACHED_USAGE, {"output_tokens": 20}))

    usage = await _stream_usage(adapter, _request([STABLE, VARIABLE]))

    assert usage == Usage(
        input_tokens=100, output_tokens=20, cache_read_tokens=2000, cache_write_tokens=400
    )
    assert estimate_cost(MODEL, usage) == CACHED_COST


async def test_stream_takes_cumulative_cache_tokens_from_message_delta() -> None:
    delta = {"output_tokens": 20, "cache_read_input_tokens": 2500, "cache_creation_input_tokens": 0}
    adapter = _adapter(_stream_ok(CACHED_USAGE, delta))

    usage = await _stream_usage(adapter, _request([STABLE, VARIABLE]))

    assert usage.cache_read_tokens == 2500
    assert usage.cache_write_tokens == 0


def test_cache_read_is_priced_by_cache_read_not_input(pricing_file: Path) -> None:
    """`input_tokens` excludes cache tokens: a cache hit costs the cache_read price only."""
    hit = Usage(input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000)
    write = Usage(input_tokens=0, output_tokens=0, cache_write_tokens=1_000_000)

    assert estimate_cost(MODEL, hit) == Decimal("0.3")
    assert estimate_cost(MODEL, write) == Decimal("3.75")


async def test_cache_tokens_and_cache_cost_reach_the_llm_call_log(
    pricing_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = InstrumentedLLMClient(_adapter(_ok(CACHED_USAGE)), model=MODEL, provider="anthropic")

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        await client.complete(_request([STABLE, VARIABLE]))

    calls = [r for r in caplog.records if getattr(r, "event", None) == "llm_call"]
    assert len(calls) == 1
    record = calls[0]
    assert getattr(record, "cache_read_tokens") == 2000  # noqa: B009
    assert getattr(record, "cache_write_tokens") == 400  # noqa: B009
    assert getattr(record, "cost_usd") == CACHED_COST  # noqa: B009


# --- no cache fields → zeros (CACHE-90) ---------------------------------------------------

NO_CACHE_USAGE: dict[str, Any] = {"input_tokens": 100, "output_tokens": 20}
NULL_CACHE_USAGE: dict[str, Any] = {
    **NO_CACHE_USAGE,
    "cache_read_input_tokens": None,
    "cache_creation_input_tokens": None,
}


@pytest.mark.parametrize("usage", [NO_CACHE_USAGE, NULL_CACHE_USAGE], ids=["absent", "null"])
async def test_complete_without_cache_fields_reports_zeros(usage: dict[str, Any]) -> None:
    adapter = _adapter(_ok(usage))

    response = await adapter.complete(_request([VARIABLE]))

    assert response.usage == Usage(input_tokens=100, output_tokens=20)
    assert response.usage.cache_read_tokens == 0
    assert response.usage.cache_write_tokens == 0


@pytest.mark.parametrize("usage", [NO_CACHE_USAGE, NULL_CACHE_USAGE], ids=["absent", "null"])
async def test_stream_without_cache_fields_reports_zeros(usage: dict[str, Any]) -> None:
    adapter = _adapter(_stream_ok(usage, {"output_tokens": 20}))

    stream_usage = await _stream_usage(adapter, _request([VARIABLE]))

    assert stream_usage == Usage(input_tokens=100, output_tokens=20)


def test_no_cache_tokens_cost_only_input_and_output(pricing_file: Path) -> None:
    # 100*3 + 20*15 = 600 per million.
    assert estimate_cost(MODEL, Usage(input_tokens=100, output_tokens=20)) == Decimal("0.0006")
