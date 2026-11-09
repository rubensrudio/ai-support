"""Language-aware full-text search (CT-29, RAG-15).

`english` config for `en`, `portuguese` for `pt`; only chunks of that language, from documents
with status `indexed`, are searched. The query goes through `websearch_to_tsquery` with the
config of the question language (stop words, stemming, quoted phrases, `-term`), then its
top-level ANDs become ORs so a natural question matches chunks that share only some of its terms;
`ts_rank_cd` ranks chunks that cover more terms first. The query matches the generated
`chunks.tsv` column (DA-13).
"""

from dataclasses import dataclass

from sqlalchemy import Text, cast, func, literal, select
from sqlalchemy.dialects.postgresql import REGCONFIG, TSQUERY
from sqlalchemy.ext.asyncio import AsyncSession

from api.messages import Lang
from api.rag.models import TS_CONFIGS, Chunk, Document

# `&` joins the terms of a websearch query; `& !` introduces an excluded `-term` and stays AND.
# Lexemes are quoted (`'refund'`), so the pattern never matches inside one.
_AND_BETWEEN_TERMS = r" & (?!!)"


@dataclass(frozen=True)
class ScoredChunk:
    chunk_id: int
    score: float


async def search_fulltext(
    session: AsyncSession, query: str, language: Lang, k: int
) -> list[ScoredChunk]:
    """Return up to `k` chunks of `language` matching `query`, best score first.

    A query without searchable terms (empty, or only stop words) returns `[]`.
    """
    if k <= 0 or not query.strip():
        return []
    config = cast(literal(TS_CONFIGS[language]), REGCONFIG)
    parsed = cast(func.websearch_to_tsquery(config, query), Text)
    # Cast straight to tsquery (no `to_tsquery`): the lexemes are already normalised.
    ts_query = cast(func.regexp_replace(parsed, _AND_BETWEEN_TERMS, " | ", "g"), TSQUERY)
    score = func.ts_rank_cd(Chunk.tsv, ts_query).label("score")
    statement = (
        select(Chunk.id, score)
        .join(Document, Document.id == Chunk.document_id)
        .where(
            Chunk.language == language,
            Document.status == "indexed",
            Chunk.tsv.op("@@")(ts_query),
        )
        .order_by(score.desc(), Chunk.id)
        .limit(k)
    )
    rows = await session.execute(statement)
    return [ScoredChunk(chunk_id=chunk_id, score=float(value)) for chunk_id, value in rows]
