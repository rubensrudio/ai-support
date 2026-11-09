"""Phase 09 streaming demo: TTFT and total time of `POST /chat/stream` vs `POST /chat`
(TASK-098, STR-06, DOD-02)."""

import asyncio
import re
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from api.config import get_settings
from api.db.engine import dispose_engine
from api.llm.client import LLMRequest, LLMResponse, Usage
from api.llm.fake import FakeLLMClient, split_deltas
from demos.common import DEFAULT_RESULTS_PATH
from demos.demo_09_streaming import (
    QUESTIONS,
    PacedFakeLLM,
    SseReader,
    StreamingASGITransport,
    Timing,
    after_cell,
    before_cell,
    build_parser,
    main,
    percentile,
    questions_for,
    stream_ok,
)

pytestmark = pytest.mark.integration

_MS = r"(\d+) ms"


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "fake")
    monkeypatch.setenv("ATLAS_EMBEDDING_PROVIDER", "fake")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def fresh_engine(migrated_database: str) -> AsyncIterator[None]:
    """The demo runs its own event loop: close pooled connections around it."""
    await dispose_engine()
    yield
    await dispose_engine()


# --- pure helpers ----------------------------------------------------------------------------


def test_parser_has_the_common_flags_and_the_demo_flags() -> None:
    args = build_parser().parse_args([])

    assert args.provider == "real"
    assert args.results is None
    assert args.n == 10
    assert args.base_url is None
    assert args.delta_delay_ms > 0


def test_base_url_cannot_be_combined_with_the_fake_provider(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--provider", "fake", "--base-url", "http://localhost:8000"])

    assert raised.value.code == 2
    assert "--base-url" in capsys.readouterr().err


def test_n_must_be_positive(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--n", "0"])

    assert "--n" in capsys.readouterr().err


def test_questions_alternate_languages_and_cycle() -> None:
    assert {question.language for question in QUESTIONS} == {"pt", "en"}

    picked = questions_for(len(QUESTIONS) + 3)

    assert len(picked) == len(QUESTIONS) + 3
    assert picked[: len(QUESTIONS)] == list(QUESTIONS)
    assert picked[len(QUESTIONS) :] == list(QUESTIONS[:3])
    assert {question.language for question in questions_for(2)} == {"pt", "en"}


def test_percentile_uses_the_nearest_rank() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]

    assert percentile(values, 50) == 50.0
    assert percentile(values, 95) == 100.0
    assert percentile([7.0], 95) == 7.0
    with pytest.raises(ValueError):
        percentile([], 50)


def test_sse_reader_parses_blocks_split_across_chunks() -> None:
    reader = SseReader()

    first = reader.feed(b'event: token\ndata: {"type": "token", "text": "Ol')
    second = reader.feed('á"}\n\nevent: done\ndata: {"type": "done"}\n'.encode())
    third = reader.feed(b"\n")

    assert first == []
    assert second == [("token", {"type": "token", "text": "Olá"})]
    assert third == [("done", {"type": "done"})]


def test_stream_ok_needs_one_final_completed_done_matching_the_tokens() -> None:
    start = ("start", {"type": "start", "trace_id": "t"})
    done = ("done", {"type": "done", "status": "completed", "content": "Hi there"})
    tokens = [("token", {"text": "Hi "}), ("token", {"text": "there"})]

    assert stream_ok([start, *tokens, done])
    # The plain chat path (codex_cli, tools off) sends no token: the answer is in `done`.
    assert stream_ok([start, done])
    assert not stream_ok([start, ("token", {"text": "Hi"}), done])
    assert not stream_ok([start, *tokens, ("error", {"code": "LLM_UNAVAILABLE"})])
    assert not stream_ok([start, *tokens, done, done])
    assert not stream_ok([start, ("done", {"status": "limit_reached", "content": "x"})])
    assert not stream_ok([start, ("done", {"status": "completed", "content": ""})])
    assert not stream_ok([])


def test_stream_ok_needs_the_start_event_first_and_once() -> None:
    start = ("start", {"type": "start", "trace_id": "t"})
    token = ("token", {"text": "Hi"})
    done = ("done", {"type": "done", "status": "completed", "content": "Hi"})

    assert stream_ok([start, token, done])
    assert not stream_ok([token, done])
    assert not stream_ok([token, start, done])
    assert not stream_ok([start, start, token, done])


def test_stream_ok_with_tools_needs_the_tokens_to_end_with_the_last_call_text() -> None:
    start = ("start", {"type": "start", "trace_id": "t"})
    tool = [("tool_start", {"call_id": "c"}), ("tool_end", {"call_id": "c"})]
    done = ("done", {"type": "done", "status": "completed", "content": "Your plan is Pro."})
    citation = ("citation", {"index": 1, "document": "plans.md"})
    # Text before the tool call is streamed too, but `done.content` is the last call's text.
    preface = ("token", {"text": "Let me check. "})
    answer = [("token", {"text": "Your plan "}), ("token", {"text": "is Pro."})]

    assert stream_ok([start, preface, *tool, *answer, citation, done])
    assert stream_ok([start, *tool, *answer, done])
    assert not stream_ok([start, *tool, preface, done])
    assert not stream_ok([start, preface, *answer, done])  # no tools: tokens must equal it


def test_cells_report_ttft_and_total() -> None:
    before = [Timing(language="en", ttft_ms=300.0, total_ms=300.0, ok=True)] * 2
    after = [
        Timing(language="en", ttft_ms=20.0, total_ms=310.0, ok=True),
        Timing(language="pt", ttft_ms=40.0, total_ms=330.0, ok=True),
    ]

    assert before_cell(before) == (
        "POST /chat (JSON): TTFT = total, p50 300 ms, p95 300 ms (n=2, ok 2/2)"
    )
    assert after_cell(after) == (
        "POST /chat/stream (SSE): TTFT p50 20 ms, p95 40 ms; total p50 310 ms, p95 330 ms "
        "(n=2, ok 2/2)"
    )


# --- paced fake and streaming transport ------------------------------------------------------


def _response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        usage=Usage(input_tokens=1, output_tokens=1),
        model="fake-model",
        provider="fake",
        stop_reason="end_turn",
    )


async def test_paced_fake_streams_deltas_with_a_delay_and_complete_takes_the_whole_time() -> None:
    text = "one two three four"
    request = LLMRequest(system=[], messages=[], max_tokens=10)
    llm = PacedFakeLLM(FakeLLMClient([_response(text), _response(text)]), delay_s=0.02)

    started = time.perf_counter()
    first_at: float | None = None
    chunks: list[str] = []
    async for event in llm.stream(request):
        if event.kind == "text_delta" and event.text:
            first_at = first_at or time.perf_counter() - started
            chunks.append(event.text)
    streamed = time.perf_counter() - started

    started = time.perf_counter()
    response = await llm.complete(request)
    completed = time.perf_counter() - started

    assert "".join(chunks) == text == response.text
    assert first_at is not None and first_at < streamed / 2
    deltas = len(split_deltas(text))
    assert completed >= 0.02 * deltas * 0.9


async def test_streaming_transport_delivers_chunks_before_the_response_ends() -> None:
    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        await asyncio.sleep(0.2)
        await send({"type": "http.response.body", "body": b"second", "more_body": False})

    transport = StreamingASGITransport(app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        started = time.perf_counter()
        arrivals: list[tuple[bytes, float]] = []
        async with client.stream("POST", "/", json={}) as response:
            async for chunk in response.aiter_raw():
                arrivals.append((chunk, time.perf_counter() - started))

    assert [chunk for chunk, _ in arrivals] == [b"first", b"second"]
    assert arrivals[0][1] < 0.1 <= arrivals[1][1]


async def test_streaming_transport_reports_disconnect_only_after_the_client_closes() -> None:
    seen: list[str] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen.append((await receive())["type"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"x", "more_body": True})
        seen.append((await receive())["type"])  # blocks until the client closes

    transport = StreamingASGITransport(app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream("POST", "/", content=b"body") as response:
            assert await anext(response.aiter_raw()) == b"x"
            await asyncio.sleep(0.05)
            assert seen == ["http.request"]

    assert seen == ["http.request", "http.disconnect"]


# --- full run --------------------------------------------------------------------------------


def _ms(cell: str, label: str) -> list[int]:
    match = re.search(rf"{label} p50 {_MS}, p95 {_MS}", cell)
    assert match, (label, cell)
    return [int(match.group(1)), int(match.group(2))]


@pytest.mark.usefixtures("fresh_engine")
def test_fake_run_records_the_row_with_ttft_and_total_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results.md"

    exit_code = main(["--provider", "fake", "--n", "4", "--results", str(results)])

    output = capsys.readouterr().out
    assert exit_code == 0, output
    rows = [line for line in results.read_text().splitlines() if line.startswith("| 09 |")]
    assert len(rows) == 1
    _, _, metric, before, after, notes, _ = rows[0].split("|")
    assert "TTFT" in metric and "total" in metric.lower()
    assert "POST /chat (JSON): TTFT = total" in before
    assert "POST /chat/stream (SSE): TTFT" in after
    assert "(n=4, ok 4/4)" in before and "(n=4, ok 4/4)" in after
    chat_total = _ms(before, "TTFT = total,")
    stream_ttft = _ms(after, "TTFT")
    stream_total = _ms(after, "total")
    # The first token arrives long before the paced answer ends, and before /chat answers.
    assert stream_ttft[0] < stream_total[0]
    assert stream_ttft[0] < chat_total[0]
    assert "provider=fake" in notes and "claude_cli" in notes
    assert output.count("[stream]") == 4 and output.count("[chat]") == 4
    assert output.count("events start..done") == 4
    assert "[pt]" in output and "[en]" in output


@pytest.mark.usefixtures("fresh_engine")
def test_fake_run_without_results_writes_to_a_temp_dir_and_never_to_docs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    before = DEFAULT_RESULTS_PATH.read_bytes() if DEFAULT_RESULTS_PATH.exists() else None

    exit_code = main(["--provider", "fake", "--n", "2"])

    assert exit_code == 0
    after = DEFAULT_RESULTS_PATH.read_bytes() if DEFAULT_RESULTS_PATH.exists() else None
    assert after == before
    assert "| 09 |" in (tmp_path / "atlas-demo09" / "results.md").read_text()
