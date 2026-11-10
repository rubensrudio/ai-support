"""Phase 14 demo: from a negative-feedback trace to a passing eval case (TASK-139, OBS-05,
DOD-02)."""

import asyncio
import base64
import hashlib
import json
import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import delete, func, select

from api.config import get_settings
from api.db.engine import dispose_engine, session_scope
from api.feedback.models import Feedback
from api.observability.logging import bind_trace_id, new_trace_id, reset_trace_id
from api.observability.tracing import add_span_exporter, start_span
from api.rag.models import Document
from demos.demo_14_trace_to_eval import (
    CORPUS_DIR,
    DEMO_CASE,
    PHASE_14_DATASET,
    STALE_DOC_KEY,
    STEPS,
    fetch_langfuse_retrievals,
    main,
    retrievals_from_spans,
    upsert_case,
)
from evals.dataset import load_cases
from ingest.pipeline import load_manifest

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "fake")
    monkeypatch.setenv("ATLAS_EMBEDDING_PROVIDER", "fake")
    for name in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ATLAS_RETRIEVAL_MODE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def corpus_cleanup(migrated_database: str) -> AsyncIterator[None]:
    """Removes the ingested corpus afterwards, so other tests see an empty knowledge base."""
    await dispose_engine()
    yield
    await dispose_engine()
    keys = [entry.doc_key for entry in load_manifest(CORPUS_DIR)] + [STALE_DOC_KEY]
    async with session_scope() as session:
        await session.execute(delete(Document).where(Document.doc_key.in_(keys)))
    await dispose_engine()


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# --- trace reading ---------------------------------------------------------------------------


def test_retrievals_from_spans_keeps_only_the_retrieval_spans_of_the_trace() -> None:
    exporter = InMemorySpanExporter()
    detach = add_span_exporter(exporter)
    trace_ids: list[str] = []
    try:
        for doc_key in ("refund-policy", "plans-and-pricing"):
            trace_id = new_trace_id()
            trace_ids.append(trace_id)
            token = bind_trace_id(trace_id)
            try:
                with start_span("request"):
                    with start_span(
                        "retrieval",
                        **{
                            "atlas.retrieval.mode": "fulltext",
                            "atlas.retrieval.doc_keys": [doc_key],
                            "atlas.retrieval.chunk_ids": [7],
                            "atlas.retrieval.scores": [0.5],
                        },
                    ):
                        pass
            finally:
                reset_trace_id(token)
    finally:
        detach()

    [found] = retrievals_from_spans(exporter.get_finished_spans(), trace_ids[1])

    assert found.doc_keys == ["plans-and-pricing"]
    assert found.chunk_ids == [7]
    assert found.scores == [0.5]
    assert found.mode == "fulltext"


async def test_fetch_langfuse_retrievals_reads_the_retrieval_observations() -> None:
    trace_id = "ab" * 16
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": trace_id,
                "observations": [
                    {"name": "request", "metadata": {}},
                    {
                        "name": "retrieval",
                        "metadata": {
                            "attributes": {
                                "atlas.retrieval.mode": "fulltext",
                                "atlas.retrieval.doc_keys": json.dumps([STALE_DOC_KEY, "x"]),
                                "atlas.retrieval.chunk_ids": [11, 12],
                                "atlas.retrieval.scores": "[0.9, 0.1]",
                            }
                        },
                    },
                ],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://langfuse.test"
    ) as http:
        [found] = await fetch_langfuse_retrievals(http, trace_id, "pk", "sk")

    assert found.doc_keys == [STALE_DOC_KEY, "x"]
    assert found.chunk_ids == [11, 12]
    assert found.scores == [0.9, 0.1]
    assert seen[0].url.path == f"/api/public/traces/{trace_id}"
    expected = "Basic " + base64.b64encode(b"pk:sk").decode()
    assert seen[0].headers["Authorization"] == expected


async def test_fetch_langfuse_retrievals_returns_nothing_for_an_unknown_trace() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(404)),
        base_url="https://langfuse.test",
    ) as http:
        assert await fetch_langfuse_retrievals(http, "cd" * 16, "pk", "sk") == []


# --- dataset ---------------------------------------------------------------------------------


def test_upsert_case_appends_once_and_keeps_the_other_lines(tmp_path: Path) -> None:
    path = tmp_path / "phase_14.jsonl"
    other = '{"id": "other", "phase": 14}\n'
    path.write_text(other, encoding="utf-8")

    assert upsert_case(path, DEMO_CASE) is True
    assert upsert_case(path, DEMO_CASE) is False

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    assert lines[0] == other
    assert len(lines) == 2
    assert json.loads(lines[1])["id"] == DEMO_CASE.id


def test_the_demo_case_is_the_versioned_phase_14_case() -> None:
    versioned = {case.id: case for case in load_cases([PHASE_14_DATASET])}

    assert versioned[DEMO_CASE.id] == DEMO_CASE


# --- end to end ------------------------------------------------------------------------------


async def _down_ratings() -> int:
    async with session_scope() as session:
        count = await session.scalar(
            select(func.count()).select_from(Feedback).where(Feedback.rating == "down")
        )
    await dispose_engine()
    return int(count or 0)


def test_fake_run_prints_the_five_steps_and_the_new_case_passes(
    corpus_cleanup: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus_before = _tree_digest(CORPUS_DIR)
    dataset_before = PHASE_14_DATASET.read_bytes()
    ratings_before = asyncio.run(_down_ratings())
    dataset_out = tmp_path / "phase_14.jsonl"
    results = tmp_path / "results.md"

    code = main(
        ["--provider", "fake", "--dataset-out", str(dataset_out), "--results", str(results)]
    )

    printed = capsys.readouterr().out
    assert code == 0, printed
    positions = [printed.index(f"[{number}/5] {title}") for number, title in enumerate(STEPS, 1)]
    assert positions == sorted(positions)
    assert f"culprit: {STALE_DOC_KEY}" in printed
    assert "before the fix: FAIL" in printed
    assert "after the fix: PASS" in printed
    assert "feedback: 204" in printed
    trace_id = re.search(r"trace_id: ([0-9a-f]{32})", printed)
    assert trace_id, printed
    assert f"local recorder, no --langfuse): [('{trace_id.group(1)}', -1)]" in printed
    assert "score ids: ['" in printed

    [case] = [case for case in load_cases([dataset_out]) if case.id == DEMO_CASE.id]
    assert case == DEMO_CASE
    assert "| 14 |" in results.read_text(encoding="utf-8")
    assert asyncio.run(_down_ratings()) == ratings_before + 1

    assert _tree_digest(CORPUS_DIR) == corpus_before
    assert PHASE_14_DATASET.read_bytes() == dataset_before
