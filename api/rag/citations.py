"""Citation extraction (CT-32). Skeleton stub: implemented by TASK-048.

Markers are `[n]`, 1-based over the chunks given to the model; unknown indices are dropped.
"""

from api.chat.schemas import Citation
from api.rag.retriever import RetrievedChunk


def extract_citations(answer: str, chunks: list[RetrievedChunk]) -> list[Citation]:
    raise NotImplementedError("TASK-048")
