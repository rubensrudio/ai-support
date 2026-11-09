"""Phase 06 tool calling demo: cancel a paid order with confirmation and audit
(TASK-081, TOOL-11, DOD-02)."""

import asyncio
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy import select

from api.config import get_settings
from api.db.engine import dispose_engine, session_scope
from api.tools.models import AuditLog, Order, OrderStatus
from demos.common import DEFAULT_RESULTS_PATH
from demos.demo_06_tools import (
    MESSAGES,
    LanguageResult,
    after_cell,
    before_cell,
    build_parser,
    main,
    succeeded,
)

pytestmark = pytest.mark.integration


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


async def _current_cancellations() -> list[tuple[AuditLog, str]]:
    """`cancel_order` audit rows of orders that still exist, with the order status.

    The commerce seed deletes and recreates the personas' orders on every run, so audit rows
    of earlier runs point to orders that no longer exist and are left out."""
    try:
        async with session_scope() as session:
            audits = await session.scalars(
                select(AuditLog).where(AuditLog.tool == "cancel_order").order_by(AuditLog.id)
            )
            found: list[tuple[AuditLog, str]] = []
            for audit in audits:
                order = await session.get(Order, int(audit.arguments["order_id"]))
                if order is not None:
                    found.append((audit, order.status))
            return found
    finally:
        await dispose_engine()


def test_parser_has_the_common_flags_and_both_messages() -> None:
    args = build_parser().parse_args([])

    assert args.provider == "real"
    assert args.results is None
    assert MESSAGES["en"].format(order_id=7) == "cancel my order #7"
    assert MESSAGES["pt"].format(order_id=7) == "cancela meu pedido #7"


@pytest.mark.usefixtures("fresh_engine")
def test_fake_run_cancels_the_order_audits_it_and_records_the_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results.md"

    exit_code = main(["--provider", "fake", "--results", str(results)])

    output = capsys.readouterr().out
    assert exit_code == 0, output
    assert "[en] after 'cancel my order #" in output
    assert output.count(": awaiting_confirmation: ") == 2
    rows = [line for line in results.read_text().splitlines() if line.startswith("| 06 |")]
    assert len(rows) == 1
    _, _, metric, before, after, notes, _ = rows[0].split("|")
    assert "cancel" in metric.lower()
    # Before: tools off, text only, order unchanged; After: cancelled with an audit row.
    assert "en: text only, order paid" in before and "pt: text only, order paid" in before
    assert "en: order cancelled, audit success" in after
    assert "pt: order cancelled, audit success" in after
    assert "provider=fake" in notes
    assert "ATLAS_AGENT_TOOLS_ENABLED=false" in notes

    cancellations = asyncio.run(_current_cancellations())
    assert len(cancellations) == 2
    for audit, status in cancellations:
        assert audit.outcome == "success"
        assert audit.trace_id and f"trace_id={audit.trace_id}" in output
        assert status == OrderStatus.CANCELLED.value
        assert f"#{audit.arguments['order_id']}" in output
    assert len({audit.customer_id for audit, _ in cancellations}) == 2


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
    assert "| 06 |" in (tmp_path / "atlas-demo06" / "results.md").read_text()


def test_cells_report_a_model_that_did_not_call_the_tool() -> None:
    result = LanguageResult(
        language="en",
        order_id=7,
        before_status="completed",
        order_after_before="paid",
        after_status="completed",
        confirmation_target=None,
        final_status=None,
        order_after="paid",
        audit_outcome=None,
        trace_id=None,
    )

    assert before_cell([result]) == "en: text only, order paid (#7)"
    assert after_cell([result]) == "en: no confirmation requested, order paid"
    assert not succeeded(result)
