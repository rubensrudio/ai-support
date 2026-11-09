"""Build the PDF documents of the support corpus from their Markdown sources (TASK-042, RAG-02).

Usage: `python scripts/build_corpus_pdfs.py [--corpus corpus] [--out <corpus>/pdf]`

Every `corpus/manifest.yaml` item with `format: pdf` is rendered, for each of its languages,
into `<out>/<language>/<doc_key>.pdf`. The document title opens the first page and every `##`
section starts on its own page, so a PDF page maps to one section of the source.

The generated PDFs are versioned, so ingestion never depends on this builder. Builds run in
reportlab's invariant mode: the same source always produces the same bytes.

Supported Markdown: `#` title, `##` sections, `###` sub-headings, paragraphs, `-` bullets,
`1.` numbered items, and the inline `**bold**` and `` `code` `` markers.
"""

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from xml.sax.saxutils import escape

import yaml
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

DEFAULT_CORPUS = Path(__file__).resolve().parents[1] / "corpus"
MANIFEST_NAME = "manifest.yaml"
PDF_FORMAT = "pdf"
AUTHOR = "Assinaflow"
EXIT_OK = 0
EXIT_ERROR = 1

BlockKind = Literal["paragraph", "bullet", "numbered", "subheading"]

_TITLE = re.compile(r"^#\s+(.+?)\s*$")
_SECTION = re.compile(r"^##\s+(.+?)\s*$")
_SUBHEADING = re.compile(r"^###\s+(.+?)\s*$")
_BULLET = re.compile(r"^[-*]\s+(.+?)\s*$")
_NUMBERED = re.compile(r"^(\d+)\.\s+(.+?)\s*$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`(.+?)`")


@dataclass(frozen=True)
class Block:
    kind: BlockKind
    text: str
    marker: str = ""


@dataclass
class Section:
    heading: str
    blocks: list[Block] = field(default_factory=list)


@dataclass
class MarkdownDocument:
    title: str
    intro: list[Block]
    sections: list[Section]


@dataclass(frozen=True)
class PdfJob:
    doc_key: str
    language: str
    title: str
    source: Path
    target: Path


def plain_text(markdown: str) -> str:
    """Inline Markdown without its markers, as it reads in the rendered PDF."""
    return _CODE.sub(r"\1", _BOLD.sub(r"\1", markdown))


def parse_markdown(markdown: str) -> MarkdownDocument:
    """Split a Markdown source into title, intro blocks and `##` sections."""
    title = ""
    intro: list[Block] = []
    sections: list[Section] = []
    paragraph: list[str] = []

    def target() -> list[Block]:
        return sections[-1].blocks if sections else intro

    def flush() -> None:
        if paragraph:
            target().append(Block("paragraph", " ".join(paragraph)))
            paragraph.clear()

    for raw in markdown.splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        if line.startswith("|"):
            raise ValueError("tables are not supported in PDF sources")
        if match := _SECTION.match(line):
            flush()
            sections.append(Section(match.group(1)))
        elif match := _SUBHEADING.match(line):
            flush()
            target().append(Block("subheading", match.group(1)))
        elif match := _TITLE.match(line):
            flush()
            if title:
                raise ValueError("a PDF source must have a single `#` title")
            title = match.group(1)
        elif match := _BULLET.match(line):
            flush()
            target().append(Block("bullet", match.group(1), "•"))
        elif match := _NUMBERED.match(line):
            flush()
            target().append(Block("numbered", match.group(2), f"{match.group(1)}."))
        else:
            paragraph.append(line)
    flush()
    if not title:
        raise ValueError("a PDF source must start with a `#` title")
    return MarkdownDocument(title, intro, sections)


def _markup(markdown: str) -> str:
    text = escape(markdown)
    text = _BOLD.sub(r"<b>\1</b>", text)
    return _CODE.sub(r'<font face="Courier">\1</font>', text)


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    body = ParagraphStyle("CorpusBody", parent=base["BodyText"], fontSize=10.5, leading=15)
    return {
        "title": base["Title"],
        "section": base["Heading2"],
        "subheading": base["Heading3"],
        "paragraph": body,
        "item": ParagraphStyle("CorpusItem", parent=body, leftIndent=18, bulletIndent=6),
    }


def _flowables(block: Block, styles: dict[str, ParagraphStyle]) -> list[Any]:
    if block.kind == "subheading":
        return [Paragraph(_markup(block.text), styles["subheading"])]
    if block.kind == "paragraph":
        return [Paragraph(_markup(block.text), styles["paragraph"])]
    return [Paragraph(_markup(block.text), styles["item"], bulletText=block.marker)]


def render_pdf(document: MarkdownDocument, target: Path) -> None:
    """Render a parsed document: title on the first page, one page per section."""
    styles = _styles()
    story: list[Any] = [Paragraph(_markup(document.title), styles["title"])]
    for block in document.intro:
        story.extend(_flowables(block, styles))
    for index, section in enumerate(document.sections):
        if index:
            story.append(PageBreak())
        else:
            story.append(Spacer(1, 0.2 * inch))
        story.append(Paragraph(_markup(section.heading), styles["section"]))
        for block in section.blocks:
            story.extend(_flowables(block, styles))
    target.parent.mkdir(parents=True, exist_ok=True)
    pdf = SimpleDocTemplate(
        str(target),
        pagesize=LETTER,
        title=document.title,
        author=AUTHOR,
        creator="scripts/build_corpus_pdfs.py",
        invariant=1,
    )
    pdf.build(story)


def pdf_jobs(corpus: Path, out_dir: Path) -> list[PdfJob]:
    """One job per manifest item with `format: pdf` and per language."""
    with (corpus / MANIFEST_NAME).open(encoding="utf-8") as handle:
        manifest = yaml.safe_load(handle)
    jobs: list[PdfJob] = []
    for item in manifest["documents"]:
        if item["format"] != PDF_FORMAT:
            continue
        for language in item["languages"]:
            jobs.append(
                PdfJob(
                    doc_key=item["doc_key"],
                    language=language,
                    title=item["title"][language],
                    source=corpus / item["source"][language],
                    target=out_dir / language / f"{item['doc_key']}.pdf",
                )
            )
    return jobs


def build_all(corpus: Path, out_dir: Path | None = None) -> list[Path]:
    """Build every PDF of the corpus and return the written paths."""
    out = out_dir if out_dir is not None else corpus / PDF_FORMAT
    written: list[Path] = []
    for job in pdf_jobs(corpus, out):
        document = parse_markdown(job.source.read_text(encoding="utf-8"))
        if document.title != job.title:
            raise ValueError(
                f"{job.source}: title {document.title!r} differs from the manifest {job.title!r}"
            )
        render_pdf(document, job.target)
        written.append(job.target)
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the corpus PDFs from Markdown sources.")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--out", type=Path, default=None, help="default: <corpus>/pdf")
    args = parser.parse_args(argv)
    try:
        written = build_all(args.corpus, args.out)
    except (OSError, KeyError, ValueError, yaml.YAMLError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_ERROR
    for path in written:
        print(path)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
