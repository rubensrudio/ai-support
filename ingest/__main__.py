"""Ingestion CLI (CT-28).

`python -m ingest run [--corpus corpus] [--report reports/ingest-report.json]
[--skip-embeddings | --reembed]` ingests the corpus and writes the JSON report; exit 0 when no
document failed, 1 otherwise (also 1 when the manifest cannot be read).

Without `--skip-embeddings` the chunks are embedded by `build_embedder(settings)`
(`ATLAS_EMBEDDING_PROVIDER`). With the `openai` provider and no `OPENAI_API_KEY`, the command
stops with exit 1 before opening the database: running would mark every document that needs an
embedding as failed and drop its chunks. `--skip-embeddings` builds no embedder and the chunks
keep a null embedding. `--reembed` recomputes the embedding of every chunk of the manifest
documents, to switch embedders without mixing vectors; it cannot be combined with
`--skip-embeddings` (usage error, exit 2). At the first embedder failure it stops with
`error: re-embedding aborted: <exception type>` and exit 1 without writing the report: the
failing document and the ones after it are untouched, the ones before it already have the new
vectors, so run `--reembed` again once the provider is back.

`python -m ingest reindex-hnsw` drops and recreates the chunks HNSW index with the current
`ATLAS_HNSW_M` and `ATLAS_HNSW_EF_CONSTRUCTION` (RAG-10) and prints the new definition; exit 0 on
success, 1 on a database error (generic message on stderr, no SQL).
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from api.config import MissingConfigError, Settings, get_settings
from api.db.engine import dispose_engine, get_sessionmaker
from api.observability.logging import configure_logging
from api.rag.embeddings import build_embedder
from ingest.pipeline import ManifestError, ReembedAborted, ingest_corpus
from ingest.reindex import reindex_hnsw
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
    embeddings = run.add_mutually_exclusive_group()
    embeddings.add_argument(
        "--skip-embeddings", action="store_true", help="store the chunks without embeddings"
    )
    embeddings.add_argument(
        "--reembed",
        action="store_true",
        help="recompute the embedding of every chunk (e.g. after switching embedders)",
    )
    commands.add_parser(
        "reindex-hnsw",
        help="rebuild the chunks HNSW index with the configured m and ef_construction",
    )
    return parser


def _require_embedding_config(settings: Settings) -> None:
    """Fail fast when the embedding provider lacks its key, before any document is touched."""
    if settings.embedding_provider != "openai":
        return
    key = settings.openai_api_key
    if key is None or not key.get_secret_value().strip():
        raise MissingConfigError("OPENAI_API_KEY")


async def _ingest(corpus: Path, skip_embeddings: bool, reembed: bool) -> IngestReport:
    embedder = None if skip_embeddings else build_embedder(get_settings())
    try:
        return await ingest_corpus(
            corpus,
            get_sessionmaker(),
            skip_embeddings=skip_embeddings,
            embedder=embedder,
            reembed=reembed,
        )
    finally:
        await dispose_engine()


def _run(corpus: Path, report_path: Path, skip_embeddings: bool, reembed: bool = False) -> int:
    if not skip_embeddings:
        try:
            _require_embedding_config(get_settings())
        except MissingConfigError as error:
            print(f"error: {error} (or use --skip-embeddings)", file=sys.stderr)
            return EXIT_FAILED
    try:
        report = asyncio.run(_ingest(corpus, skip_embeddings, reembed))
    except (ManifestError, ReembedAborted) as error:
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


async def _reindex() -> str:
    try:
        return await reindex_hnsw(get_sessionmaker(), get_settings())
    finally:
        await dispose_engine()


def _reindex_hnsw() -> int:
    try:
        indexdef = asyncio.run(_reindex())
    except (SQLAlchemyError, OSError):
        print("error: could not rebuild the HNSW index (database error)", file=sys.stderr)
        return EXIT_FAILED
    print(f"rebuilt {indexdef}")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    configure_logging(get_settings())
    if args.command == "run":
        return _run(args.corpus, args.report, args.skip_embeddings, args.reembed)
    return _reindex_hnsw()


if __name__ == "__main__":
    sys.exit(main())
