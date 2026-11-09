"""Corpus document parsers (TASK-044, RAG-01, RAG-91).

Markdown is split by ATX headings (`#` .. `######`, outside fenced code blocks): each heading
opens a section named after it, and the heading line stays in the section text so full-text
search can match it. PDF is split by page with `pypdf`: one section per page, numbered from 1.
A file that cannot be read or yields no text raises `UnreadableDocument`.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(?P<title>.+?)[ \t#]*$")
_FENCE = re.compile(r"^ {0,3}(```|~~~)")


class UnreadableDocument(Exception):
    """The source file is missing, corrupted, in an unsupported format or has no text."""


@dataclass(frozen=True)
class Section:
    text: str
    section: str | None = None
    page: int | None = None


@dataclass(frozen=True)
class ParsedDocument:
    title: str | None
    sections: list[Section]


def parse_markdown_text(text: str) -> ParsedDocument:
    title: str | None = None
    sections: list[Section] = []
    current_name: str | None = None
    current_lines: list[str] = []
    in_fence = False

    def flush() -> None:
        body = "\n".join(current_lines).strip()
        heading_only = current_name is not None and len(body.splitlines()) <= 1
        if body and not heading_only:
            sections.append(Section(text=body, section=current_name))

    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        heading = None if in_fence else _HEADING.match(line)
        if heading is None:
            current_lines.append(line)
            continue
        flush()
        current_name = heading.group("title").strip()
        current_lines = [line.strip()]
        if title is None and len(heading.group(1)) == 1:
            title = current_name

    flush()
    return ParsedDocument(title=title, sections=sections)


def parse_markdown(path: Path) -> ParsedDocument:
    try:
        text = path.read_bytes().decode("utf-8")
    except OSError as error:
        raise UnreadableDocument(f"cannot read {path.name}: {error.strerror}") from error
    except UnicodeDecodeError as error:
        raise UnreadableDocument(f"{path.name} is not valid UTF-8") from error
    parsed = parse_markdown_text(text)
    if not parsed.sections:
        raise UnreadableDocument(f"{path.name} has no text")
    return parsed


def parse_pdf(path: Path) -> ParsedDocument:
    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise UnreadableDocument(f"{path.name} is encrypted")
        pages = [page.extract_text() or "" for page in reader.pages]
        metadata_title = reader.metadata.title if reader.metadata else None
    except UnreadableDocument:
        raise
    except Exception as error:  # pypdf raises many types for malformed files
        raise UnreadableDocument(f"cannot read PDF {path.name}: {error}") from error

    sections = [
        Section(text=text.strip(), page=number)
        for number, text in enumerate(pages, start=1)
        if text.strip()
    ]
    if not sections:
        raise UnreadableDocument(f"{path.name} has no extractable text")
    title = metadata_title.strip() if isinstance(metadata_title, str) else None
    return ParsedDocument(title=title or None, sections=sections)


def parse_document(path: Path, fmt: str) -> ParsedDocument:
    """Parse `path` according to the manifest format (`md` or `pdf`)."""
    if fmt == "md":
        return parse_markdown(path)
    if fmt == "pdf":
        if not path.is_file():
            raise UnreadableDocument(f"cannot read {path.name}: file not found")
        return parse_pdf(path)
    raise UnreadableDocument(f"unsupported format {fmt!r} for {path.name}")
