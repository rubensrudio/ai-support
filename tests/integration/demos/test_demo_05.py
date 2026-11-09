"""Phase 05 HNSW demo: brute force vs HNSW on `bench_vectors` and hybrid recall@5
(TASK-063, RAG-11, RAG-13, DOD-02)."""

import re
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete, text

from api.config import get_settings
from api.db.engine import dispose_engine, session_scope
from api.rag.models import CHUNKS_HNSW_INDEX, Document
from demos import demo_05_hnsw
from demos.common import DEFAULT_RESULTS_PATH
from demos.demo_04_embeddings import CORPUS_DIR
from demos.demo_05_hnsw import (
    BENCH_DIM,
    BENCH_INDEX,
    DEFAULT_QUERIES,
    DEFAULT_SIZE,
    build_parser,
    main,
    run_benchmark,
)
from ingest.pipeline import load_manifest

pytestmark = pytest.mark.integration

_MS = r"\d+\.\d\d ms"
_RECALL = r"\d\.\d\d"


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "fake")
    monkeypatch.setenv("ATLAS_EMBEDDING_PROVIDER", "fake")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def corpus_cleanup(migrated_database: str) -> AsyncIterator[None]:
    """Removes the ingested corpus afterwards, so other tests see an empty knowledge base."""
    await dispose_engine()
    yield
    await dispose_engine()
    keys = [entry.doc_key for entry in load_manifest(CORPUS_DIR)]
    async with session_scope() as session:
        await session.execute(delete(Document).where(Document.doc_key.in_(keys)))
    await dispose_engine()


def _bench_state(database_url: str) -> tuple[int, list[str], str | None]:
    """Rows of `bench_vectors`, its index names and the production chunks HNSW `indexdef`."""
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT count(*) FROM bench_vectors")).scalar_one()
            indexes = connection.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'bench_vectors'")
            ).scalars()
            names = [name for name in indexes if not name.endswith("_pkey")]
            production = connection.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
                {"name": CHUNKS_HNSW_INDEX},
            ).scalar_one_or_none()
    finally:
        engine.dispose()
    return int(rows), names, production


def test_parser_defaults_follow_the_briefing_and_the_settings() -> None:
    settings = get_settings()

    args = build_parser().parse_args([])

    assert args.size == DEFAULT_SIZE == 1_000_000
    assert args.queries == DEFAULT_QUERIES == 50
    assert (args.m, args.ef_construction, args.ef_search) == (None, None, None)
    assert args.provider == "real"
    custom = build_parser().parse_args(["--m", "8", "--ef-construction", "32", "--ef-search", "20"])
    assert (custom.m, custom.ef_construction, custom.ef_search) == (8, 32, 20)
    assert settings.hnsw_m > 0 and settings.hnsw_ef_search > 0
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--size", "0"])


def test_benchmark_measures_both_methods_and_leaves_bench_vectors_empty(
    migrated_database: str,
) -> None:
    production_before = _bench_state(migrated_database)[2]

    result = run_benchmark(
        migrated_database, size=2000, queries=10, seed=5, m=16, ef_construction=64, ef_search=200
    )

    assert BENCH_DIM == 256
    assert result.size == 2000 and result.queries == 10
    assert result.bruteforce.recall_at_5 == 1.0
    assert result.hnsw.recall_at_5 >= 0.9  # ef_search=200 on 2k vectors: near exact
    for bench in (result.bruteforce, result.hnsw):
        assert 0.0 <= bench.p50_ms <= bench.p95_ms
    assert result.build_seconds > 0.0
    assert result.index_scan, "the HNSW queries must use the bench_vectors index"
    rows, indexes, production_after = _bench_state(migrated_database)
    assert (rows, indexes) == (0, [])
    assert production_after == production_before is not None


def test_benchmark_cleans_up_when_the_measurement_fails(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_measure(*_: object, **__: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(demo_05_hnsw, "measure_hnsw", broken_measure)

    with pytest.raises(RuntimeError, match="boom"):
        run_benchmark(
            migrated_database, size=300, queries=3, seed=1, m=8, ef_construction=32, ef_search=20
        )

    rows, indexes, _ = _bench_state(migrated_database)
    assert (rows, indexes) == (0, [])
    assert BENCH_INDEX not in indexes


@pytest.mark.usefixtures("corpus_cleanup")
def test_fake_run_records_both_methods_and_the_hybrid_recall(
    migrated_database: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results.md"

    exit_code = main(
        [
            "--provider",
            "fake",
            "--size",
            "2000",
            "--queries",
            "10",
            "--results",
            str(results),
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    rows = [line for line in results.read_text().splitlines() if line.startswith("| 05 |")]
    assert len(rows) == 1
    _, _, metric, before, after, notes, _ = rows[0].split("|")
    assert "HNSW vs brute force (p50/p95, recall@5); hybrid recall@5" in metric
    assert re.search(rf"brute force .*p50 {_MS} / p95 {_MS} / recall@5 {_RECALL}", before)
    assert re.search(rf"HNSW .*p50 {_MS} / p95 {_MS} / recall@5 {_RECALL}", after)
    hybrid = re.search(rf"hybrid recall@5 = ({_RECALL}) \((\d+)/16;", notes)
    assert hybrid, notes
    assert float(hybrid.group(1)) >= 0.75
    assert re.search(rf"full-text recall@5 = {_RECALL}", notes)
    assert re.search(rf"vector HNSW recall@5 = {_RECALL}", notes)
    for fragment in ("size=2k", "dim=256", "m=16", "ef_construction=64", "ef_search=40"):
        assert fragment in notes, fragment
    assert re.search(r"index build \d+\.\d+ s", notes)
    assert "provider=fake" in notes
    # RAG-13: the hybrid search finds "end subscription" for "cancel plan" in both languages.
    assert re.search(r"\[pt\] syn-pt-01 .*fulltext=miss .*hybrid=hit", output)
    assert re.search(r"\[en\] syn-en-01 .*fulltext=miss .*hybrid=hit", output)
    assert _bench_state(migrated_database)[:2] == (0, [])


@pytest.mark.usefixtures("corpus_cleanup")
def test_fake_run_without_results_writes_to_a_temp_dir_and_never_to_docs(
    migrated_database: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    before = DEFAULT_RESULTS_PATH.read_bytes() if DEFAULT_RESULTS_PATH.exists() else None

    exit_code = main(["--provider", "fake", "--size", "500", "--queries", "3", "--m", "8"])

    assert exit_code == 0
    after = DEFAULT_RESULTS_PATH.read_bytes() if DEFAULT_RESULTS_PATH.exists() else None
    assert after == before
    written = (tmp_path / "atlas-demo05" / "results.md").read_text()
    assert "| 05 |" in written and "m=8" in written
