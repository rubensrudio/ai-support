"""Ingestion CLI (CT-28).

`python -m ingest run [--corpus corpus] [--report reports/ingest-report.json] [--skip-embeddings]`
ingests the corpus and writes the JSON report; exit 0 when no document failed, 1 otherwise
(also 1 when the manifest cannot be read). `python -m ingest reindex-hnsw` arrives in phase 05.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from api.config import get_settings
from api.db.engine import dispose_engine, get_sessionmaker
from api.observability.logging import configure_logging
from ingest.pipeline import ManifestError, ingest_corpus
from ingest.report import IngestReport, write_report

EXIT_OK = 0
EXIT_FAILED = 1
DEFAULT_CORPUS = Path("corpus")
DEFAULT_REPORT = Path("reports/ingest-report.json")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ingest", description="Atlas corpus ingestion")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="ingest the corpus listed in the manifest")
    run.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS, help="corpus folder")
    run.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="report JSON to write")
    run.add_argument(
        "--skip-embeddings", action="store_true", help="do not compute embeddings (phase 04)"
    )
    commands.add_parser("reindex-hnsw", help="rebuild the HNSW index (phase 05)")
    return parser


async def _ingest(corpus: Path, skip_embeddings: bool) -> IngestReport:
    try:
        return await ingest_corpus(corpus, get_sessionmaker(), skip_embeddings=skip_embeddings)
    finally:
        await dispose_engine()


def _run(corpus: Path, report_path: Path, skip_embeddings: bool) -> int:
    try:
        report = asyncio.run(_ingest(corpus, skip_embeddings))
    except ManifestError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_FAILED
    write_report(report, report_path)
    totals = report.totals()
    print(
        f"ingested {len(report.documents)} documents: "
        + ", ".join(f"{status}={count}" for status, count in totals.items())
        + f" (report: {report_path})"
    )
    for document in report.documents:
        if document.status == "failed":
            print(f"failed: {document.doc_key} [{document.language}]: {document.error}")
    return EXIT_FAILED if report.has_failures else EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    configure_logging(get_settings())
    if args.command == "run":
        return _run(args.corpus, args.report, args.skip_embeddings)
    print("reindex-hnsw is not available until phase 05")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
