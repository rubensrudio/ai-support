"""Phase 04 demo: embeddings vs full-text recall and brute-force latency (RAG-08/09/17, DOD-02).

1. **Recall@5 on synonym questions.** Ingests `corpus/` with the embedder of the provider
   (`ingest_corpus`, CT-28) and runs every question of `demos/data/synonym_questions.jsonl`
   (pt and en; the question words never appear in the expected document, e.g. "cancelar
   plano" for the `end-subscription` document that only says "encerrar assinatura") through
   the full-text search (`search_fulltext`, CT-29) and through the vector search (`embed` +
   `search_bruteforce`, CT-34/CT-35). A question is a hit for a method when one of its top 5
   chunks belongs to `expected_doc_key`. Recall@5 = hits / questions, per method.
2. **Brute-force latency.** For each size in `--sizes` (default 10k, 100k and 1M), builds a
   synthetic matrix of unit vectors (`demos.bench.generate_vectors`, CT-37), takes the exact
   top 5 of `--queries` synthetic queries as ground truth (`exact_top_k`) and times a NumPy
   brute-force search per query (`measure`): p50/p95 in ms. The synthetic dimension is
   `--dim` (default 256): 1M x 1536 float32 vectors would need about 6 GB of memory.
3. **Results.** Records the `| 04 |` row of `docs/results.md`: Before = full-text recall@5,
   After = embedding recall@5, Notes = provider, dimensions and latencies per size.

The embedder must be the one that produced the stored vectors: after ingesting, the demo
re-embeds a few stored chunks and compares them with what is in the database. When they differ
(the database was ingested by another provider or without the demo's synonym table), the demo
stops (exit 2), unless `--reembed` is given, which clears the corpus embeddings and ingests
again; `python -m ingest run --reembed` puts back the vectors of the configured embedder.
Ingesting also retires documents that are no longer in the manifest, as `python -m ingest run`.

`--provider fake` runs offline: `FakeEmbeddingClient` (hash of each word, CT-34) with
`FAKE_SYNONYMS`, a table that maps the question words to the corpus wording, so the fake shows
what a semantic embedder does. Its recall is a demonstration, not a measurement; the real
number comes from `--provider real` (`build_embedder`, OpenAI `text-embedding-3-small`). A fake
run writes its results under `<tmp>/atlas-demo04/` unless `--results` is given, so it never
replaces the real `| 04 |` row.

Warning: the index lives in the configured database (`ATLAS_DATABASE_URL`). A fake run against
the main database stores fake vectors in `chunks.embedding` (on an empty index, or with
`--reembed`), and the chat in a vector mode then searches them. Point `ATLAS_DATABASE_URL` to a
throwaway database for `--provider fake`; if the main one got fake vectors, restore them with
`python -m ingest run --reembed`.

Usage:
    uv run python -m demos.demo_04_embeddings --provider fake --sizes 1000,2000 \
        --results /tmp/results.md
    uv run python -m demos.demo_04_embeddings --provider real   # writes docs/results.md
"""

import argparse
import asyncio
import json
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.config import Settings, get_settings
from api.db.engine import dispose_engine, get_sessionmaker
from api.messages import Lang
from api.rag.embeddings import EmbeddingClient, FakeEmbeddingClient, build_embedder
from api.rag.fulltext import ScoredChunk, search_fulltext
from api.rag.models import Chunk, Document
from api.rag.vector_bruteforce import search_bruteforce
from demos.bench import SearchFn, exact_top_k, generate_vectors, measure
from demos.common import DEFAULT_RESULTS_PATH, ROOT_DIR, demo_parser, record_result
from ingest.pipeline import ingest_corpus, load_manifest
from ingest.report import IngestReport

PHASE = 4
METRIC = "recall@5 keyword vs embedding (synonyms); brute-force latency"
LANGUAGES: tuple[Lang, ...] = ("en", "pt")
QUESTIONS_PATH = ROOT_DIR / "demos" / "data" / "synonym_questions.jsonl"
CORPUS_DIR = ROOT_DIR / "corpus"
TOP_K = 5
DEFAULT_SIZES = "10000,100000,1000000"
DEFAULT_DIM = 256
DEFAULT_QUERIES = 20
DEFAULT_SEED = 4
# Stored vectors that are re-embedded per language to check they come from the same embedder.
CHECK_SAMPLE = 3
MIN_SAME_EMBEDDER_COSINE = 0.99

# Question word -> word the corpus uses. Only the fake embedder needs it: a real embedding
# model places these synonyms close together on its own.
FAKE_SYNONYMS: dict[str, str] = {
    # en
    "cancel": "end",
    "plan": "subscription",
    "freeze": "pause",
    "membership": "subscription",
    "weeks": "months",
    "parcel": "box",
    "broken": "damaged",
    "passcode": "password",
    "recover": "reset",
    "lost": "forgot",
    "entitled": "eligible",
    "reimbursement": "refund",
    "transaction": "payment",
    "rejected": "failed",
    "miles": "points",
    "vanish": "expire",
    "bonus": "loyalty",
    # pt
    "cancelar": "encerrar",
    "plano": "assinatura",
    "congelar": "pausar",
    "mensalidade": "assinatura",
    "semanas": "meses",
    "pacote": "caixa",
    "quebrado": "danificado",
    "palavra": "senha",
    "passe": "senha",
    "recupero": "redefinir",
    "ressarcido": "reembolsado",
    "direito": "elegível",
    "transação": "cobrança",
    "negada": "falhar",
    "milhas": "pontos",
    "somem": "expiram",
    "bônus": "fidelidade",
}


class EmbeddingMismatch(RuntimeError):
    """The stored vectors were produced by another embedder than the demo's."""


@dataclass(frozen=True)
class SynonymQuestion:
    id: str
    language: Lang
    question: str
    expected_doc_key: str


def load_questions(path: Path = QUESTIONS_PATH) -> list[SynonymQuestion]:
    """Questions of the JSONL file, one per non-blank line. Raises `ValueError` on bad data."""
    questions: list[SynonymQuestion] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        raw = json.loads(line)
        values = [raw.get(name) for name in ("id", "question", "expected_doc_key")]
        language = raw.get("language")
        if language not in LANGUAGES or not all(isinstance(v, str) and v.strip() for v in values):
            raise ValueError(f"{path}:{line_number}: invalid synonym question {raw!r}")
        questions.append(
            SynonymQuestion(
                id=raw["id"],
                language="pt" if language == "pt" else "en",
                question=raw["question"],
                expected_doc_key=raw["expected_doc_key"],
            )
        )
    return questions


def build_demo_embedder(provider: str, settings: Settings) -> EmbeddingClient:
    """`fake`: hash embedder with `FAKE_SYNONYMS`; `real`: the configured embedder."""
    if provider == "fake":
        return FakeEmbeddingClient(settings.embedding_dimensions, FAKE_SYNONYMS)
    return build_embedder(settings)


# --- 1. Index and recall@5 --------------------------------------------------------------------


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    left, right = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    norms = float(np.linalg.norm(left) * np.linalg.norm(right))
    return float(left @ right) / norms if norms else 0.0


async def embeddings_match(
    session_factory: async_sessionmaker[AsyncSession], embedder: EmbeddingClient
) -> bool:
    """Whether the first stored vectors of each language are what `embedder` produces now.

    Raises `EmbeddingMismatch` when no indexed chunk has an embedding at all.
    """
    sample: list[tuple[str, list[float]]] = []
    async with session_factory() as session:
        for language in LANGUAGES:
            rows = await session.execute(
                select(Chunk.content, Chunk.embedding)
                .join(Document, Document.id == Chunk.document_id)
                .where(
                    Chunk.language == language,
                    Document.status == "indexed",
                    Chunk.embedding.is_not(None),
                )
                .order_by(Chunk.id)
                .limit(CHECK_SAMPLE)
            )
            sample += [
                (content, [float(x) for x in vector])
                for content, vector in rows
                if vector is not None  # excluded by the query; narrows the type
            ]
    if not sample:
        raise EmbeddingMismatch("No indexed chunk has an embedding; check the ingestion report.")
    fresh = await embedder.embed([content for content, _ in sample])
    return all(
        _cosine(stored, new) >= MIN_SAME_EMBEDDER_COSINE
        for (_, stored), new in zip(sample, fresh, strict=True)
    )


async def _clear_corpus_embeddings(
    session_factory: async_sessionmaker[AsyncSession], corpus_dir: Path
) -> None:
    keys = [entry.doc_key for entry in load_manifest(corpus_dir)]
    async with session_factory() as session, session.begin():
        documents = select(Document.id).where(Document.doc_key.in_(keys))
        await session.execute(
            update(Chunk).where(Chunk.document_id.in_(documents)).values(embedding=None)
        )


async def prepare_index(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: EmbeddingClient,
    corpus_dir: Path,
    *,
    reembed: bool,
) -> IngestReport:
    """Ingest `corpus_dir` with `embedder` and make sure the stored vectors are its own.

    Ingestion only embeds chunks without a vector, so vectors of another embedder survive it.
    With `reembed` they are cleared and the corpus is ingested again; without it the mismatch
    raises `EmbeddingMismatch`.
    """
    if reembed:
        await _clear_corpus_embeddings(session_factory, corpus_dir)
    report = await ingest_corpus(corpus_dir, session_factory, embedder=embedder)
    if not await embeddings_match(session_factory, embedder):
        raise EmbeddingMismatch(
            "The stored embeddings come from another embedder or model. Rerun "
            "`python -m demos.demo_04_embeddings --reembed` to replace them with this "
            "embedder's vectors, or restore the configured embedder's vectors with "
            "`python -m ingest run --reembed`. The chat searches whatever vectors are stored."
        )
    return report


@dataclass(frozen=True)
class QuestionResult:
    question: SynonymQuestion
    fulltext_doc_keys: list[str]
    embedding_doc_keys: list[str]

    @property
    def fulltext_hit(self) -> bool:
        return self.question.expected_doc_key in self.fulltext_doc_keys

    @property
    def embedding_hit(self) -> bool:
        return self.question.expected_doc_key in self.embedding_doc_keys


async def _doc_keys(session: AsyncSession, scored: Sequence[ScoredChunk]) -> list[str]:
    """Document key of each scored chunk, in ranking order."""
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
    """Top-`k` documents of each question by full-text and by embedding + brute force."""
    vectors = await embedder.embed([q.question for q in questions]) if questions else []
    results: list[QuestionResult] = []
    async with session_factory() as session:
        for question, vector in zip(questions, vectors, strict=True):
            fulltext = await search_fulltext(session, question.question, question.language, k)
            vector_hits = await search_bruteforce(session, vector, question.language, k)
            results.append(
                QuestionResult(
                    question=question,
                    fulltext_doc_keys=await _doc_keys(session, fulltext),
                    embedding_doc_keys=await _doc_keys(session, vector_hits),
                )
            )
    return results


def _recall_cell(method: str, results: Sequence[QuestionResult]) -> str:
    def hits(selected: Sequence[QuestionResult]) -> int:
        return sum(r.fulltext_hit if method == "full-text" else r.embedding_hit for r in selected)

    total = hits(results)
    recall = total / len(results) if results else 0.0
    per_language = ", ".join(
        f"{lang} {hits(chosen)}/{len(chosen)}"
        for lang in LANGUAGES
        if (chosen := [r for r in results if r.question.language == lang])
    )
    return f"{method} recall@{TOP_K} = {recall:.2f} ({total}/{len(results)}; {per_language})"


# --- 2. Brute-force latency -------------------------------------------------------------------


@dataclass(frozen=True)
class LatencyResult:
    size: int
    p50_ms: float
    p95_ms: float
    recall_at_5: float


def bruteforce_search(vectors: np.ndarray) -> SearchFn:
    """Exact cosine top-k over unit-norm `vectors` with NumPy: one matrix-vector product."""

    def search(query: np.ndarray, k: int) -> list[int]:
        scores = vectors @ np.asarray(query, dtype=vectors.dtype)
        k = min(k, scores.size)
        candidates = np.argpartition(-scores, k - 1)[:k] if k < scores.size else np.arange(k)
        order = np.lexsort((candidates, -scores[candidates]))
        return [int(i) for i in candidates[order]]

    return search


def run_latency(
    sizes: Sequence[int], dim: int, queries: int, seed: int, k: int = TOP_K
) -> list[LatencyResult]:
    """p50/p95 of the NumPy brute force per size, with its recall@5 against the exact top-k."""
    query_vectors = generate_vectors(queries, dim, seed + 1)
    results: list[LatencyResult] = []
    for size in sizes:
        vectors = generate_vectors(size, dim, seed)
        truth = exact_top_k(vectors, query_vectors, k)
        bench = measure(bruteforce_search(vectors), query_vectors, k, truth=truth)
        results.append(LatencyResult(size, bench.p50_ms, bench.p95_ms, bench.recall_at_5))
        del vectors
    return results


def size_label(size: int) -> str:
    if size % 1_000_000 == 0:
        return f"{size // 1_000_000}M"
    if size % 1000 == 0:
        return f"{size // 1000}k"
    return str(size)


def _latency_note(results: Sequence[LatencyResult], dim: int, queries: int) -> str:
    sizes = ", ".join(
        f"{size_label(r.size)}: p50 {r.p50_ms:.2f} ms / p95 {r.p95_ms:.2f} ms" for r in results
    )
    recall = min((r.recall_at_5 for r in results), default=0.0)
    return (
        f"brute force (NumPy, in memory, synthetic dim={dim}, {queries} queries) {sizes}; "
        f"recall@5 vs exact {recall:.2f}"
    )


# --- Orchestration ----------------------------------------------------------------------------


def _parse_sizes(value: str) -> list[int]:
    try:
        sizes = [int(part) for part in value.split(",") if part.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid --sizes {value!r}") from None
    if not sizes or any(size <= 0 for size in sizes):
        raise argparse.ArgumentTypeError(f"--sizes needs positive integers, got {value!r}")
    return sizes


def _positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}")
    return number


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


def _embedder_note(provider: str, settings: Settings) -> str:
    if provider == "fake":
        return f"embedder=fake hash + synonym table, dim={settings.embedding_dimensions}"
    return f"embedder={settings.embedding_model}, dim={settings.embedding_dimensions}"


def main(argv: list[str] | None = None) -> int:
    parser = demo_parser("Phase 04: recall@5 full-text vs embedding and brute-force latency")
    parser.add_argument("--sizes", type=_parse_sizes, default=_parse_sizes(DEFAULT_SIZES))
    parser.add_argument("--dim", type=_positive, default=DEFAULT_DIM)
    parser.add_argument("--queries", type=_positive, default=DEFAULT_QUERIES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--questions", type=Path, default=QUESTIONS_PATH)
    parser.add_argument("--corpus", type=Path, default=CORPUS_DIR)
    parser.add_argument(
        "--reembed", action="store_true", help="replace stored vectors of another embedder"
    )
    parser.set_defaults(results=None)
    args = parser.parse_args(argv)
    if args.results is None:
        # A fake run never replaces the real `| 04 |` row (same rule as demo_03).
        fake_results = Path(tempfile.gettempdir()) / "atlas-demo04" / "results.md"
        args.results = fake_results if args.provider == "fake" else DEFAULT_RESULTS_PATH

    settings = get_settings()
    questions = load_questions(args.questions)
    embedder = build_demo_embedder(args.provider, settings)
    try:
        report, recall = asyncio.run(_run_recall(embedder, args.corpus, questions, args.reembed))
    except EmbeddingMismatch as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    failed = [d for d in report.documents if d.status == "failed"]
    print(f"Ingested {len(report.documents) - len(failed)} documents ({len(failed)} failed)")
    for result in recall:
        print(
            f"[{result.question.language}] {result.question.id} "
            f"{result.question.question!r} -> {result.question.expected_doc_key}: "
            f"fulltext={'hit' if result.fulltext_hit else 'miss'} "
            f"embedding={'hit' if result.embedding_hit else 'miss'}"
        )
    before, after = _recall_cell("full-text", recall), _recall_cell("embedding", recall)
    print(before)
    print(after)

    latencies = run_latency(args.sizes, args.dim, args.queries, args.seed)
    for latency in latencies:
        print(
            f"[brute force] n={latency.size} dim={args.dim} p50={latency.p50_ms:.2f} ms "
            f"p95={latency.p95_ms:.2f} ms recall@5={latency.recall_at_5:.2f}"
        )

    counts = {lang: sum(q.language == lang for q in questions) for lang in LANGUAGES}
    record_result(
        PHASE,
        METRIC,
        before,
        after,
        notes=(
            f"provider={args.provider}; {_embedder_note(args.provider, settings)}; "
            f"{len(questions)} synonym questions ({counts['pt']} pt + {counts['en']} en), "
            f"top-{TOP_K} chunks; {_latency_note(latencies, args.dim, args.queries)}"
        ),
        results_path=args.results,
    )
    print(f"Recorded phase {PHASE:02d} in {args.results}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
