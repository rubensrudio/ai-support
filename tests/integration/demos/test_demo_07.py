"""Phase 07 multi-step agent benchmark: 15 tasks, before (1 step) vs after (TASK-085, AGT-06,
AGT-07, DOD-02)."""

import re
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from api.config import get_settings
from api.db.engine import dispose_engine
from api.messages import Lang
from api.seed.personas import PERSONAS
from api.tools.catalog import TOOL_NAMES
from api.tools.models import OrderStatus
from demos.common import DEFAULT_RESULTS_PATH
from demos.demo_07_agent import (
    ORDER_REFS,
    TASKS_PATH,
    TaskResult,
    build_parser,
    load_tasks,
    main,
    resolve_refs,
    run_cell,
    summarize,
)

pytestmark = pytest.mark.integration

_WRITE_TOOLS = ("cancel_order", "issue_refund", "send_email")


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


def test_tasks_file_has_exactly_15_multistep_tasks_in_pt_and_en() -> None:
    tasks = load_tasks(TASKS_PATH)
    personas = {persona.username: persona.language for persona in PERSONAS}
    statuses = {status.value for status in OrderStatus}

    assert len(tasks) == 15
    assert {task.language for task in tasks} == {"pt", "en"}
    assert len({task.id for task in tasks}) == 15
    for task in tasks:
        assert personas[task.persona] == task.language, task.id
        assert len(task.expected_trajectory) >= 2, task.id
        assert set(task.expected_trajectory) <= set(TOOL_NAMES), task.id
        assert not re.search(r"#\d", task.message), f"{task.id} hard-codes an order number"
        outcome = task.expected_outcome
        assert outcome["final_status"] == "completed", task.id
        # Writes are audited only on success, in trajectory order.
        writes = [tool for tool in task.expected_trajectory if tool in _WRITE_TOOLS]
        assert outcome["audited"] == writes, task.id
        if outcome["order"] is not None:
            assert outcome["order_status"] in statuses, task.id
            assert resolve_refs(outcome["order"], dict.fromkeys(ORDER_REFS, 1)) == "1"
    # At least one 6-tool trajectory per language (AGT-06).
    long_ones = {task.language for task in tasks if len(task.expected_trajectory) == 6}
    assert long_ones == {"pt", "en"}


def test_resolve_refs_replaces_placeholders_and_rejects_unknown_ones() -> None:
    refs = {"paid_order_id": 41, "shipped_order_id": 42}

    assert resolve_refs("cancel #{paid_order_id} not #{shipped_order_id}", refs) == (
        "cancel #41 not #42"
    )
    with pytest.raises(KeyError):
        resolve_refs("#{lost_order_id}", refs)


def test_parser_has_the_common_flags() -> None:
    args = build_parser().parse_args([])

    assert args.provider == "real"
    assert args.results is None


def _result(task_id: str, language: Lang, trajectory: tuple[str, ...], *, ok: bool) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        language=language,
        expected_trajectory=("get_order", "send_email"),
        trajectory=trajectory,
        final_status="completed" if ok else "limit_reached",
        steps=3 if ok else 1,
        outcome_ok=ok,
        outcome_detail="ok" if ok else "final_status limit_reached",
    )


def test_summary_counts_trajectory_and_outcome_and_averages_steps() -> None:
    results = [
        _result("a", "en", ("get_order", "send_email"), ok=True),
        _result("b", "pt", ("get_order",), ok=False),
        # Right trajectory but wrong outcome is still a failure.
        TaskResult(
            task_id="c",
            language="pt",
            expected_trajectory=("get_order", "send_email"),
            trajectory=("get_order", "send_email"),
            final_status="completed",
            steps=2,
            outcome_ok=False,
            outcome_detail="audited []",
        ),
    ]

    summary = summarize(results)

    assert [r.succeeded for r in results] == [True, False, False]
    assert summary.successes == 1
    assert summary.total == 3
    assert summary.avg_steps == pytest.approx(2.0)
    assert run_cell(summary, max_steps=8) == (
        "max_steps=8: success 1/3 (33%), avg 2.0 steps (en 1/1, pt 0/2)"
    )


@pytest.mark.usefixtures("fresh_engine")
def test_fake_run_records_the_row_with_success_rate_and_average_steps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results.md"

    exit_code = main(["--provider", "fake", "--results", str(results)])

    output = capsys.readouterr().out
    assert exit_code == 0, output
    rows = [line for line in results.read_text().splitlines() if line.startswith("| 07 |")]
    assert len(rows) == 1
    _, _, metric, before, after, notes, _ = rows[0].split("|")
    assert "15" in metric and "multi-step" in metric.lower()
    # Before: one step is never enough for a multi-step task.
    assert "max_steps=1: success 0/15 (0%), avg 1.0 steps (en 0/8, pt 0/7)" in before
    # After: the scripted fake follows every expected trajectory (51 tools + 15 answers).
    assert "max_steps=8: success 15/15 (100%), avg 4.4 steps (en 8/8, pt 7/7)" in after
    assert "provider=fake" in notes
    assert "ATLAS_AGENT_MAX_STEPS=1" in notes
    assert output.count("[after] ") == 15 and output.count(": ok") >= 15
    # With one step the tools asked by that last call never run (step_limit).
    assert output.count(": limit_reached, 1 steps, -: FAIL") == 15
    assert "list_orders > get_order > check_refund_policy > cancel_order > issue_refund" in output


@pytest.mark.usefixtures("fresh_engine")
def test_fake_run_without_results_writes_to_a_temp_dir_and_never_to_docs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    before = DEFAULT_RESULTS_PATH.read_bytes() if DEFAULT_RESULTS_PATH.exists() else None

    exit_code = main(["--provider", "fake"])

    assert exit_code == 0
    after = DEFAULT_RESULTS_PATH.read_bytes() if DEFAULT_RESULTS_PATH.exists() else None
    assert after == before
    assert "| 07 |" in (tmp_path / "atlas-demo07" / "results.md").read_text()
