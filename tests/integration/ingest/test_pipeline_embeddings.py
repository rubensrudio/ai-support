"""Embedding step of the ingestion pipeline (TASK-055, RAG-07, RAG-93, CT-28, CT-34)."""

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ingest.__main__ as cli
from api.config import MissingConfigError, Settings, get_settings
from api.rag.embeddings import EmbeddingError, FakeEmbeddingClient
from api.rag.models import EMBEDDING_DIMENSIONS, Chunk, Document
from ingest.pipeline import ReembedAborted, ingest_corpus
from ingest.report import IngestReport

pytestmark = pytest.mark.integration

ROOT_DIR = Path(__file__).resolve().parents[3]

Factory = async_sessionmaker[AsyncSession]

KEY_PREFIX = "ingest-test-"
REFUND_KEY = f"{KEY_PREFIX}embed-refund"
SHIPPING_KEY = f"{KEY_PREFIX}embed-shipping"
PROVIDER_SECRET = "provider body: invalid api key sk-secret"

TEXTS = {
    (REFUND_KEY, "en"): (
        "# Refund policy\n\nRefunds are accepted within 7 days of the charge.\n\n"
        "## Amounts\n\nAll amounts are in USD.\n"
    ),
    (REFUND_KEY, "pt"): (
        "# Política de reembolso\n\nReembolsos são aceitos em até 7 dias após a cobrança.\n\n"
        "## Valores\n\nTodos os valores estão em USD.\n"
    ),
    (SHIPPING_KEY, "en"): "# Shipping\n\nOrders ship in 2 business days.\n",
    (SHIPPING_KEY, "pt"): "# Envio\n\nPedidos são enviados em 2 dias úteis.\n",
}


def build_corpus(root: Path) -> Path:
    corpus = root / "corpus"
    for (doc_key, language), text in TEXTS.items():
        (corpus / language).mkdir(parents=True, exist_ok=True)
        (corpus / language / f"{doc_key}.md").write_text(text, encoding="utf-8")
    documents = [
        {"doc_key": key, "title": key, "doc_type": "policy", "format": "md"}
        for key in (REFUND_KEY, SHIPPING_KEY)
    ]
    (corpus / "manifest.yaml").write_text(
        yaml.safe_dump({"documents": documents}, allow_unicode=True), encoding="utf-8"
    )
    return corpus


def fake_embedder() -> FakeEmbeddingClient:
    return FakeEmbeddingClient(EMBEDDING_DIMENSIONS)


class FailingOnCall(FakeEmbeddingClient):
    """Fake embedder whose n-th call (1-based) raises `EmbeddingError`."""

    def __init__(self, failing_call: int) -> None:
        super().__init__(EMBEDDING_DIMENSIONS)
        self.failing_call = failing_call

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if len(self.calls) + 1 == self.failing_call:
            self.calls.append(list(texts))
            raise EmbeddingError(PROVIDER_SECRET)
        return await super().embed(texts)


class NegatedFailingOnCall(FailingOnCall):
    """`FailingOnCall` whose vectors have the opposite sign of the plain fake ones."""

    def vector(self, text: str) -> list[float]:
        return [-x for x in super().vector(text)]


class NegatedEmbedder(FakeEmbeddingClient):
    """Another deterministic embedder: the fake vector with the opposite sign."""

    def vector(self, text: str) -> list[float]:
        return [-x for x in super().vector(text)]


@pytest.fixture
def fake_embedding_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make the CLI see `ATLAS_EMBEDDING_PROVIDER=fake`, whatever `.env` says."""
    monkeypatch.setenv("ATLAS_EMBEDDING_PROVIDER", "fake")
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


async def _chunks(factory: Factory) -> list[tuple[Chunk, Document]]:
    async with factory() as session:
        rows = await session.execute(
            select(Chunk, Document)
            .join(Document, Chunk.document_id == Document.id)
            .where(Document.doc_key.startswith(KEY_PREFIX))
            .order_by(Document.doc_key, Document.language, Chunk.ordinal)
        )
        return [(chunk, document) for chunk, document in rows]


async def _null_embeddings(factory: Factory) -> int:
    async with factory() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(Chunk)
                .join(Document, Chunk.document_id == Document.id)
                .where(Document.doc_key.startswith(KEY_PREFIX), Chunk.embedding.is_(None))
            )
        ).scalar_one()


async def _documents(factory: Factory) -> dict[tuple[str, str], Document]:
    async with factory() as session:
        rows = (
            await session.execute(select(Document).where(Document.doc_key.startswith(KEY_PREFIX)))
        ).scalars()
        return {(row.doc_key, row.language): row for row in rows}


async def test_every_ingested_chunk_has_the_embedding_of_its_content(
    tmp_path: Path, session_factory: Factory
) -> None:
    embedder = fake_embedder()

    report = await ingest_corpus(build_corpus(tmp_path), session_factory, embedder=embedder)

    assert {document.status for document in report.documents} == {"indexed"}
    chunks = await _chunks(session_factory)
    assert chunks
    assert await _null_embeddings(session_factory) == 0
    for chunk, _ in chunks:
        assert chunk.embedding is not None
        assert np.allclose(np.asarray(chunk.embedding), embedder.vector(chunk.content))
    # One batched call per document.
    assert len(embedder.calls) == len(TEXTS)


async def test_embedding_failure_fails_only_that_document_without_chunks(
    tmp_path: Path, session_factory: Factory
) -> None:
    corpus = build_corpus(tmp_path)
    embedder = FailingOnCall(2)

    report = await ingest_corpus(corpus, session_factory, embedder=embedder)

    by_key = {(document.doc_key, document.language): document for document in report.documents}
    failed = by_key[(REFUND_KEY, "pt")]  # second (doc_key, language) processed
    assert failed.status == "failed"
    assert failed.chunks == 0
    assert failed.error == "embedding failed: EmbeddingError"
    others = [document for key, document in by_key.items() if key != (REFUND_KEY, "pt")]
    assert {document.status for document in others} == {"indexed"}
    stored = await _documents(session_factory)
    assert stored[(REFUND_KEY, "pt")].status == "failed"
    assert stored[(REFUND_KEY, "pt")].error == "embedding failed: EmbeddingError"
    assert PROVIDER_SECRET not in (stored[(REFUND_KEY, "pt")].error or "")
    keys = {(document.doc_key, document.language) for _, document in await _chunks(session_factory)}
    assert (REFUND_KEY, "pt") not in keys
    assert keys == set(TEXTS) - {(REFUND_KEY, "pt")}
    assert await _null_embeddings(session_factory) == 0

    # Reprocessable: the next run embeds and indexes it.
    retry = await ingest_corpus(corpus, session_factory, embedder=fake_embedder())

    assert {document.status for document in retry.documents} == {"indexed"}
    keys = {(document.doc_key, document.language) for _, document in await _chunks(session_factory)}
    assert keys == set(TEXTS)
    assert await _null_embeddings(session_factory) == 0


async def test_reingesting_a_document_without_embeddings_fills_them(
    tmp_path: Path, session_factory: Factory
) -> None:
    corpus = build_corpus(tmp_path)
    await ingest_corpus(corpus, session_factory)  # phase 03 behaviour: no embedder
    before = await _chunks(session_factory)
    assert before
    assert await _null_embeddings(session_factory) == len(before)

    embedder = fake_embedder()
    report = await ingest_corpus(corpus, session_factory, embedder=embedder)

    assert {document.status for document in report.documents} == {"indexed"}
    after = await _chunks(session_factory)
    assert await _null_embeddings(session_factory) == 0
    # The unchanged chunks are kept (same ids) and only gain their embeddings.
    assert [chunk.id for chunk, _ in after] == [chunk.id for chunk, _ in before]
    for chunk, _ in after:
        assert np.allclose(np.asarray(chunk.embedding), embedder.vector(chunk.content))
    assert sorted(len(call) for call in embedder.calls) == sorted(
        sum(1 for _, document in before if (document.doc_key, document.language) == key)
        for key in TEXTS
    )


async def test_reingesting_with_every_embedding_present_does_not_call_the_embedder(
    tmp_path: Path, session_factory: Factory
) -> None:
    corpus = build_corpus(tmp_path)
    await ingest_corpus(corpus, session_factory, embedder=fake_embedder())
    before = await _chunks(session_factory)

    embedder = fake_embedder()
    report = await ingest_corpus(corpus, session_factory, embedder=embedder)

    assert embedder.calls == []
    assert {document.status for document in report.documents} == {"indexed"}
    assert [chunk.id for chunk, _ in await _chunks(session_factory)] == [
        chunk.id for chunk, _ in before
    ]


async def test_failure_while_filling_missing_embeddings_leaves_no_chunks(
    tmp_path: Path, session_factory: Factory
) -> None:
    corpus = build_corpus(tmp_path)
    await ingest_corpus(corpus, session_factory, skip_embeddings=True)

    report = await ingest_corpus(corpus, session_factory, embedder=FailingOnCall(1))

    first = report.documents[0]
    assert (first.doc_key, first.language) == (REFUND_KEY, "en")
    assert first.status == "failed"
    assert first.error == "embedding failed: EmbeddingError"
    keys = {(document.doc_key, document.language) for _, document in await _chunks(session_factory)}
    assert (REFUND_KEY, "en") not in keys
    assert await _null_embeddings(session_factory) == 0


async def test_skip_embeddings_keeps_the_phase_03_behaviour(
    tmp_path: Path, session_factory: Factory
) -> None:
    embedder = fake_embedder()

    report = await ingest_corpus(
        build_corpus(tmp_path), session_factory, skip_embeddings=True, embedder=embedder
    )

    assert {document.status for document in report.documents} == {"indexed"}
    chunks = await _chunks(session_factory)
    assert chunks
    assert await _null_embeddings(session_factory) == len(chunks)
    assert embedder.calls == []


async def test_reembed_recomputes_every_embedding_keeping_the_chunks(
    tmp_path: Path, session_factory: Factory
) -> None:
    corpus = build_corpus(tmp_path)
    await ingest_corpus(corpus, session_factory, embedder=fake_embedder())
    before = await _chunks(session_factory)

    embedder = NegatedEmbedder(EMBEDDING_DIMENSIONS)
    report = await ingest_corpus(corpus, session_factory, embedder=embedder, reembed=True)

    assert {document.status for document in report.documents} == {"indexed"}
    after = await _chunks(session_factory)
    assert [chunk.id for chunk, _ in after] == [chunk.id for chunk, _ in before]
    for chunk, _ in after:
        assert np.allclose(np.asarray(chunk.embedding), embedder.vector(chunk.content))
    assert sum(len(call) for call in embedder.calls) == len(after)
    assert len(embedder.calls) == len(TEXTS)


async def test_reembed_aborts_on_the_first_failure_keeping_the_index(
    tmp_path: Path, session_factory: Factory
) -> None:
    corpus = build_corpus(tmp_path)
    old = fake_embedder()
    await ingest_corpus(corpus, session_factory, embedder=old)
    before = await _chunks(session_factory)
    embedder = NegatedFailingOnCall(2)  # fails on the 2nd document: (REFUND_KEY, "pt")

    with pytest.raises(ReembedAborted, match=r"^re-embedding aborted: EmbeddingError$"):
        await ingest_corpus(corpus, session_factory, embedder=embedder, reembed=True)

    after = await _chunks(session_factory)
    assert [chunk.id for chunk, _ in after] == [chunk.id for chunk, _ in before]
    stored = await _documents(session_factory)
    assert {document.status for document in stored.values()} == {"indexed"}
    assert {document.error for document in stored.values()} == {None}
    assert await _null_embeddings(session_factory) == 0
    for chunk, document in after:
        # The 1st document was committed before the failure and keeps its new vectors; the
        # failing one and the ones after it keep the old vectors.
        current = embedder if (document.doc_key, document.language) == (REFUND_KEY, "en") else old
        assert np.allclose(np.asarray(chunk.embedding), current.vector(chunk.content))
    assert PROVIDER_SECRET not in str(ReembedAborted("EmbeddingError"))


async def test_failures_outside_reembed_still_fail_only_the_document(
    tmp_path: Path, session_factory: Factory
) -> None:
    corpus = build_corpus(tmp_path)
    await ingest_corpus(corpus, session_factory, skip_embeddings=True)

    report = await ingest_corpus(corpus, session_factory, embedder=FailingOnCall(2))

    assert [document.status for document in report.documents].count("failed") == 1
    assert report.documents[1].error == "embedding failed: EmbeddingError"


async def test_reembed_needs_an_embedder(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reembed"):
        await ingest_corpus(tmp_path, async_sessionmaker(), reembed=True)
    with pytest.raises(ValueError, match="reembed"):
        await ingest_corpus(
            tmp_path,
            async_sessionmaker(),
            skip_embeddings=True,
            embedder=fake_embedder(),
            reembed=True,
        )


async def test_cli_without_openai_key_stops_before_touching_the_index(
    tmp_path: Path, session_factory: Factory, test_database_url: str
) -> None:
    corpus = build_corpus(tmp_path)
    # Real loss scenario: a phase 03 / --skip-embeddings index, whose null embeddings would make
    # every document call the embedder (and fail) without the fail-fast.
    await ingest_corpus(corpus, session_factory, skip_embeddings=True)
    before = await _chunks(session_factory)
    assert before
    report = tmp_path / "report.json"
    env = {
        **os.environ,
        "ATLAS_DATABASE_URL": test_database_url,
        "ATLAS_LLM_PROVIDER": "fake",
        "ATLAS_EMBEDDING_PROVIDER": "openai",
        "OPENAI_API_KEY": "",  # overrides any key in .env: no request can reach OpenAI
    }

    completed = subprocess.run(  # noqa: S603 - fixed interpreter and arguments
        [sys.executable, "-m", "ingest", "run", "--corpus", str(corpus), "--report", str(report)],
        cwd=ROOT_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 1
    assert (
        "error: Missing required configuration: OPENAI_API_KEY (or use --skip-embeddings)"
        in completed.stderr
    )
    assert not report.exists()
    after = await _chunks(session_factory)
    assert [chunk.id for chunk, _ in after] == [chunk.id for chunk, _ in before]
    assert {document.status for document in (await _documents(session_factory)).values()} == {
        "indexed"
    }
    assert await _null_embeddings(session_factory) == len(before)


@pytest.mark.usefixtures("fake_embedding_provider")
def test_cli_reports_an_aborted_reembed_without_writing_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def ingest(corpus: Path, factory: object, **kwargs: Any) -> IngestReport:
        assert kwargs["reembed"] is True
        raise ReembedAborted("EmbeddingError")

    async def no_dispose() -> None:
        return None

    monkeypatch.setattr(cli, "ingest_corpus", ingest)
    monkeypatch.setattr(cli, "get_sessionmaker", lambda: None)
    monkeypatch.setattr(cli, "dispose_engine", no_dispose)
    report = tmp_path / "report.json"

    code = cli.main(["run", "--corpus", str(tmp_path), "--report", str(report), "--reembed"])

    assert code == cli.EXIT_FAILED
    assert "error: re-embedding aborted: EmbeddingError" in capsys.readouterr().err
    assert not report.exists()


@pytest.mark.parametrize(
    ("provider", "key", "missing"),
    [
        ("openai", None, True),
        ("openai", "", True),
        ("openai", "   ", True),
        ("openai", "sk-test", False),
        ("fake", None, False),
    ],
)
def test_embedding_config_check(provider: str, key: str | None, missing: bool) -> None:
    settings = Settings(embedding_provider=provider, openai_api_key=key)
    if missing:
        with pytest.raises(MissingConfigError, match="OPENAI_API_KEY"):
            cli._require_embedding_config(settings)
    else:
        cli._require_embedding_config(settings)


def test_cli_rejects_reembed_with_skip_embeddings(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["run", "--corpus", str(tmp_path), "--skip-embeddings", "--reembed"])
    assert exit_info.value.code == 2


@pytest.mark.usefixtures("fake_embedding_provider")
@pytest.mark.parametrize(
    ("flag", "expects_embedder", "expects_reembed"),
    [((), True, False), (("--skip-embeddings",), False, False), (("--reembed",), True, True)],
)
def test_cli_builds_the_embedder_unless_embeddings_are_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: tuple[str, ...],
    expects_embedder: bool,
    expects_reembed: bool,
) -> None:
    sentinel = fake_embedder()
    built: list[object] = []
    received: dict[str, Any] = {}

    def build_embedder(settings: object) -> FakeEmbeddingClient:
        built.append(settings)
        return sentinel

    async def ingest(corpus: Path, factory: object, **kwargs: Any) -> IngestReport:
        received.update(kwargs)
        return IngestReport()

    async def no_dispose() -> None:
        return None

    monkeypatch.setattr(cli, "build_embedder", build_embedder)
    monkeypatch.setattr(cli, "ingest_corpus", ingest)
    monkeypatch.setattr(cli, "get_sessionmaker", lambda: None)
    monkeypatch.setattr(cli, "dispose_engine", no_dispose)

    report = tmp_path / "report.json"
    code = cli.main(["run", "--corpus", str(tmp_path), "--report", str(report), *flag])

    assert code == cli.EXIT_OK
    assert received["skip_embeddings"] is (not expects_embedder)
    assert received["embedder"] is (sentinel if expects_embedder else None)
    assert received["reembed"] is expects_reembed
    assert len(built) == (1 if expects_embedder else 0)
