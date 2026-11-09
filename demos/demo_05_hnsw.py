"""Phase 05 demo: brute force vs HNSW on `bench_vectors` and hybrid recall@5 (RAG-11/13, DOD-02).

1. **Benchmark.** Empties `bench_vectors` (migration 0006, `vector(256)`, no index), generates
   `--size` synthetic unit vectors (`demos.bench.generate_vectors`, CT-37, default 1M) and loads
   them with `COPY` (`load_bench_table`). Builds a cosine HNSW index on `bench_vectors` with
   `--m` / `--ef-construction` (default: `ATLAS_HNSW_M` / `ATLAS_HNSW_EF_CONSTRUCTION`) and
   reports the build time. The exact top 5 of `--queries` synthetic queries (`exact_top_k`) is
   the ground truth for both methods, timed per query with `measure`:
   - brute force: NumPy in memory, one matrix-vector product (as in demo 04);
   - HNSW: `ORDER BY embedding <=> :q LIMIT 5` on `bench_vectors` through a synchronous
     SQLAlchemy connection (psycopg 3, same URL as the app), inside one transaction with
     `SET LOCAL hnsw.ef_search = --ef-search` (default `ATLAS_HNSW_EF_SEARCH`) and
     `SET LOCAL enable_seqscan = off` (on small tables the planner would otherwise pick an exact
     sort). The latency includes the round trip to PostgreSQL. `EXPLAIN` checks that the index
     is really used; the row and the output say so.
   Whatever happens, the bench index is dropped and the table truncated at the end, so 1M
   vectors (~1 GB plus the index) never stay in the database. The production index
   `ix_chunks_embedding_hnsw` is never touched.
2. **Recall@5 on synonym questions.** Ingests `--corpus` with the embedder of the provider (as
   demo 04, same stored-vector check) and runs every question of `--questions` through the
   full-text search (`search_fulltext`), the vector search with HNSW (`search_hnsw`, CT-35) and
   the hybrid search (`search_hybrid`, RRF of both with `ATLAS_RRF_K`, CT-36). A question is a
   hit when one of its top 5 chunks belongs to `expected_doc_key`. On a corpus of a few hundred
   chunks the planner may answer the vector query with an exact sort instead of the index: the
   ranking is the same, only the latency differs (`bench_vectors` has no such filter).
3. **Results.** Records the `| 05 |` row of `docs/results.md`: Before = brute force
   (p50/p95/recall@5), After = HNSW (p50/p95/recall@5), Notes = hybrid recall@5 next to
   full-text and vector HNSW, size, dimension, `m`/`ef_construction`/`ef_search`, build time
   and provider.

`--provider fake` runs offline with `FakeEmbeddingClient` and the `FAKE_SYNONYMS` table of demo
04: its recall is a demonstration, not a measurement. A fake run writes its results under
`<tmp>/atlas-demo05/` unless `--results` is given, so it never replaces the real `| 05 |` row.
The benchmark itself never calls an embedder, so its numbers are the same with both providers.

Warning: everything runs in the configured database (`ATLAS_DATABASE_URL`). A fake run against
the main database stores fake vectors in `chunks.embedding` (see demo 04); point it to a
throwaway database, or restore the vectors with `python -m ingest run --reembed`. Building the
HNSW index of 1M vectors is much faster when the graph fits in `maintenance_work_mem`: pass
`--maintenance-work-mem 2GB` (set for the build transaction only) if the server has the memory.

Usage:
    uv run python -m demos.demo_05_hnsw --provider fake --size 2000 --results /tmp/results.md
    uv run python -m demos.demo_05_hnsw --provider real   # 1M vectors, writes docs/results.md
"""

import argparse
import asyncio
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sqlalchemy import Connection, Engine, create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.config import Settings, get_settings
from api.db.engine import dispose_engine, get_sessionmaker
from api.rag.embeddings import EmbeddingClient
from api.rag.fulltext import ScoredChunk, search_fulltext
from api.rag.hybrid import search_hybrid
from api.rag.models import Chunk, Document
from api.rag.vector_hnsw import search_hnsw
from demos.bench import (
    BenchResult,
    SearchFn,
    exact_top_k,
    generate_vectors,
    load_bench_table,
    measure,
)
from demos.common import DEFAULT_RESULTS_PATH, demo_parser, record_result
from demos.demo_04_embeddings import (
    CORPUS_DIR,
    LANGUAGES,
    QUESTIONS_PATH,
    EmbeddingMismatch,
    SynonymQuestion,
    bruteforce_search,
    build_demo_embedder,
    load_questions,
    prepare_index,
    size_label,
)
from ingest.report import IngestReport

PHASE = 5
METRIC = "HNSW vs brute force (p50/p95, recall@5); hybrid recall@5"
TOP_K = 5
BENCH_DIM = 256  # `bench_vectors.embedding` is `vector(256)` (migration 0006, user decision)
BENCH_INDEX = "ix_bench_vectors_embedding_hnsw"
DEFAULT_SIZE = 1_000_000
DEFAULT_QUERIES = 50
DEFAULT_SEED = 5
# Method of the results row -> short label of the per-question output.
METHODS = {"full-text": "fulltext", "vector HNSW": "hnsw", "hybrid": "hybrid"}


# --- 1. Benchmark -----------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkResult:
    size: int
    queries: int
    m: int
    ef_construction: int
    ef_search: int
    build_seconds: float
    bruteforce: BenchResult
    hnsw: BenchResult
    index_scan: bool


def _vector_literal(vector: np.ndarray) -> str:
    """pgvector text input, `[x,y,...]`, with the shortest round-trip repr of each value."""
    return "[" + ",".join(repr(value) for value in vector.tolist()) + "]"


async def load_bench(database_url: str, vectors: np.ndarray) -> None:
    """Empty `bench_vectors` (ids restart at 1, so row index = id - 1) and `COPY` `vectors`."""
    engine = create_async_engine(database_url)
    try:
        async with async_sessionmaker(engine)() as session, session.begin():
            await session.execute(text("TRUNCATE bench_vectors RESTART IDENTITY"))
            await load_bench_table(session, vectors)
    finally:
        await engine.dispose()


def build_bench_index(
    engine: Engine, m: int, ef_construction: int, maintenance_work_mem: str | None = None
) -> float:
    """Create the cosine HNSW index of `bench_vectors`; returns the build time in seconds."""
    with engine.begin() as connection:
        if maintenance_work_mem:
            connection.execute(
                text("SELECT set_config('maintenance_work_mem', :value, true)"),
                {"value": maintenance_work_mem},
            )
        started = time.perf_counter()
        connection.execute(
            text(
                f"CREATE INDEX {BENCH_INDEX} ON bench_vectors "
                f"USING hnsw (embedding vector_cosine_ops) "
                f"WITH (m = {int(m)}, ef_construction = {int(ef_construction)})"
            )
        )
        elapsed = time.perf_counter() - started
        connection.execute(text("ANALYZE bench_vectors"))
    return elapsed


_HNSW_SQL = text("SELECT id FROM bench_vectors ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k")


def hnsw_search(connection: Connection) -> SearchFn:
    """Top-k row indexes (`id - 1`) of `bench_vectors` by cosine distance, via the index."""

    def search(query: np.ndarray, k: int) -> list[int]:
        rows = connection.execute(_HNSW_SQL, {"q": _vector_literal(query), "k": k}).scalars()
        return [int(row_id) - 1 for row_id in rows]

    return search


def uses_bench_index(connection: Connection, query: np.ndarray, k: int = TOP_K) -> bool:
    """Whether the plan of the benchmark query scans `BENCH_INDEX`."""
    plan = connection.execute(
        text(f"EXPLAIN {_HNSW_SQL.text}"), {"q": _vector_literal(query), "k": k}
    ).scalars()
    return any(BENCH_INDEX in line for line in plan)


def measure_hnsw(
    engine: Engine,
    queries: np.ndarray,
    truth: Sequence[Sequence[int]],
    ef_search: int,
    k: int = TOP_K,
) -> tuple[BenchResult, bool]:
    """p50/p95 and recall@5 of the HNSW queries, all in one transaction with `ef_search`.

    The transaction also sets `enable_seqscan = off`: on a small table, or with a large
    `ef_search`, the planner prefers a sequential scan with an exact sort, which would time
    the wrong method. Both settings are `SET LOCAL` and end with the transaction.
    """
    with engine.connect() as connection, connection.begin():
        connection.execute(
            text(
                "SELECT set_config('hnsw.ef_search', :value, true), "
                "set_config('enable_seqscan', 'off', true)"
            ),
            {"value": str(ef_search)},
        )
        index_scan = uses_bench_index(connection, queries[0], k)
        return measure(hnsw_search(connection), queries, k, truth=truth), index_scan


def drop_bench(engine: Engine) -> None:
    """Drop the bench index and empty `bench_vectors`; never touches the chunks index."""
    with engine.begin() as connection:
        connection.execute(text(f"DROP INDEX IF EXISTS {BENCH_INDEX}"))
        connection.execute(text("TRUNCATE bench_vectors RESTART IDENTITY"))


@contextmanager
def _sync_engine(database_url: str) -> Iterator[Engine]:
    engine = create_engine(database_url)
    try:
        yield engine
    finally:
        engine.dispose()


def run_benchmark(
    database_url: str,
    *,
    size: int,
    queries: int,
    seed: int,
    m: int,
    ef_construction: int,
    ef_search: int,
    maintenance_work_mem: str | None = None,
    k: int = TOP_K,
) -> BenchmarkResult:
    """Brute force vs HNSW on `size` synthetic vectors; `bench_vectors` ends empty, unindexed."""
    vectors = generate_vectors(size, BENCH_DIM, seed)
    query_vectors = generate_vectors(queries, BENCH_DIM, seed + 1)
    truth = exact_top_k(vectors, query_vectors, k)
    bruteforce = measure(bruteforce_search(vectors), query_vectors, k, truth=truth)
    with _sync_engine(database_url) as engine:
        try:
            asyncio.run(load_bench(database_url, vectors))
            del vectors
            build_seconds = build_bench_index(engine, m, ef_construction, maintenance_work_mem)
            hnsw, index_scan = measure_hnsw(engine, query_vectors, truth, ef_search, k)
        finally:
            drop_bench(engine)
    return BenchmarkResult(
        size=size,
        queries=queries,
        m=m,
        ef_construction=ef_construction,
        ef_search=ef_search,
        build_seconds=build_seconds,
        bruteforce=bruteforce,
        hnsw=hnsw,
        index_scan=index_scan,
    )


def _bench_cell(method: str, result: BenchResult) -> str:
    return (
        f"{method}: p50 {result.p50_ms:.2f} ms / p95 {result.p95_ms:.2f} ms / "
        f"recall@{TOP_K} {result.recall_at_5:.2f}"
    )


# --- 2. Recall@5 on synonym questions ---------------------------------------------------------


@dataclass(frozen=True)
class QuestionResult:
    question: SynonymQuestion
    doc_keys: dict[str, list[str]]  # method -> top-k document keys, in ranking order

    def hit(self, method: str) -> bool:
        return self.question.expected_doc_key in self.doc_keys[method]


async def _doc_keys(session: AsyncSession, scored: Sequence[ScoredChunk]) -> list[str]:
    ids = [item.chunk_id for item in scored]
    if not ids:
        return []
    rows = await session.execute(
        select(Chunk.id, Document.doc_key)
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.id.in_(ids))
    )
    by_id: dict[int, str] = {chunk_id: doc_key for chunk_id, doc_key in rows}
    return [by_id[chunk_id] for chunk_id in ids if chunk_id in by_id]


async def evaluate_recall(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: EmbeddingClient,
    questions: Sequence[SynonymQuestion],
    k: int = TOP_K,
) -> list[QuestionResult]:
    """Top-`k` documents of each question by full-text, vector HNSW and hybrid search."""
    vectors = await embedder.embed([q.question for q in questions]) if questions else []
    results: list[QuestionResult] = []
    for question, vector in zip(questions, vectors, strict=True):
        # One session (one transaction) per question: `search_hnsw` uses `SET LOCAL`.
        async with session_factory() as session:
            text_hits = await search_fulltext(session, question.question, question.language, k)
            vector_hits = await search_hnsw(session, vector, question.language, k)
            hybrid_hits = await search_hybrid(
                session, question.question, vector, question.language, k
            )
            keys = {
                "full-text": await _doc_keys(session, text_hits),
                "vector HNSW": await _doc_keys(session, vector_hits),
                "hybrid": await _doc_keys(session, hybrid_hits),
            }
        results.append(QuestionResult(question=question, doc_keys=keys))
    return results


def recall_cell(method: str, results: Sequence[QuestionResult]) -> str:
    total = sum(r.hit(method) for r in results)
    recall = total / len(results) if results else 0.0
    per_language = ", ".join(
        f"{lang} {sum(r.hit(method) for r in chosen)}/{len(chosen)}"
        for lang in LANGUAGES
        if (chosen := [r for r in results if r.question.language == lang])
    )
    return f"{method} recall@{TOP_K} = {recall:.2f} ({total}/{len(results)}; {per_language})"


async def _run_recall(
    embedder: EmbeddingClient,
    corpus_dir: Path,
    questions: Sequence[SynonymQuestion],
    reembed: bool,
) -> tuple[IngestReport, list[QuestionResult]]:
    try:
        factory = get_sessionmaker()
        report = await prepare_index(factory, embedder, corpus_dir, reembed=reembed)
        return report, await evaluate_recall(factory, embedder, questions)
    finally:
        # The engine's pooled connections belong to this event loop; close them here.
        await dispose_engine()


# --- Orchestration ----------------------------------------------------------------------------


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}") from None
    if number <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = demo_parser("Phase 05: brute force vs HNSW (p50/p95, recall@5) and hybrid recall@5")
    parser.add_argument("--size", type=_positive, default=DEFAULT_SIZE)
    parser.add_argument("--queries", type=_positive, default=DEFAULT_QUERIES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--m", type=_positive, default=None, help="default: ATLAS_HNSW_M")
    parser.add_argument(
        "--ef-construction",
        dest="ef_construction",
        type=_positive,
        default=None,
        help="default: ATLAS_HNSW_EF_CONSTRUCTION",
    )
    parser.add_argument(
        "--ef-search",
        dest="ef_search",
        type=_positive,
        default=None,
        help="default: ATLAS_HNSW_EF_SEARCH",
    )
    parser.add_argument(
        "--maintenance-work-mem",
        dest="maintenance_work_mem",
        default=None,
        help="maintenance_work_mem for the index build only, e.g. 2GB (default: server value)",
    )
    parser.add_argument("--questions", type=Path, default=QUESTIONS_PATH)
    parser.add_argument("--corpus", type=Path, default=CORPUS_DIR)
    parser.add_argument(
        "--reembed", action="store_true", help="replace stored vectors of another embedder"
    )
    parser.set_defaults(results=None)
    return parser


def _embedder_note(provider: str, settings: Settings) -> str:
    if provider == "fake":
        return f"embedder=fake hash + synonym table, dim={settings.embedding_dimensions}"
    return f"embedder={settings.embedding_model}, dim={settings.embedding_dimensions}"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.results is None:
        # A fake run never replaces the real `| 05 |` row (same rule as demos 03 and 04).
        fake_results = Path(tempfile.gettempdir()) / "atlas-demo05" / "results.md"
        args.results = fake_results if args.provider == "fake" else DEFAULT_RESULTS_PATH

    settings = get_settings()
    m = args.m or settings.hnsw_m
    ef_construction = args.ef_construction or settings.hnsw_ef_construction
    ef_search = args.ef_search or settings.hnsw_ef_search
    questions = load_questions(args.questions)
    embedder = build_demo_embedder(args.provider, settings)
    try:
        report, recall = asyncio.run(_run_recall(embedder, args.corpus, questions, args.reembed))
    except EmbeddingMismatch:
        print(
            "error: the stored embeddings come from another embedder or model. Rerun "
            "`python -m demos.demo_05_hnsw --reembed` to replace them with this embedder's "
            "vectors, or restore the configured embedder's vectors with "
            "`python -m ingest run --reembed`.",
            file=sys.stderr,
        )
        return 2
    failed = [d for d in report.documents if d.status == "failed"]
    print(f"Ingested {len(report.documents) - len(failed)} documents ({len(failed)} failed)")
    for result in recall:
        marks = " ".join(
            f"{label}={'hit' if result.hit(method) else 'miss'}"
            for method, label in METHODS.items()
        )
        print(
            f"[{result.question.language}] {result.question.id} {result.question.question!r} "
            f"-> {result.question.expected_doc_key}: {marks}"
        )
    recall_cells = {method: recall_cell(method, recall) for method in METHODS}
    for cell in recall_cells.values():
        print(cell)

    bench = run_benchmark(
        settings.database_url,
        size=args.size,
        queries=args.queries,
        seed=args.seed,
        m=m,
        ef_construction=ef_construction,
        ef_search=ef_search,
        maintenance_work_mem=args.maintenance_work_mem,
    )
    before = _bench_cell("brute force (NumPy, in memory)", bench.bruteforce)
    after = _bench_cell("HNSW (pgvector, SQL)", bench.hnsw)
    print(f"[bench] n={bench.size} dim={BENCH_DIM} index build {bench.build_seconds:.2f} s")
    print(f"[bench] {before}")
    print(f"[bench] {after} (index scan: {'yes' if bench.index_scan else 'NO'})")

    counts = {lang: sum(q.language == lang for q in questions) for lang in LANGUAGES}
    record_result(
        PHASE,
        METRIC,
        before,
        after,
        notes=(
            f"{recall_cells['hybrid']} vs {recall_cells['full-text']} and "
            f"{recall_cells['vector HNSW']} on {len(questions)} synonym questions "
            f"({counts['pt']} pt + {counts['en']} en, RRF rrf_k={settings.rrf_k}); "
            f"bench size={size_label(bench.size)}, dim={BENCH_DIM}, {bench.queries} queries, "
            f"m={bench.m}, ef_construction={bench.ef_construction}, "
            f"ef_search={bench.ef_search}, index build {bench.build_seconds:.2f} s, "
            f"index scan {'yes' if bench.index_scan else 'no'}; "
            f"provider={args.provider}; {_embedder_note(args.provider, settings)}"
        ),
        results_path=args.results,
    )
    print(f"Recorded phase {PHASE:02d} in {args.results}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
