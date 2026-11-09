"""Token-based chunker (TASK-044, RAG-01, P-6).

A section that fits in `CHUNK_SIZE_TOKENS` becomes one chunk, never cut. A longer section is
split into overlapping windows on whitespace boundaries: the number of windows is the minimum
needed for `CHUNK_SIZE_TOKENS` with `CHUNK_OVERLAP_TOKENS` of overlap, and the windows are
balanced so the last one is not a tiny tail. Tokens come from `api.llm.tokens.count_tokens`
(CT-8) and every chunk is checked against the limit with the exact count of its own text.

Sections are never merged with their neighbours to fill the 500-token budget: a small
section becomes a small chunk, so each chunk keeps exactly one `section` (or `page`) for its
citation metadata.
"""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from api.llm.tokens import count_tokens
from ingest.parsers import Section

CHUNK_SIZE_TOKENS = 500
CHUNK_OVERLAP_TOKENS = 80
# Pieces bigger than this (long unbroken strings) are cut by characters before windowing.
_MAX_PIECE_TOKENS = 50
_FALLBACK_PIECE_CHARS = 40

# Leading whitespace stays with the word, as in the tokenizer, so piece counts add up.
_PIECE = re.compile(r"\s*\S+")


@dataclass(frozen=True)
class TextChunk:
    ordinal: int
    content: str
    section: str | None
    page: int | None
    token_count: int


def chunk_sections(
    sections: Sequence[Section],
    *,
    size: int = CHUNK_SIZE_TOKENS,
    overlap: int = CHUNK_OVERLAP_TOKENS,
) -> list[TextChunk]:
    if not 0 <= overlap < size:
        raise ValueError("overlap must be >= 0 and smaller than size")
    chunks: list[TextChunk] = []
    for section in sections:
        for content in split_text(section.text, size=size, overlap=overlap):
            chunks.append(
                TextChunk(
                    ordinal=len(chunks),
                    content=content,
                    section=section.section,
                    page=section.page,
                    token_count=count_tokens(content),
                )
            )
    return chunks


def split_text(text: str, *, size: int, overlap: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    total = count_tokens(text)
    if total <= size:
        return [text]

    pieces = _pieces(text)
    counts = [count_tokens(piece) for piece in pieces]
    windows = math.ceil((total - overlap) / (size - overlap))
    target = min(size, math.ceil((total + (windows - 1) * overlap) / windows))

    result: list[str] = []
    start = 0
    while start < len(pieces):
        end = start
        budget = 0
        while end < len(pieces) and (end == start or budget + counts[end] <= target):
            budget += counts[end]
            end += 1
        if sum(counts[start:]) <= size:  # the rest fits: close with one window
            end = len(pieces)
        content = "".join(pieces[start:end]).strip()
        while count_tokens(content) > size and end - start > 1:
            end -= 1
            content = "".join(pieces[start:end]).strip()
        result.append(content)
        if end >= len(pieces):
            break
        start = _overlap_start(counts, start, end, overlap)
    return result


def _overlap_start(counts: list[int], start: int, end: int, overlap: int) -> int:
    """First piece of the next window: the shortest tail of [start, end) with `overlap` tokens."""
    tail = 0
    index = end
    while index > start + 1 and tail < overlap:
        index -= 1
        tail += counts[index]
    return index


def _pieces(text: str) -> list[str]:
    pieces: list[str] = []
    for piece in _PIECE.findall(text):
        if count_tokens(piece) <= _MAX_PIECE_TOKENS:
            pieces.append(piece)
            continue
        pieces.extend(
            piece[i : i + _FALLBACK_PIECE_CHARS]
            for i in range(0, len(piece), _FALLBACK_PIECE_CHARS)
        )
    return pieces
