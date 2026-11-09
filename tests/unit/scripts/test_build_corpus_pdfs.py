"""Corpus PDF builder (TASK-042, RAG-02)."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pypdf import PdfReader

from scripts.build_corpus_pdfs import build_all, main, parse_markdown, plain_text

CORPUS = Path(__file__).resolve().parents[3] / "corpus"


def _pdf_items() -> list[dict[str, Any]]:
    with (CORPUS / "manifest.yaml").open(encoding="utf-8") as handle:
        manifest = yaml.safe_load(handle)
    return [doc for doc in manifest["documents"] if doc["format"] == "pdf"]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("•", " ")).strip()


def _pages(path: Path) -> list[str]:
    return [_normalize(page.extract_text()) for page in PdfReader(path).pages]


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("pdf")
    build_all(CORPUS, out)
    return out


def test_parse_markdown_splits_title_and_sections() -> None:
    document = parse_markdown(
        "# Title\n\nIntro.\n\n## First\n\nA **b** c.\n\n- one\n- two\n\n## Second\n\nEnd.\n"
    )
    assert document.title == "Title"
    assert [section.heading for section in document.sections] == ["First", "Second"]
    assert [block.text for block in document.intro] == ["Intro."]
    assert [block.kind for block in document.sections[0].blocks] == [
        "paragraph",
        "bullet",
        "bullet",
    ]
    assert plain_text("A **b** `c`.") == "A b c."


def test_builds_one_pdf_per_pdf_item_and_language(built: Path) -> None:
    expected = {
        Path(lang) / f"{doc['doc_key']}.pdf" for doc in _pdf_items() for lang in ("en", "pt")
    }
    produced = {path.relative_to(built) for path in built.rglob("*.pdf")}
    assert expected and produced == expected


@pytest.mark.parametrize("language", ["en", "pt"])
def test_pypdf_extracts_the_title_and_one_section_per_page(built: Path, language: str) -> None:
    for doc in _pdf_items():
        source = parse_markdown((CORPUS / doc["source"][language]).read_text(encoding="utf-8"))
        pages = _pages(built / language / f"{doc['doc_key']}.pdf")
        assert doc["title"][language] in pages[0]
        assert len(pages) == len(source.sections), doc["doc_key"]
        for page, section in zip(pages, source.sections, strict=True):
            assert _normalize(section.heading) in page
            for block in section.blocks:
                assert _normalize(plain_text(block.text)) in page, (doc["doc_key"], block)


def test_build_is_deterministic(tmp_path: Path, built: Path) -> None:
    build_all(CORPUS, tmp_path)
    for path in built.rglob("*.pdf"):
        assert (tmp_path / path.relative_to(built)).read_bytes() == path.read_bytes()


def test_versioned_pdfs_match_their_sources(built: Path) -> None:
    for doc in _pdf_items():
        for language in ("en", "pt"):
            versioned = CORPUS / doc["file"][language]
            assert versioned.is_file(), versioned
            assert _pages(versioned) == _pages(built / language / f"{doc['doc_key']}.pdf")


def test_cli_writes_pdfs_and_exits_zero(tmp_path: Path) -> None:
    assert main(["--corpus", str(CORPUS), "--out", str(tmp_path)]) == 0
    assert len(list(tmp_path.rglob("*.pdf"))) == 2 * len(_pdf_items())
