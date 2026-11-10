"""Ingestion pipeline (TASK-045, CT-28, RAG-01, RAG-02, RAG-14, RAG-91, RAG-92).

Reads `corpus/manifest.yaml` and processes every `(doc_key, language)` in a transaction of its
own, so one bad file never stops the others (RAG-91). Re-ingestion is idempotent (RAG-92): a
document whose `content_hash` did not change keeps its chunks; a changed one has its chunks
deleted and inserted again. Each chunk stores its language and `ts_config = TS_CONFIGS[lang]`
(DA-13).

Injection scan (phase 10, GRD-03, GRD-91): with `ATLAS_INGEST_INJECTION_SCAN` (default true, or
`scan_injection` of `ingest_corpus`), the full parsed text of each document is checked with
`find_injection` before anything is embedded or indexed. A match stores the document
`quarantined` with `error='injection: <pattern names>'`, deletes any chunk it had (from a
previous run too), never calls the embedder and counts it in `totals.quarantined`. A quarantined
document is never "unchanged" (only `indexed` ones are), so every run scans it again: while it
matches it stays quarantined; once it no longer matches (or the scan is off) it is indexed.
There is no release flow: fixing the source file is the way out of quarantine.

Embeddings (phase 04, RAG-07, RAG-93): with an `embedder` and without `skip_embeddings`, the
vectors of a document are computed before its write, in one batched `embed` call per document,
so every stored chunk has an embedding. An unchanged document keeps its chunks and only the
ones whose `embedding` is null (ingested in phase 03 or with `--skip-embeddings`) are embedded;
when none is null the embedder is not called. Any embedder failure marks the document `failed`
with `embedding failed: <exception type>` (never the provider message), drops its chunks and
leaves it reprocessable by the next run. Without an embedder, or with `skip_embeddings`, chunks
are stored with a null embedding, as in phase 03. With `reembed` (to switch embedders, e.g. fake
-> OpenAI, without mixing vectors), every chunk of the manifest documents is embedded again,
including the ones that already have an embedding. A `reembed` run is all-or-nothing per
document but not per run: the first embedder failure raises `ReembedAborted` before anything of
that document is written, so it and every document after it keep their chunks, old vectors and
status (the index, full-text included, stays usable); the documents already processed in that
run keep their new vectors, and running `--reembed` again finishes the switch. Outside
`reembed`, an embedder failure fails only its document (RAG-93).

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
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.config import get_settings
from api.guardrails.patterns import find_injection
from api.observability.logging import log_event
from api.rag.embeddings import EmbeddingClient, EmbeddingError
from api.rag.models import DOC_FORMATS, DOC_TYPES, TS_CONFIGS, Chunk, Document
from ingest.chunking import TextChunk, chunk_sections
from ingest.parsers import UnreadableDocument, parse_document
from ingest.report import DocumentReport, IngestReport

MANIFEST_NAME = "manifest.yaml"
DEFAULT_LANGUAGES = ("en", "pt")
REMOVED_FROM_MANIFEST = "removed from manifest"
INJECTION_ERROR_PREFIX = "injection: "
# Alternative PDF layouts accepted after the default `pdf/<lang>/<doc_key>.pdf`.
_PDF_LAYOUTS = ("pdf/{lang}/{key}.pdf", "pdf/{key}.{lang}.pdf", "pdf/{key}-{lang}.pdf")


class ManifestError(Exception):
    """The manifest is missing, is not valid YAML or has an entry without `doc_key`."""


class ReembedAborted(RuntimeError):
    """A `reembed` run stopped at the first embedder failure, leaving the rest untouched.

    The message names only the exception type (`re-embedding aborted: <type>`), never the
    provider response or the chunk text.
    """

    def __init__(self, error_type: str) -> None:
        super().__init__(f"re-embedding aborted: {error_type}")
        self.error_type = error_type


class EmbeddingsOutOfDate(RuntimeError):
    """The stored document changed between computing its embeddings and writing them."""


@dataclass(frozen=True)
class _Embeddings:
    """Vectors computed before a document is written.

    `fresh` holds one vector per parsed chunk, for a document whose chunks are (re)written;
    `missing` maps the id of each stored chunk without embedding of an unchanged document to
    its vector (empty when every stored chunk already has one).
    """

    fresh: list[list[float]] | None = None
    missing: Mapping[int, list[float]] = field(default_factory=dict)


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
    embedder: EmbeddingClient | None = None,
    reembed: bool = False,
    scan_injection: bool | None = None,
) -> IngestReport:
    """Ingest every manifest entry × language, embedding the chunks unless told not to.

    `reembed` recomputes the embedding of every chunk and needs an `embedder`; it cannot be
    combined with `skip_embeddings`. It raises `ReembedAborted` at the first embedder failure.
    `scan_injection` quarantines the documents whose text matches an injection pattern; `None`
    follows `settings.ingest_injection_scan` (`ATLAS_INGEST_INJECTION_SCAN`).
    """
    if reembed and (skip_embeddings or embedder is None):
        raise ValueError("reembed needs an embedder and cannot be combined with skip_embeddings")
    if skip_embeddings:
        embedder = None
    if scan_injection is None:
        scan_injection = get_settings().ingest_injection_scan
    report = IngestReport()
    entries = load_manifest(corpus_dir)
    for entry in entries:
        for language in entry.languages:
            path = source_path(corpus_dir, entry, language)
            report.documents.append(
                _logged(
                    await _ingest_one(
                        session_factory,
                        corpus_dir,
                        entry,
                        language,
                        path,
                        embedder,
                        reembed,
                        scan_injection=scan_injection,
                    )
                )
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
    embedder: EmbeddingClient | None,
    reembed: bool = False,
    *,
    scan_injection: bool = True,
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
        sections = parse_document(path, entry.format).sections
    except UnreadableDocument as error:
        return await _record_failure(
            session_factory, entry, language, stored_path, content_hash, str(error)
        )

    if scan_injection:  # GRD-03: before any embedding or indexing
        patterns = find_injection("\n\n".join(section.text for section in sections))
        if patterns:
            message = INJECTION_ERROR_PREFIX + ", ".join(patterns)
            return await _record_failure(
                session_factory,
                entry,
                language,
                stored_path,
                content_hash,
                message,
                status="quarantined",
            )
    chunks = chunk_sections(sections)

    embeddings: _Embeddings | None = None
    if embedder is not None:
        try:
            missing = await _chunks_to_embed(
                session_factory, entry.doc_key, language, content_hash, reembed=reembed
            )
        except Exception as error:  # same isolation and generic message as a storage error
            message = f"cannot store {stored_path}: {type(error).__name__}"
            return await _record_failure(
                session_factory, entry, language, stored_path, content_hash, message
            )
        try:
            embeddings = await _embed(embedder, chunks, missing)
        except Exception as error:  # RAG-93: never the provider message, nor the chunk text
            if reembed:  # stop before dropping a working index over a provider outage
                log_event(
                    "ingest_reembed_aborted",
                    doc_key=entry.doc_key,
                    language=language,
                    error_type=type(error).__name__,
                )
                raise ReembedAborted(type(error).__name__) from None
            message = f"embedding failed: {type(error).__name__}"
            return await _record_failure(
                session_factory, entry, language, stored_path, content_hash, message
            )

    try:
        count = await _store(
            session_factory, entry, language, stored_path, content_hash, chunks, embeddings
        )
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


async def _chunks_to_embed(
    session_factory: async_sessionmaker[AsyncSession],
    doc_key: str,
    language: str,
    content_hash: str,
    *,
    reembed: bool,
) -> list[tuple[int, str]] | None:
    """`(id, content)` of the stored chunks to embed when the document is unchanged.

    Those without embedding, or all of them with `reembed`. `None` when its chunks will be
    (re)written: new, changed or previously failed document.
    """
    async with session_factory() as session:
        document = await _find(session, doc_key, language)
        if document is None or not _is_unchanged(document, content_hash):
            return None
        query = select(Chunk.id, Chunk.content).where(Chunk.document_id == document.id)
        if not reembed:
            query = query.where(Chunk.embedding.is_(None))
        rows = await session.execute(query.order_by(Chunk.ordinal))
        return [(row.id, row.content) for row in rows]


async def _embed(
    embedder: EmbeddingClient,
    chunks: Sequence[TextChunk],
    missing: list[tuple[int, str]] | None,
) -> _Embeddings:
    """One `embed` call for the texts that need a vector; none when no text does."""
    if missing is None:
        return _Embeddings(fresh=await _vectors(embedder, [chunk.content for chunk in chunks]))
    vectors = await _vectors(embedder, [content for _, content in missing])
    return _Embeddings(
        missing={chunk_id: vector for (chunk_id, _), vector in zip(missing, vectors, strict=True)}
    )


async def _vectors(embedder: EmbeddingClient, texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    vectors = await embedder.embed(texts)
    if len(vectors) != len(texts):
        raise EmbeddingError("the provider returned a different number of vectors")
    return vectors


def _is_unchanged(document: Document, content_hash: str) -> bool:
    return document.status == "indexed" and document.content_hash == content_hash


async def _store(
    session_factory: async_sessionmaker[AsyncSession],
    entry: ManifestEntry,
    language: str,
    stored_path: str,
    content_hash: str,
    chunks: Sequence[TextChunk],
    embeddings: _Embeddings | None = None,
) -> int:
    async with session_factory() as session, session.begin():
        document = await _find(session, entry.doc_key, language)
        if document is not None and _is_unchanged(document, content_hash):
            # Unchanged: keep its chunks (RAG-92), only filling the missing embeddings.
            _apply_metadata(document, entry, language, stored_path)
            if embeddings is not None:
                await _fill_embeddings(session, document.id, embeddings)
            return await _count_chunks(session, document.id)
        vectors = None if embeddings is None else embeddings.fresh
        if embeddings is not None and vectors is None:
            raise EmbeddingsOutOfDate  # it changed after its stored chunks were embedded
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
                embedding=None if vectors is None else vectors[index],
            )
            for index, chunk in enumerate(chunks)
        )
        return len(chunks)


async def _fill_embeddings(
    session: AsyncSession, document_id: int, embeddings: _Embeddings
) -> None:
    """Store the planned vectors (replacing any old one); every chunk must end with one."""
    if embeddings.fresh is not None:
        raise EmbeddingsOutOfDate  # computed for a rewrite, but the document is unchanged now
    for chunk_id, vector in embeddings.missing.items():
        await session.execute(
            update(Chunk)
            .where(Chunk.id == chunk_id, Chunk.document_id == document_id)
            .values(embedding=vector)
        )
    still_missing = (
        await session.execute(
            select(func.count())
            .select_from(Chunk)
            .where(Chunk.document_id == document_id, Chunk.embedding.is_(None))
        )
    ).scalar_one()
    if still_missing:
        raise EmbeddingsOutOfDate


async def _record_failure(
    session_factory: async_sessionmaker[AsyncSession],
    entry: ManifestEntry,
    language: str,
    stored_path: str,
    content_hash: str,
    message: str,
    *,
    status: str = "failed",
) -> DocumentReport:
    """Mark the document failed (or quarantined) and drop its old chunks.

    Stale text is never retrieved: neither the chunks of a document that now fails nor those of
    one that is now quarantined.
    """
    report = DocumentReport(
        doc_key=entry.doc_key,
        language=language,
        doc_type=entry.doc_type,
        format=entry.format,
        status=status,
        chunks=0,
        error=message,
    )
    try:
        async with session_factory() as session, session.begin():
            document = await _find(session, entry.doc_key, language)
            document = await _upsert(
                session, document, entry, language, stored_path, content_hash, status, message
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
