"""Phase 04 embeddings demo: recall@5 full-text vs embedding and brute-force latency
(TASK-058, RAG-08, RAG-09, RAG-17, DOD-02)."""

import re
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy import delete

from api.config import get_settings
from api.db.engine import dispose_engine, get_sessionmaker, session_scope
from api.rag.embeddings import FakeEmbeddingClient
from api.rag.models import Document
from demos.bench import exact_top_k, generate_vectors, measure
from demos.common import DEFAULT_RESULTS_PATH
from demos.demo_04_embeddings import (
    CORPUS_DIR,
    FAKE_SYNONYMS,
    QUESTIONS_PATH,
    EmbeddingMismatch,
    bruteforce_search,
    build_demo_embedder,
    embeddings_match,
    load_questions,
    main,
    prepare_index,
    run_latency,
    size_label,
)
from ingest.pipeline import load_manifest

pytestmark = pytest.mark.integration

_WORD = re.compile(r"\w+")
# Function words a question may share with its document; every other word must be absent.
STOP_WORDS = {
    "en": {"a", "am", "an", "can", "do", "for", "how", "i", "it", "my", "the", "to", "what"},
    "pt": {"a", "como", "de", "e", "o", "para", "por", "que", "ser"},
}


class ReversedEmbedder:
    """Another embedder of the same dimension: the fake vectors in reverse order."""

    def __init__(self, dim: int) -> None:
        self.fake = FakeEmbeddingClient(dim)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.fake.vector(text)[::-1] for text in texts]


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


def _doc_words(doc_key: str, language: str) -> set[str]:
    return set(_WORD.findall((CORPUS_DIR / language / f"{doc_key}.md").read_text().lower()))


def test_synonym_questions_use_words_the_expected_document_never_uses() -> None:
    questions = load_questions(QUESTIONS_PATH)
    manifest = {entry.doc_key for entry in load_manifest(CORPUS_DIR)}

    assert len({q.id for q in questions}) == len(questions)
    for language in ("pt", "en"):
        assert sum(q.language == language for q in questions) >= 6, language
    for question in questions:
        assert question.expected_doc_key in manifest, question.id
        words = set(_WORD.findall(question.question.lower()))
        shared = words & _doc_words(question.expected_doc_key, question.language)
        assert shared <= STOP_WORDS[question.language], (question.id, shared)
        assert words & set(FAKE_SYNONYMS), f"{question.id} has no synonym for the fake embedder"
    assert {(q.language, q.question) for q in questions} >= {
        ("pt", "cancelar plano"),
        ("en", "cancel plan"),
    }


def test_load_questions_rejects_a_line_without_expected_doc(tmp_path: Path) -> None:
    path = tmp_path / "questions.jsonl"
    path.write_text('{"id": "x", "language": "en", "question": "Hi?"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="questions.jsonl:1"):
        load_questions(path)


def test_bruteforce_search_returns_the_exact_top_k() -> None:
    vectors = generate_vectors(500, 16, seed=1)
    queries = generate_vectors(10, 16, seed=2)
    truth = exact_top_k(vectors, queries, 5)

    result = measure(bruteforce_search(vectors), queries, 5, truth=truth)

    assert result.recall_at_5 == 1.0
    assert list(bruteforce_search(vectors)(queries[0], 5)) == truth[0]


def test_run_latency_reports_p50_and_p95_per_size() -> None:
    results = run_latency([300, 600], dim=8, queries=5, seed=1)

    assert [r.size for r in results] == [300, 600]
    for result in results:
        assert 0.0 <= result.p50_ms <= result.p95_ms
        assert result.recall_at_5 == 1.0


def test_size_label_uses_k_and_m() -> None:
    assert [size_label(n) for n in (1000, 10_000, 100_000, 1_000_000, 1500)] == [
        "1k",
        "10k",
        "100k",
        "1M",
        "1500",
    ]


def _recall(cell: str, method: str) -> float:
    match = re.search(rf"{method} recall@5 = (\d\.\d\d) \((\d+)/(\d+);", cell)
    assert match, f"missing {method} recall in {cell!r}"
    return float(match.group(1))


@pytest.mark.usefixtures("corpus_cleanup")
def test_fake_run_records_both_recalls_and_the_latencies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results.md"

    exit_code = main(["--provider", "fake", "--sizes", "1000,2000", "--results", str(results)])

    output = capsys.readouterr().out
    assert exit_code == 0
    rows = [line for line in results.read_text().splitlines() if line.startswith("| 04 |")]
    assert len(rows) == 1
    _, _, metric, before, after, notes, _ = rows[0].split("|")
    assert "recall@5 keyword vs embedding (synonyms); brute-force latency" in metric
    fulltext = _recall(before, "full-text")
    embedding = _recall(after, "embedding")
    assert embedding > fulltext
    assert embedding >= 0.75
    for size in ("1k", "2k"):
        assert re.search(rf"{size}: p50 \d+\.\d+ ms / p95 \d+\.\d+ ms", notes), notes
    assert "provider=fake" in notes and "dim=256" in notes
    # RAG-08 / RAG-17: the vector search finds "end subscription" for "cancel plan".
    assert re.search(r"\[pt\] syn-pt-01 .*fulltext=miss embedding=hit", output)
    assert re.search(r"\[en\] syn-en-01 .*fulltext=miss embedding=hit", output)


@pytest.mark.usefixtures("corpus_cleanup")
def test_fake_run_without_results_writes_to_a_temp_dir_and_never_to_docs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    before = DEFAULT_RESULTS_PATH.read_bytes() if DEFAULT_RESULTS_PATH.exists() else None

    exit_code = main(["--provider", "fake", "--sizes", "500"])

    assert exit_code == 0
    after = DEFAULT_RESULTS_PATH.read_bytes() if DEFAULT_RESULTS_PATH.exists() else None
    assert after == before
    written = tmp_path / "atlas-demo04" / "results.md"
    assert "| 04 |" in written.read_text()


@pytest.mark.usefixtures("corpus_cleanup")
async def test_vectors_of_another_embedder_are_refused_unless_reembedding() -> None:
    settings = get_settings()
    demo = build_demo_embedder("fake", settings)
    other = ReversedEmbedder(settings.embedding_dimensions)
    factory = get_sessionmaker()
    try:
        await prepare_index(factory, demo, CORPUS_DIR, reembed=False)
        assert await embeddings_match(factory, demo) is True
        assert await embeddings_match(factory, other) is False

        with pytest.raises(EmbeddingMismatch) as refused:
            await prepare_index(factory, other, CORPUS_DIR, reembed=False)
        message = str(refused.value)
        assert "demo_04_embeddings" in message and "--reembed" in message
        assert "python -m ingest run --reembed" in message

        await prepare_index(factory, other, CORPUS_DIR, reembed=True)
        assert await embeddings_match(factory, other) is True
    finally:
        await dispose_engine()
