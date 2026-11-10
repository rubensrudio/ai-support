"""Phase 11 prompt cache demo: cost and latency per call and cache hit rate, `naive` (before)
vs `cache_ordered` (after) (TASK-112, CACHE-04, DOD-02)."""

import tempfile
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from api.chat.context import HistoryWindow
from api.chat.prompt_builder import build_request
from api.config import get_settings
from api.db.engine import dispose_engine
from api.llm.client import ChatMessage, LLMRequest, SystemBlock, ToolSpec, Usage
from demos.common import DEFAULT_RESULTS_PATH
from demos.demo_11_prompt_cache import (
    CallRecord,
    PromptCacheSimulator,
    SimulatedCacheLLM,
    build_parser,
    call_cost,
    conversations_for,
    main,
    metrics_cell,
    summarize,
)

pytestmark = pytest.mark.integration

TOOLS = [ToolSpec("get_subscription", "Read the plan.", {"type": "object", "properties": {}})]


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "fake")
    monkeypatch.setenv("ATLAS_EMBEDDING_PROVIDER", "fake")
    monkeypatch.delenv("ATLAS_GUARDRAIL_MODEL", raising=False)
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


def _request(language: str, layout: str, at: datetime, question: str) -> LLMRequest:
    return build_request(
        language="pt" if language == "pt" else "en",
        history=HistoryWindow(messages=[], summary=None, tokens=0, summarized_upto=None),
        chunks=[],
        user_message=question,
        tools=TOOLS,
        layout="naive" if layout == "naive" else "cache_ordered",
        clock=lambda: at,
    )


def test_conversations_alternate_languages_with_two_or_three_turns() -> None:
    conversations = conversations_for(5)

    assert [c.language for c in conversations] == ["en", "pt", "en", "pt", "en"]
    assert [len(c.turns) for c in conversations] == [2, 2, 3, 3, 2]
    assert len(conversations_for(100)) == 100
    assert Counter(c.language for c in conversations_for(100)) == {"en": 50, "pt": 50}
    for conversation in conversations_for(100):
        assert 2 <= len(conversation.turns) <= 3
        assert all(turn.strip() for turn in conversation.turns)
    for language in ("en", "pt"):
        lengths = {len(c.turns) for c in conversations_for(100) if c.language == language}
        assert lengths == {2, 3}, language


async def test_simulated_cache_reads_a_prefix_seen_before_across_languages() -> None:
    llm = SimulatedCacheLLM(PromptCacheSimulator())
    now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)

    first = await llm.complete(_request("en", "cache_ordered", now, "Can I pause my plan?"))
    second = await llm.complete(_request("pt", "cache_ordered", now, "Posso pausar meu plano?"))

    assert first.usage.cache_read_tokens == 0 and first.usage.cache_write_tokens > 0
    assert second.usage.cache_read_tokens == first.usage.cache_write_tokens
    assert second.usage.cache_write_tokens == 0
    # The answer follows the reply-language instruction of the request.
    assert "subscription" in first.text and "assinatura" in second.text


async def test_simulated_cache_never_hits_when_the_date_is_on_top() -> None:
    llm = SimulatedCacheLLM(PromptCacheSimulator())
    now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)

    first = await llm.complete(_request("en", "naive", now, "Can I pause my plan?"))
    later = now + timedelta(seconds=1)
    second = await llm.complete(_request("en", "naive", later, "Can I pause my plan?"))
    same_instant = await llm.complete(_request("en", "naive", now, "Can I pause my plan?"))

    # No breakpoint: nothing is written; a different instant changes the prefix.
    assert first.usage.cache_write_tokens == 0 and first.usage.cache_read_tokens == 0
    assert second.usage.cache_read_tokens == 0
    # The simulator compares bytes: only the very same prefix (same instant) is read.
    assert same_instant.usage.cache_read_tokens > 0


async def test_simulated_cache_requires_a_language_instruction() -> None:
    llm = SimulatedCacheLLM(PromptCacheSimulator())
    request = LLMRequest(system=[SystemBlock("no language")], messages=[ChatMessage("user", "hi")])

    with pytest.raises(ValueError, match="language"):
        await llm.complete(request)


def test_call_cost_is_unknown_for_subscription_and_unpriced_models() -> None:
    usage = Usage(input_tokens=100, output_tokens=10, cache_read_tokens=50)

    assert call_cost("fake-model", "fake", usage) == Decimal("0")
    assert call_cost("sonnet", "claude_cli", usage) is None
    assert call_cost("no-such-model", "anthropic", usage) is None


def _record(ms: float, read: int, cost: Decimal | None = Decimal("0.001")) -> CallRecord:
    usage = Usage(input_tokens=100, output_tokens=20, cache_read_tokens=read)
    return CallRecord(latency_ms=ms, usage=usage, cost_usd=cost)


def test_summary_averages_cost_and_latency_and_counts_cache_hits() -> None:
    summary = summarize([_record(10.0, 0), _record(20.0, 300), _record(30.0, 300)])

    assert (summary.calls, summary.cache_hits) == (3, 2)
    assert summary.avg_cost_usd == Decimal("0.001")
    assert summary.avg_latency_ms == pytest.approx(20.0)
    assert metrics_cell(summary) == (
        "cost/call $0.001000, latency/call 20.0 ms, cache hit 2/3 (67%), "
        "tokens/call: input 100, cache read 200, cache write 0"
    )


def test_summary_reports_unknown_cost_when_any_call_is_unpriced() -> None:
    summary = summarize([_record(10.0, 0), _record(10.0, 300, cost=None)])

    assert summary.avg_cost_usd is None
    assert metrics_cell(summary).startswith("cost/call unknown (1/2 calls unpriced), ")


def test_parser_defaults_to_100_conversations() -> None:
    args = build_parser().parse_args([])

    assert args.provider == "real"
    assert args.n == 100
    assert args.results is None


@pytest.mark.usefixtures("fresh_engine")
def test_fake_run_records_the_row_with_cost_latency_and_cache_hit_rate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results.md"

    exit_code = main(["--provider", "fake", "--n", "5", "--results", str(results)])

    output = capsys.readouterr().out
    assert exit_code == 0, output
    rows = [line for line in results.read_text().splitlines() if line.startswith("| 11 |")]
    assert len(rows) == 1
    _, _, metric, before, after, notes, _ = rows[0].split("|")
    assert "5 conversations" in metric and "cache hit" in metric.lower()
    # 5 conversations, 2-3 turns each: 12 chat calls per layout.
    # Before: the date and time on top change every prefix, so nothing is read.
    assert "cache hit 0/12 (0%)" in before and "cache read 0," in before
    # After: only the first call writes the stable prefix; the other 11 read it, pt and en.
    assert "cache hit 11/12 (92%)" in after
    assert "cost/call $0.000000" in before and "cost/call $0.000000" in after
    assert "naive" in notes and "cache_ordered" in notes and "provider=fake" in notes
    assert "guardrails off" in notes and "language mismatches 0" in notes
    assert output.count("[naive] ") == 12 and output.count("[cache_ordered] ") == 12


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
    assert "| 11 |" in (tmp_path / "atlas-demo11" / "results.md").read_text()
