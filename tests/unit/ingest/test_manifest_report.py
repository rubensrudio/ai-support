import json
from pathlib import Path

import pytest

from ingest.__main__ import main
from ingest.pipeline import ManifestEntry, ManifestError, load_manifest, source_path
from ingest.report import DocumentReport, IngestReport, write_report


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_manifest_accepts_a_documents_list_with_titles_per_language(tmp_path: Path) -> None:
    _write(
        tmp_path / "manifest.yaml",
        "documents:\n"
        "  - doc_key: refund-policy\n"
        "    title: {en: Refund policy, pt: Política de reembolso}\n"
        "    doc_type: policy\n"
        "    format: md\n"
        "    malicious: true\n"
        "  - doc_key: faq\n"
        "    title: FAQ\n"
        "    doc_type: faq\n"
        "    format: pdf\n"
        "    languages: [en]\n",
    )

    refund, faq = load_manifest(tmp_path)

    assert (refund.title("en"), refund.title("pt")) == ("Refund policy", "Política de reembolso")
    assert refund.languages == ("en", "pt") and refund.malicious
    assert (faq.title("pt"), faq.languages, faq.malicious) == ("FAQ", ("en",), False)


def test_manifest_accepts_a_top_level_list(tmp_path: Path) -> None:
    _write(tmp_path / "manifest.yaml", "- {doc_key: a, title: A, doc_type: faq, format: md}\n")

    assert [entry.doc_key for entry in load_manifest(tmp_path)] == ["a"]


@pytest.mark.parametrize(
    "content",
    [
        "documents: {doc_key: a}\n",
        "- {title: no key}\n",
        "- {doc_key: a}\n- {doc_key: a}\n",
        "documents: [unclosed\n",
    ],
)
def test_invalid_manifest_raises_manifest_error(tmp_path: Path, content: str) -> None:
    _write(tmp_path / "manifest.yaml", content)

    with pytest.raises(ManifestError):
        load_manifest(tmp_path)


def test_missing_manifest_raises_manifest_error(tmp_path: Path) -> None:
    with pytest.raises(ManifestError):
        load_manifest(tmp_path)


def test_default_and_explicit_source_paths(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    md = ManifestEntry(doc_key="faq", titles={}, doc_type="faq", format="md")
    pdf = ManifestEntry(doc_key="terms", titles={}, doc_type="manual", format="pdf")
    flat_pdf = _write(corpus / "pdf" / "terms.pt.pdf", "")
    explicit = ManifestEntry(
        doc_key="faq",
        titles={},
        doc_type="faq",
        format="md",
        paths={"en": "corpus/en/custom.md", "pt": "pt/custom.md"},
    )
    _write(corpus / "en" / "custom.md", "")

    assert source_path(corpus, md, "pt") == corpus / "pt" / "faq.md"
    assert source_path(corpus, pdf, "en") == corpus / "pdf" / "en" / "terms.pdf"
    assert source_path(corpus, pdf, "pt") == flat_pdf
    assert source_path(corpus, explicit, "en") == corpus / "en" / "custom.md"
    assert source_path(corpus, explicit, "pt") == corpus / "pt" / "custom.md"


def test_pdf_entry_ignores_a_markdown_source_path(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    pdf = ManifestEntry(
        doc_key="terms", titles={}, doc_type="manual", format="pdf", paths={"en": "en/terms.md"}
    )

    assert source_path(corpus, pdf, "en") == corpus / "pdf" / "en" / "terms.pdf"


def test_report_counts_every_status_and_writes_the_ct28_json(tmp_path: Path) -> None:
    report = IngestReport(
        documents=[
            DocumentReport("a", "en", "faq", "md", "indexed", 3),
            DocumentReport("a", "pt", "faq", "md", "failed", 0, "a.md is not valid UTF-8"),
        ]
    )
    path = tmp_path / "reports" / "ingest-report.json"

    write_report(report, path)

    assert report.has_failures
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "documents": [
            {
                "doc_key": "a",
                "language": "en",
                "doc_type": "faq",
                "format": "md",
                "status": "indexed",
                "chunks": 3,
                "error": None,
            },
            {
                "doc_key": "a",
                "language": "pt",
                "doc_type": "faq",
                "format": "md",
                "status": "failed",
                "chunks": 0,
                "error": "a.md is not valid UTF-8",
            },
        ],
        "totals": {"indexed": 1, "quarantined": 0, "failed": 1},
    }


def test_cli_exits_1_when_the_manifest_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["run", "--corpus", str(tmp_path), "--report", str(tmp_path / "r.json")])

    assert code == 1
    assert "manifest.yaml" in capsys.readouterr().err
    assert not (tmp_path / "r.json").exists()


ROOT_DIR = Path(__file__).resolve().parents[3]


def test_file_key_is_the_source_read_for_each_language(tmp_path: Path) -> None:
    _write(
        tmp_path / "manifest.yaml",
        "documents:\n"
        "  - doc_key: terms\n"
        "    title: {en: Terms, pt: Termos}\n"
        "    doc_type: manual\n"
        "    format: pdf\n"
        "    source: {en: en/terms.md, pt: pt/terms.md}\n"
        "    file: {en: generated/terms-en.pdf, pt: generated/terms-pt.pdf}\n",
    )

    (entry,) = load_manifest(tmp_path)

    assert source_path(tmp_path, entry, "en") == tmp_path / "generated" / "terms-en.pdf"
    assert source_path(tmp_path, entry, "pt") == tmp_path / "generated" / "terms-pt.pdf"


def test_real_corpus_manifest_resolves_every_document_to_an_existing_file() -> None:
    corpus = ROOT_DIR / "corpus"
    entries = load_manifest(corpus)

    resolved = [
        (entry, source_path(corpus, entry, language))
        for entry in entries
        for language in entry.languages
    ]

    assert len(resolved) == 42
    assert len({(entry.doc_key, path) for entry, path in resolved}) == 42
    for entry, path in resolved:
        assert path.is_file(), path
        assert path.suffix == f".{entry.format}", path
