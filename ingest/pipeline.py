"""Ingestion pipeline (TASK-045, CT-28, RAG-01, RAG-02, RAG-14, RAG-91, RAG-92).

Reads `corpus/manifest.yaml` and processes every `(doc_key, language)` in a transaction of its
own, so one bad file never stops the others (RAG-91). Re-ingestion is idempotent (RAG-92): a
document whose `content_hash` did not change keeps its chunks; a changed one has its chunks
deleted and inserted again. Each chunk stores its language and `ts_config = TS_CONFIGS[lang]`
(DA-13). Embeddings arrive in phase 04 and the injection scan (`quarantined`) in phase 10.

At the end of a run, every `(doc_key, language)` stored in the database but absent from the
manifest has its chunks deleted and is marked `failed` with `removed from manifest`, so it can
no longer be retrieved and the report shows it.

Manifest: a list of entries, or a mapping with a `documents` list. Each entry has `doc_key`,
`title` (a string, or one per language), `doc_type`, `format`, optional `languages`
(default `[en, pt]`) and `malicious`. The file ingested per language is `file.<lang>`
(relative to the corpus folder); without it, `paths`/`sources`/`source` are used when they
have the extension of the format, and then the defaults: `<lang>/<doc_key>.md` for `md` and
`pdf/<lang>/<doc_key>.pdf` for `pdf`.

Storage errors are recorded as `cannot store <path>: <exception type>` only: driver messages
can carry SQL and chunk text, so they never reach `documents.error` or the report.
"""

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.observability.logging import log_event
from api.rag.models import DOC_FORMATS, DOC_TYPES, TS_CONFIGS, Chunk, Document
from ingest.chunking import TextChunk, chunk_sections
from ingest.parsers import UnreadableDocument, parse_document
from ingest.report import DocumentReport, IngestReport

MANIFEST_NAME = "manifest.yaml"
DEFAULT_LANGUAGES = ("en", "pt")
REMOVED_FROM_MANIFEST = "removed from manifest"
# Alternative PDF layouts accepted after the default `pdf/<lang>/<doc_key>.pdf`.
_PDF_LAYOUTS = ("pdf/{lang}/{key}.pdf", "pdf/{key}.{lang}.pdf", "pdf/{key}-{lang}.pdf")


class ManifestError(Exception):
    """The manifest is missing, is not valid YAML or has an entry without `doc_key`."""


@dataclass(frozen=True)
class ManifestEntry:
    doc_key: str
    titles: Mapping[str, str]
    doc_type: str
    format: str
    languages: tuple[str, ...] = DEFAULT_LANGUAGES
    malicious: bool = False
    paths: Mapping[str, str] = field(default_factory=dict)
    files: Mapping[str, str] = field(default_factory=dict)

    def title(self, language: str) -> str:
        return self.titles.get(language) or self.titles.get("*") or self.doc_key


def load_manifest(corpus_dir: Path) -> list[ManifestEntry]:
    path = corpus_dir / MANIFEST_NAME
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ManifestError(f"cannot read {path}: {error.strerror}") from error
    except yaml.YAMLError as error:
        raise ManifestError(f"{path} is not valid YAML: {error}") from error

    items = raw.get("documents") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ManifestError(f"{path} must be a list of documents or have a 'documents' list")
    entries = [_entry(item, index) for index, item in enumerate(items)]
    keys = [entry.doc_key for entry in entries]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ManifestError(f"duplicated doc_key in manifest: {', '.join(duplicates)}")
    return entries


def _entry(item: object, index: int) -> ManifestEntry:
    if not isinstance(item, dict) or not isinstance(item.get("doc_key"), str):
        raise ManifestError(f"manifest entry #{index + 1} has no doc_key")
    title = item.get("title")
    titles = (
        {str(lang): str(value) for lang, value in title.items()}
        if isinstance(title, dict)
        else {"*": str(title)}
        if title
        else {}
    )
    languages = item.get("languages") or list(DEFAULT_LANGUAGES)
    paths = item.get("paths") or item.get("sources") or item.get("source") or {}
    files = item.get("file") or {}
    return ManifestEntry(
        doc_key=item["doc_key"],
        titles=titles,
        doc_type=str(item.get("doc_type", "")),
        format=str(item.get("format", "")),
        languages=tuple(str(language) for language in languages),
        malicious=bool(item.get("malicious", False)),
        paths=_per_language(paths),
        files=_per_language(files),
    )


def _per_language(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(language): str(path) for language, path in value.items() if path}


def source_path(corpus_dir: Path, entry: ManifestEntry, language: str) -> Path:
    declared = entry.files.get(language)
    if declared:  # the manifest names the file to ingest: use it as is
        return _resolve(corpus_dir, declared)
    explicit = entry.paths.get(language)
    if explicit and Path(explicit).suffix == f".{entry.format}":
        return _resolve(corpus_dir, explicit)
    if entry.format == "pdf":
        candidates = [
            corpus_dir / layout.format(lang=language, key=entry.doc_key) for layout in _PDF_LAYOUTS
        ]
        return next((path for path in candidates if path.exists()), candidates[0])
    return corpus_dir / language / f"{entry.doc_key}.{entry.format}"


def _resolve(corpus_dir: Path, relative: str) -> Path:
    """`relative` to the corpus folder, or to its parent when it starts with the folder name."""
    candidate = corpus_dir / relative
    if not candidate.exists() and Path(relative).parts[:1] == (corpus_dir.name,):
        candidate = corpus_dir.parent / relative
    return candidate


async def ingest_corpus(
    corpus_dir: Path,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    skip_embeddings: bool = False,
) -> IngestReport:
    """Ingest every manifest entry × language. `skip_embeddings` is a no-op until phase 04."""
    report = IngestReport()
    entries = load_manifest(corpus_dir)
    for entry in entries:
        for language in entry.languages:
            path = source_path(corpus_dir, entry, language)
            report.documents.append(
                _logged(await _ingest_one(session_factory, corpus_dir, entry, language, path))
            )
    listed = {(entry.doc_key, language) for entry in entries for language in entry.languages}
    for result in await _retire_unlisted(session_factory, listed):
        report.documents.append(_logged(result))
    return report


def _logged(result: DocumentReport) -> DocumentReport:
    log_event(
        "ingest_document",
        doc_key=result.doc_key,
        language=result.language,
        status=result.status,
        chunks=result.chunks,
    )
    return result


async def _retire_unlisted(
    session_factory: async_sessionmaker[AsyncSession], listed: set[tuple[str, str]]
) -> list[DocumentReport]:
    """Drop the chunks of stored documents no longer in the manifest and mark them failed."""
    async with session_factory() as session:
        stored = await session.execute(select(Document.id, Document.doc_key, Document.language))
        stale = [row.id for row in stored if (row.doc_key, row.language) not in listed]
    results: list[DocumentReport] = []
    for document_id in stale:  # one transaction each, like the manifest documents
        try:
            async with session_factory() as session, session.begin():
                document = await session.get(Document, document_id)
                if document is None:
                    continue
                await session.execute(delete(Chunk).where(Chunk.document_id == document.id))
                document.status = "failed"
                document.error = REMOVED_FROM_MANIFEST
                document.ingested_at = func.now()
                result = DocumentReport(
                    doc_key=document.doc_key,
                    language=document.language,
                    doc_type=document.doc_type,
                    format=document.format,
                    status="failed",
                    chunks=0,
                    error=REMOVED_FROM_MANIFEST,
                )
        except Exception as error:  # isolate it like any other document (RAG-91)
            log_event(
                "ingest_retire_failed", document_id=document_id, error_type=type(error).__name__
            )
            continue
        results.append(result)
    return results


async def _ingest_one(
    session_factory: async_sessionmaker[AsyncSession],
    corpus_dir: Path,
    entry: ManifestEntry,
    language: str,
    path: Path,
) -> DocumentReport:
    def result(status: str, chunks: int = 0, error: str | None = None) -> DocumentReport:
        return DocumentReport(
            doc_key=entry.doc_key,
            language=language,
            doc_type=entry.doc_type,
            format=entry.format,
            status=status,
            chunks=chunks,
            error=error,
        )

    invalid = _invalid_entry(entry, language)
    if invalid:  # the table CHECKs cannot hold it, so it is only reported
        return result("failed", error=invalid)

    stored_path = _stored_path(corpus_dir, path)
    try:
        data = path.read_bytes()
    except OSError as error:
        message = f"cannot read {stored_path}: {error.strerror}"
        return await _record_failure(session_factory, entry, language, stored_path, "", message)
    content_hash = hashlib.sha256(data).hexdigest()

    try:
        chunks = chunk_sections(parse_document(path, entry.format).sections)
    except UnreadableDocument as error:
        return await _record_failure(
            session_factory, entry, language, stored_path, content_hash, str(error)
        )

    try:
        count = await _store(session_factory, entry, language, stored_path, content_hash, chunks)
    except Exception as error:  # isolate any storage error to this document (RAG-91)
        # Never str(error): driver messages can carry SQL and chunk content.
        message = f"cannot store {stored_path}: {type(error).__name__}"
        return await _record_failure(
            session_factory, entry, language, stored_path, content_hash, message
        )
    return result("indexed", chunks=count)


def _invalid_entry(entry: ManifestEntry, language: str) -> str | None:
    if entry.format not in DOC_FORMATS:
        return f"unsupported format {entry.format!r} (expected one of {', '.join(DOC_FORMATS)})"
    if entry.doc_type not in DOC_TYPES:
        return f"unsupported doc_type {entry.doc_type!r} (expected one of {', '.join(DOC_TYPES)})"
    if language not in TS_CONFIGS:
        return f"unsupported language {language!r} (expected one of {', '.join(TS_CONFIGS)})"
    return None


def _stored_path(corpus_dir: Path, path: Path) -> str:
    """Source path as `<corpus folder>/<relative path>`, e.g. `corpus/en/refund-policy.md`."""
    try:
        return (Path(corpus_dir.name) / path.relative_to(corpus_dir)).as_posix()
    except ValueError:
        return path.as_posix()


async def _store(
    session_factory: async_sessionmaker[AsyncSession],
    entry: ManifestEntry,
    language: str,
    stored_path: str,
    content_hash: str,
    chunks: Sequence[TextChunk],
) -> int:
    async with session_factory() as session, session.begin():
        document = await _find(session, entry.doc_key, language)
        if document is not None and document.status == "indexed":
            if document.content_hash == content_hash:  # unchanged: keep its chunks (RAG-92)
                _apply_metadata(document, entry, language, stored_path)
                return await _count_chunks(session, document.id)
        document = await _upsert(
            session, document, entry, language, stored_path, content_hash, "indexed", None
        )
        await session.execute(delete(Chunk).where(Chunk.document_id == document.id))
        session.add_all(
            Chunk(
                document_id=document.id,
                ordinal=chunk.ordinal,
                content=chunk.content,
                language=language,
                section=chunk.section,
                page=chunk.page,
                token_count=chunk.token_count,
                ts_config=TS_CONFIGS[language],
            )
            for chunk in chunks
        )
        return len(chunks)


async def _record_failure(
    session_factory: async_sessionmaker[AsyncSession],
    entry: ManifestEntry,
    language: str,
    stored_path: str,
    content_hash: str,
    message: str,
) -> DocumentReport:
    """Mark the document failed and drop its old chunks, so stale text is never retrieved."""
    report = DocumentReport(
        doc_key=entry.doc_key,
        language=language,
        doc_type=entry.doc_type,
        format=entry.format,
        status="failed",
        chunks=0,
        error=message,
    )
    try:
        async with session_factory() as session, session.begin():
            document = await _find(session, entry.doc_key, language)
            document = await _upsert(
                session, document, entry, language, stored_path, content_hash, "failed", message
            )
            await session.execute(delete(Chunk).where(Chunk.document_id == document.id))
    except Exception as error:  # the report still records the failure
        log_event(
            "ingest_failure_not_recorded",
            doc_key=entry.doc_key,
            language=language,
            error_type=type(error).__name__,
        )
    return report


async def _find(session: AsyncSession, doc_key: str, language: str) -> Document | None:
    return (
        await session.execute(
            select(Document).where(Document.doc_key == doc_key, Document.language == language)
        )
    ).scalar_one_or_none()


async def _count_chunks(session: AsyncSession, document_id: int) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(Chunk).where(Chunk.document_id == document_id)
        )
    ).scalar_one()


def _apply_metadata(
    document: Document, entry: ManifestEntry, language: str, stored_path: str
) -> None:
    document.title = entry.title(language)
    document.doc_type = entry.doc_type
    document.format = entry.format
    document.source_path = stored_path


async def _upsert(
    session: AsyncSession,
    document: Document | None,
    entry: ManifestEntry,
    language: str,
    stored_path: str,
    content_hash: str,
    status: str,
    error: str | None,
) -> Document:
    if document is None:
        document = Document(doc_key=entry.doc_key, language=language)
        session.add(document)
    _apply_metadata(document, entry, language, stored_path)
    document.content_hash = content_hash
    document.status = status
    document.error = error
    document.ingested_at = func.now()
    await session.flush()
    return document
