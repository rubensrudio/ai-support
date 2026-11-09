from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from api.llm.tokens import count_tokens
from ingest.chunking import CHUNK_OVERLAP_TOKENS, CHUNK_SIZE_TOKENS, TextChunk, chunk_sections
from ingest.parsers import (
    Section,
    UnreadableDocument,
    parse_document,
    parse_markdown,
    parse_markdown_text,
    parse_pdf,
)


def _text_with_tokens(target: int) -> str:
    """Unique sentences cut at a word boundary and padded to exactly `target` tokens."""
    sentences = [f"Sentence {i} explains how refund request {i} is reviewed." for i in range(400)]
    words = " ".join(sentences).split(" ")
    low, high = 0, len(words)
    while low < high:  # largest word prefix with at most `target` tokens
        middle = (low + high + 1) // 2
        if count_tokens(" ".join(words[:middle])) <= target:
            low = middle
        else:
            high = middle - 1
    text = " ".join(words[:low])
    while count_tokens(text) < target:
        text += " ok"
    assert count_tokens(text) == target
    return text


def _overlap_tokens(previous: str, current: str) -> int:
    for size in range(min(len(previous), len(current)), 0, -1):
        if previous.endswith(current[:size]):
            return count_tokens(current[:size])
    return 0


def _write_pdf(path: Path, pages: list[str]) -> None:
    pdf = canvas.Canvas(str(path), pagesize=A4)
    for page in pages:
        y = 800
        for line in page.splitlines():
            pdf.drawString(72, y, line)
            y -= 16
        pdf.showPage()
    pdf.save()


def test_chunk_constants_follow_the_plan() -> None:
    assert (CHUNK_SIZE_TOKENS, CHUNK_OVERLAP_TOKENS) == (500, 80)


def test_long_section_is_split_into_overlapping_windows_within_the_limit() -> None:
    chunks = chunk_sections([Section(text=_text_with_tokens(1300), section="Refunds")])

    assert len(chunks) == 3
    assert all(chunk.token_count <= CHUNK_SIZE_TOKENS for chunk in chunks)
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert _overlap_tokens(previous.content, current.content) >= 60


def test_1400_token_text_needs_four_windows_of_at_most_500_tokens() -> None:
    # 3 windows of <= 500 tokens with >= 60 tokens of overlap cover at most 1380 tokens.
    chunks = chunk_sections([Section(text=_text_with_tokens(1400), section="Refunds")])

    assert len(chunks) == 4
    assert all(chunk.token_count <= CHUNK_SIZE_TOKENS for chunk in chunks)
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert _overlap_tokens(previous.content, current.content) >= 60


def test_chunks_cover_the_whole_section_and_report_their_token_count() -> None:
    text = _text_with_tokens(1400)
    chunks = chunk_sections([Section(text=text, section="Refunds")])

    assert chunks[0].content == text[: len(chunks[0].content)]
    assert text.endswith(chunks[-1].content)
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.token_count == count_tokens(chunk.content) for chunk in chunks)


def test_a_section_that_fits_is_never_cut() -> None:
    first, second = _text_with_tokens(300), _text_with_tokens(450)
    chunks = chunk_sections(
        [Section(text=first, section="Intro"), Section(text=second, section="Billing")]
    )

    assert chunks == [
        TextChunk(ordinal=0, content=first, section="Intro", page=None, token_count=300),
        TextChunk(ordinal=1, content=second, section="Billing", page=None, token_count=450),
    ]


def test_markdown_is_split_by_headings_and_chunks_carry_the_section() -> None:
    parsed = parse_markdown_text(
        "# Refund policy\n\nIntro text.\n\n## Eligibility\n\nWithin 7 days.\n\n"
        "```\n# not a heading\n```\n\n### Steps\n\nOpen a ticket.\n"
    )

    assert parsed.title == "Refund policy"
    assert [section.section for section in parsed.sections] == [
        "Refund policy",
        "Eligibility",
        "Steps",
    ]
    assert "# not a heading" in parsed.sections[1].text
    assert parsed.sections[2].text == "### Steps\n\nOpen a ticket."
    chunks = chunk_sections(parsed.sections)
    assert [(chunk.section, chunk.page) for chunk in chunks] == [
        ("Refund policy", None),
        ("Eligibility", None),
        ("Steps", None),
    ]


def test_markdown_text_before_the_first_heading_has_no_section() -> None:
    parsed = parse_markdown_text("Preamble line.\n\n# Title\n\nBody.\n\n## Empty\n\n")

    assert [(section.section, section.text) for section in parsed.sections] == [
        (None, "Preamble line."),
        ("Title", "# Title\n\nBody."),
    ]


def test_pdf_is_split_by_page_and_chunks_carry_the_page(tmp_path: Path) -> None:
    path = tmp_path / "terms.pdf"
    _write_pdf(path, ["Terms of service", "Refunds within 7 days.\nAmounts in USD."])

    parsed = parse_pdf(path)
    chunks = chunk_sections(parsed.sections)

    assert [(chunk.section, chunk.page) for chunk in chunks] == [(None, 1), (None, 2)]
    assert "Terms of service" in chunks[0].content
    assert "Amounts in USD." in chunks[1].content


def test_corrupted_pdf_raises_unreadable_document(tmp_path: Path) -> None:
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4\nthis is not a real pdf body\n")

    with pytest.raises(UnreadableDocument):
        parse_pdf(path)


def test_pdf_without_text_raises_unreadable_document(tmp_path: Path) -> None:
    path = tmp_path / "blank.pdf"
    _write_pdf(path, [""])

    with pytest.raises(UnreadableDocument):
        parse_pdf(path)


def test_markdown_that_is_not_utf8_raises_unreadable_document(tmp_path: Path) -> None:
    path = tmp_path / "latin1.md"
    path.write_bytes("# Política de reembolso\n\nDevolução".encode("latin-1"))

    with pytest.raises(UnreadableDocument):
        parse_markdown(path)


def test_missing_or_unsupported_files_raise_unreadable_document(tmp_path: Path) -> None:
    with pytest.raises(UnreadableDocument):
        parse_document(tmp_path / "missing.md", "md")
    other = tmp_path / "notes.docx"
    other.write_bytes(b"binary")
    with pytest.raises(UnreadableDocument):
        parse_document(other, "docx")


def test_parse_document_dispatches_on_the_format(tmp_path: Path) -> None:
    markdown = tmp_path / "faq.md"
    markdown.write_text("# FAQ\n\nAnswer.\n", encoding="utf-8")
    pdf = tmp_path / "faq.pdf"
    _write_pdf(pdf, ["FAQ answer"])

    assert parse_document(markdown, "md").sections[0].section == "FAQ"
    assert parse_document(pdf, "pdf").sections[0].page == 1
