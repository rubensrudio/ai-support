"""Ingestion report (CT-28, RAG-02, RAG-91).

`IngestReport{documents: [{doc_key, language, doc_type, format, status, chunks, error}],
totals: {indexed, quarantined, failed}}`, written as JSON by `python -m ingest run --report`.
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from api.rag.models import DOC_STATUSES


@dataclass(frozen=True)
class DocumentReport:
    doc_key: str
    language: str
    doc_type: str
    format: str
    status: str
    chunks: int
    error: str | None = None


@dataclass
class IngestReport:
    documents: list[DocumentReport] = field(default_factory=list)

    def totals(self) -> dict[str, int]:
        counts = dict.fromkeys(DOC_STATUSES, 0)
        for document in self.documents:
            counts[document.status] = counts.get(document.status, 0) + 1
        return counts

    @property
    def has_failures(self) -> bool:
        return any(document.status == "failed" for document in self.documents)

    def to_dict(self) -> dict[str, object]:
        return {
            "documents": [asdict(document) for document in self.documents],
            "totals": self.totals(),
        }


def write_report(report: IngestReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
