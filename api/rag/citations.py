"""Citation extraction (CT-32, TASK-048).

Markers are `[n]`, 1-based over the chunks given to the model, also grouped as `[1, 2]` or
chained as `[1][2]`. Unknown indices (`[0]`, `[9]` with 3 chunks) are dropped, each index is
cited once, in the order of its first occurrence in the answer.
"""

import re

from api.chat.schemas import Citation
from api.rag.retriever import RetrievedChunk

_MARKER = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


def extract_citations(answer: str, chunks: list[RetrievedChunk]) -> list[Citation]:
    citations: list[Citation] = []
    seen: set[int] = set()
    for marker in _MARKER.finditer(answer):
        for number in marker.group(1).split(","):
            index = int(number)
            if index in seen or not 1 <= index <= len(chunks):
                continue
            seen.add(index)
            chunk = chunks[index - 1]
            citations.append(
                Citation(
                    index=index,
                    document=chunk.document_title,
                    doc_key=chunk.doc_key,
                    section=chunk.section,
                    page=chunk.page,
                )
            )
    return citations
