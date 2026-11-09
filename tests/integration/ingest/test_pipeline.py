import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.rag.models import TS_CONFIGS, Chunk, Document
from ingest.pipeline import ingest_corpus

pytestmark = pytest.mark.integration

ROOT_DIR = Path(__file__).resolve().parents[3]

Factory = async_sessionmaker[AsyncSession]

KEY_PREFIX = "ingest-test-"
MD_KEY = f"{KEY_PREFIX}refund-policy"
PDF_KEY = f"{KEY_PREFIX}terms"

REFUND_EN = (
    "# Refund policy\n\nRefunds are accepted within 7 days of the charge.\n\n"
    "## Amounts\n\nAll amounts are in USD.\n"
)
REFUND_PT = (
    "# Política de reembolso\n\nReembolsos são aceitos em até 7 dias após a cobrança.\n\n"
    "## Valores\n\nTodos os valores estão em USD.\n"
)


def write_pdf(path: Path, pages: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=A4)
    for page in pages:
        y = 800
        for line in page.splitlines():
            pdf.drawString(72, y, line)
            y -= 16
        pdf.showPage()
    pdf.save()


def write_manifest(corpus: Path, documents: list[dict[str, object]]) -> None:
    corpus.mkdir(parents=True, exist_ok=True)
    (corpus / "manifest.yaml").write_text(
        yaml.safe_dump({"documents": documents}, allow_unicode=True), encoding="utf-8"
    )


def build_corpus(root: Path, *, corrupt_pt_pdf: bool) -> Path:
    corpus = root / "corpus"
    for language, text in (("en", REFUND_EN), ("pt", REFUND_PT)):
        (corpus / language).mkdir(parents=True, exist_ok=True)
        (corpus / language / f"{MD_KEY}.md").write_text(text, encoding="utf-8")
    write_pdf(corpus / "pdf" / "en" / f"{PDF_KEY}.pdf", ["Terms of service", "Pay in USD."])
    pt_pdf = corpus / "pdf" / "pt" / f"{PDF_KEY}.pdf"
    if corrupt_pt_pdf:
        pt_pdf.parent.mkdir(parents=True, exist_ok=True)
        pt_pdf.write_bytes(b"%PDF-1.4\ncorrupted body\n")
    else:
        write_pdf(pt_pdf, ["Termos de serviço", "Pague em USD."])
    write_manifest(
        corpus,
        [
            {
                "doc_key": MD_KEY,
                "title": {"en": "Refund policy", "pt": "Política de reembolso"},
                "doc_type": "policy",
                "format": "md",
                "languages": ["en", "pt"],
                "malicious": False,
            },
            {
                "doc_key": PDF_KEY,
                "title": "Terms of service",
                "doc_type": "manual",
                "format": "pdf",
                "languages": ["en", "pt"],
            },
        ],
    )
    return corpus


async def _documents(factory: Factory) -> dict[tuple[str, str], Document]:
    async with factory() as session:
        rows = (
            await session.execute(select(Document).where(Document.doc_key.startswith(KEY_PREFIX)))
        ).scalars()
        return {(row.doc_key, row.language): row for row in rows}


async def _chunks(factory: Factory) -> list[tuple[Chunk, Document]]:
    async with factory() as session:
        rows = await session.execute(
            select(Chunk, Document)
            .join(Document, Chunk.document_id == Document.id)
            .where(Document.doc_key.startswith(KEY_PREFIX))
            .order_by(Document.doc_key, Document.language, Chunk.ordinal)
        )
        return [(chunk, document) for chunk, document in rows]


async def _chunk_count(factory: Factory) -> int:
    async with factory() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(Chunk)
                .join(Document, Chunk.document_id == Document.id)
                .where(Document.doc_key.startswith(KEY_PREFIX))
            )
        ).scalar_one()


async def test_md_and_pdf_are_indexed_and_a_corrupted_file_fails_alone(
    tmp_path: Path, session_factory: Factory
) -> None:
    corpus = build_corpus(tmp_path, corrupt_pt_pdf=True)

    report = await ingest_corpus(corpus, session_factory)

    statuses = {(doc.doc_key, doc.language): doc.status for doc in report.documents}
    assert statuses == {
        (MD_KEY, "en"): "indexed",
        (MD_KEY, "pt"): "indexed",
        (PDF_KEY, "en"): "indexed",
        (PDF_KEY, "pt"): "failed",
    }
    assert report.totals() == {"indexed": 3, "quarantined": 0, "failed": 1}
    failed = next(doc for doc in report.documents if doc.status == "failed")
    assert failed.format == "pdf" and failed.chunks == 0 and failed.error
    assert all(doc.chunks > 0 for doc in report.documents if doc.status == "indexed")

    stored = await _documents(session_factory)
    assert stored[(PDF_KEY, "pt")].status == "failed"
    assert stored[(PDF_KEY, "pt")].error == failed.error
    assert stored[(MD_KEY, "pt")].title == "Política de reembolso"
    assert stored[(PDF_KEY, "en")].doc_type == "manual"
    chunk_documents = {(doc.doc_key, doc.language) for _, doc in await _chunks(session_factory)}
    assert (PDF_KEY, "pt") not in chunk_documents


async def test_ingesting_twice_does_not_duplicate_chunks(
    tmp_path: Path, session_factory: Factory
) -> None:
    corpus = build_corpus(tmp_path, corrupt_pt_pdf=False)

    first = await ingest_corpus(corpus, session_factory)
    count_after_first = await _chunk_count(session_factory)
    ids_after_first = {key: doc.id for key, doc in (await _documents(session_factory)).items()}
    second = await ingest_corpus(corpus, session_factory)

    assert count_after_first > 0
    assert await _chunk_count(session_factory) == count_after_first
    assert {k: d.id for k, d in (await _documents(session_factory)).items()} == ids_after_first
    assert [doc.chunks for doc in second.documents] == [doc.chunks for doc in first.documents]


async def test_changed_document_replaces_its_chunks(
    tmp_path: Path, session_factory: Factory
) -> None:
    corpus = build_corpus(tmp_path, corrupt_pt_pdf=False)
    await ingest_corpus(corpus, session_factory)
    source = corpus / "en" / f"{MD_KEY}.md"
    source.write_text("# Refund policy\n\nRefunds now take 14 days.\n", encoding="utf-8")
    hash_before = (await _documents(session_factory))[(MD_KEY, "en")].content_hash

    report = await ingest_corpus(corpus, session_factory)

    contents = [
        chunk.content
        for chunk, doc in await _chunks(session_factory)
        if (doc.doc_key, doc.language) == (MD_KEY, "en")
    ]
    assert contents == ["# Refund policy\n\nRefunds now take 14 days."]
    assert (await _documents(session_factory))[(MD_KEY, "en")].content_hash != hash_before
    assert (
        next(d for d in report.documents if (d.doc_key, d.language) == (MD_KEY, "en")).chunks == 1
    )


async def test_every_chunk_has_language_and_source_metadata(
    tmp_path: Path, session_factory: Factory
) -> None:
    corpus = build_corpus(tmp_path, corrupt_pt_pdf=False)

    await ingest_corpus(corpus, session_factory)

    rows = await _chunks(session_factory)
    assert {(doc.doc_key, doc.language) for _, doc in rows} == {
        (MD_KEY, "en"),
        (MD_KEY, "pt"),
        (PDF_KEY, "en"),
        (PDF_KEY, "pt"),
    }
    for chunk, document in rows:
        assert chunk.language == document.language
        assert chunk.ts_config == TS_CONFIGS[document.language]
        assert chunk.token_count > 0
        assert document.source_path.endswith(f".{document.format}")
        if document.format == "md":
            assert chunk.section is not None and chunk.page is None
        else:
            assert chunk.page is not None


async def test_unsupported_entries_fail_in_the_report_and_the_rest_continue(
    tmp_path: Path, session_factory: Factory
) -> None:
    corpus = tmp_path / "corpus"
    (corpus / "en").mkdir(parents=True)
    (corpus / "en" / f"{MD_KEY}.md").write_text("# FAQ\n\nAnswer.\n", encoding="utf-8")
    write_manifest(
        corpus,
        [
            {"doc_key": f"{KEY_PREFIX}docx", "title": "Word", "doc_type": "faq", "format": "docx"},
            {"doc_key": f"{KEY_PREFIX}missing", "title": "Gone", "doc_type": "faq", "format": "md"},
            {
                "doc_key": MD_KEY,
                "title": "FAQ",
                "doc_type": "faq",
                "format": "md",
                "languages": ["en"],
            },
        ],
    )

    report = await ingest_corpus(corpus, session_factory)

    statuses = {(doc.doc_key, doc.language): doc.status for doc in report.documents}
    assert statuses[(MD_KEY, "en")] == "indexed"
    assert statuses[(f"{KEY_PREFIX}docx", "en")] == "failed"
    assert statuses[(f"{KEY_PREFIX}missing", "pt")] == "failed"
    stored = await _documents(session_factory)
    assert (f"{KEY_PREFIX}docx", "en") not in stored  # the format CHECK cannot hold it
    assert stored[(f"{KEY_PREFIX}missing", "en")].status == "failed"


async def test_documents_removed_from_the_manifest_are_marked_failed_without_chunks(
    tmp_path: Path, session_factory: Factory
) -> None:
    corpus = build_corpus(tmp_path, corrupt_pt_pdf=False)
    await ingest_corpus(corpus, session_factory)
    write_manifest(
        corpus,
        [
            {
                "doc_key": MD_KEY,
                "title": "Refund policy",
                "doc_type": "policy",
                "format": "md",
                "languages": ["en"],
            }
        ],
    )

    report = await ingest_corpus(corpus, session_factory)

    removed = {
        (doc.doc_key, doc.language): doc for doc in report.documents if doc.status == "failed"
    }
    assert set(removed) == {(MD_KEY, "pt"), (PDF_KEY, "en"), (PDF_KEY, "pt")}
    assert all(doc.error == "removed from manifest" and doc.chunks == 0 for doc in removed.values())
    assert removed[(PDF_KEY, "en")].format == "pdf"
    assert removed[(PDF_KEY, "en")].doc_type == "manual"
    stored = await _documents(session_factory)
    assert {key: doc.status for key, doc in stored.items()} == {
        (MD_KEY, "en"): "indexed",
        (MD_KEY, "pt"): "failed",
        (PDF_KEY, "en"): "failed",
        (PDF_KEY, "pt"): "failed",
    }
    assert stored[(PDF_KEY, "pt")].error == "removed from manifest"
    assert {(doc.doc_key, doc.language) for _, doc in await _chunks(session_factory)} == {
        (MD_KEY, "en")
    }


async def test_storage_errors_do_not_leak_driver_messages(
    tmp_path: Path, session_factory: Factory
) -> None:
    corpus = tmp_path / "corpus"
    secret = "Secret chunk body"
    # Valid UTF-8, but PostgreSQL text columns reject NUL, so the insert fails in the driver.
    (corpus / "en").mkdir(parents=True)
    (corpus / "en" / f"{MD_KEY}.md").write_text(f"# FAQ\n\n{secret}\x00 here.\n", "utf-8")
    write_manifest(
        corpus,
        [
            {
                "doc_key": MD_KEY,
                "title": "FAQ",
                "doc_type": "faq",
                "format": "md",
                "languages": ["en"],
            }
        ],
    )

    report = await ingest_corpus(corpus, session_factory)

    (document,) = report.documents
    assert document.status == "failed"
    assert document.error is not None
    assert document.error.startswith(f"cannot store corpus/en/{MD_KEY}.md: ")
    error_type = document.error.rsplit(": ", 1)[1]
    assert error_type.isidentifier(), document.error
    assert secret not in document.error and "INSERT" not in document.error.upper()
    assert (await _documents(session_factory))[(MD_KEY, "en")].error == document.error


def _cli(*args: str, database_url: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "ATLAS_DATABASE_URL": database_url, "ATLAS_LLM_PROVIDER": "fake"}
    return subprocess.run(  # noqa: S603 - fixed interpreter and arguments
        [sys.executable, "-m", "ingest", *args],
        cwd=ROOT_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


async def test_cli_writes_the_report_and_exits_1_when_a_document_failed(
    tmp_path: Path, session_factory: Factory, migrated_database: str
) -> None:
    corpus = build_corpus(tmp_path / "bad", corrupt_pt_pdf=True)
    report_path = tmp_path / "out" / "ingest-report.json"

    failed_run = _cli(
        "run",
        "--corpus",
        str(corpus),
        "--report",
        str(report_path),
        "--skip-embeddings",
        database_url=migrated_database,
    )

    assert failed_run.returncode == 1, failed_run.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["totals"] == {"indexed": 3, "quarantined": 0, "failed": 1}
    assert set(report["documents"][0]) == {
        "doc_key",
        "language",
        "doc_type",
        "format",
        "status",
        "chunks",
        "error",
    }

    good = build_corpus(tmp_path / "good", corrupt_pt_pdf=False)
    ok_run = _cli(
        "run", "--corpus", str(good), "--report", str(report_path), database_url=migrated_database
    )
    assert ok_run.returncode == 0, ok_run.stderr
    assert json.loads(report_path.read_text(encoding="utf-8"))["totals"]["failed"] == 0
