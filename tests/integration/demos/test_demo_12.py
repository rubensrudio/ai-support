"""Phase 12 resilience demo: final success rate and mean extra latency of N chat messages with
30% injected primary failures, without resilience (before) vs with the full chain of retry,
circuit breaker and fallback (after) (TASK-122, RES-08, DOD-02)."""

import os
import random
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from api.config import get_settings
from api.db.engine import dispose_engine
from api.llm.client import LLMError, LLMRequest
from api.llm.factory import ResilientLLMClient, reset_breakers
from api.llm.fallback import AllProvidersFailed
from api.llm.openai_adapter import PROVIDER as OPENAI_PROVIDER
from demos.common import DEFAULT_RESULTS_PATH
from demos.demo_12_resilience import (
    CHAOS_RATE,
    DEFAULT_SEED,
    MessageResult,
    ScriptedProvider,
    build_parser,
    fake_chain,
    main,
    messages_for,
    no_resilience,
    rate_cell,
    summarize,
)

pytestmark = pytest.mark.integration

REQUEST = LLMRequest(system=[], messages=[], tools=[])


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "fake")
    monkeypatch.setenv("ATLAS_EMBEDDING_PROVIDER", "fake")
    monkeypatch.delenv("ATLAS_GUARDRAIL_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CHAOS_RATE", raising=False)
    get_settings.cache_clear()
    reset_breakers()
    yield
    reset_breakers()
    get_settings.cache_clear()


@pytest.fixture
async def fresh_engine(migrated_database: str) -> AsyncIterator[None]:
    """The demo runs its own event loop: close pooled connections around it."""
    await dispose_engine()
    yield
    await dispose_engine()


def _expected_successes_without_resilience(n: int) -> int:
    """One chaos draw per message and no retry: a draw below the rate fails the message."""
    rng = random.Random(DEFAULT_SEED)  # noqa: S311 - mirrors the demo's seeded fault injection
    return sum(rng.random() >= CHAOS_RATE for _ in range(n))


def test_parser_defaults_to_50_messages_and_no_results_path() -> None:
    args = build_parser().parse_args([])

    assert args.n == 50
    assert args.provider == "real"
    assert args.results is None
    assert args.seed == DEFAULT_SEED
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--n", "0"])


def test_messages_alternate_en_and_pt() -> None:
    messages = messages_for(5)

    assert [m.language for m in messages] == ["en", "pt", "en", "pt", "en"]
    assert all(m.text.strip() for m in messages)
    assert messages[0].text != messages[2].text


def test_summary_and_cell_report_success_rate_and_extra_latency() -> None:
    results = [
        MessageResult(language="en", outcome="completed", latency_ms=30.0),
        MessageResult(language="pt", outcome="completed", latency_ms=50.0),
        MessageResult(language="en", outcome="degraded", latency_ms=90.0),
        MessageResult(language="pt", outcome="error", latency_ms=5.0),
    ]

    summary = summarize(results)

    assert (summary.messages, summary.completed, summary.degraded, summary.errors) == (4, 2, 1, 1)
    assert summary.mean_completed_latency_ms == pytest.approx(40.0)
    assert rate_cell(summary, baseline_ms=25.0) == (
        "success 2/4 (50%; degraded 1, errors 1), extra latency +15.0 ms per completed answer "
        "(40.0 ms vs 25.0 ms without chaos)"
    )


def test_cell_without_completed_answers_has_no_latency() -> None:
    summary = summarize([MessageResult(language="en", outcome="error", latency_ms=1.0)])

    assert rate_cell(summary, baseline_ms=10.0) == (
        "success 0/1 (0%; degraded 0, errors 1), extra latency n/a (no completed answer)"
    )


async def test_no_resilience_client_fails_on_the_first_injected_failure() -> None:
    provider = ScriptedProvider("fake")
    client = no_resilience(provider, model="fake-model", provider="fake", rate=1.0, seed=1)

    with pytest.raises(LLMError) as raised:
        await client.complete(REQUEST)

    assert raised.value.recoverable and raised.value.status == 503
    assert provider.calls == 0


async def test_fake_chain_retries_then_falls_back_to_the_simulated_openai() -> None:
    settings = get_settings().model_copy(
        update={"retry_base_delay_seconds": 0.0, "llm_max_retries": 2}
    )
    chain, primary, secondary = fake_chain(settings, rate=1.0, seed=1)

    response = await chain.complete(REQUEST)

    assert isinstance(chain, ResilientLLMClient)
    assert (chain.provider, [name for name, _, _ in chain.chain]) == (
        "fake",
        ["primary", "secondary"],
    )
    assert (primary.calls, secondary.calls) == (0, 1)
    assert response.provider == OPENAI_PROVIDER


async def test_fake_chain_with_a_failing_secondary_raises_all_providers_failed() -> None:
    settings = get_settings().model_copy(
        update={"retry_base_delay_seconds": 0.0, "llm_max_retries": 0}
    )
    chain, _, secondary = fake_chain(settings, rate=1.0, seed=1)
    secondary.fail = True

    with pytest.raises(AllProvidersFailed):
        await chain.complete(REQUEST)


@pytest.mark.usefixtures("fresh_engine")
def test_fake_run_records_the_row_with_success_rate_and_extra_latency(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results.md"

    exit_code = main(["--provider", "fake", "--n", "20", "--results", str(results)])

    output = capsys.readouterr().out
    assert exit_code == 0, output
    rows = [line for line in results.read_text().splitlines() if line.startswith("| 12 |")]
    assert len(rows) == 1
    _, _, metric, before, after, notes, _ = rows[0].split("|")
    assert "20 chat messages" in metric and "30%" in metric and "success" in metric.lower()
    expected_before = _expected_successes_without_resilience(20)
    assert expected_before < 20
    assert f"success {expected_before}/20 " in before
    assert f"errors {20 - expected_before})" in before
    assert "success 20/20 (100%; degraded 0, errors 0)" in after
    for cell in (before, after):
        assert "extra latency " in cell and "ms without chaos" in cell
    assert "CHAOS_RATE=0.3" in notes and "provider=fake" in notes
    assert "Before: primary only" in notes and "After: retry" in notes
    for label in ("baseline", "before", "after"):
        assert output.count(f"[{label}] message ") == 20, label
    assert "[after] message 2 [pt]: completed" in output
    # The chaos rate is only applied inside the demo, never through the environment.
    assert "CHAOS_RATE" not in os.environ
    get_settings.cache_clear()
    assert get_settings().chaos_rate == 0.0


@pytest.mark.usefixtures("fresh_engine")
def test_fake_run_without_results_writes_to_a_temp_dir_and_never_to_docs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    before = DEFAULT_RESULTS_PATH.read_bytes() if DEFAULT_RESULTS_PATH.exists() else None

    exit_code = main(["--provider", "fake", "--n", "4"])

    assert exit_code == 0
    after = DEFAULT_RESULTS_PATH.read_bytes() if DEFAULT_RESULTS_PATH.exists() else None
    assert after == before
    assert "| 12 |" in (tmp_path / "atlas-demo12" / "results.md").read_text()
