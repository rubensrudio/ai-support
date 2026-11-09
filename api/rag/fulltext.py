"""Language-aware full-text search (CT-29). Skeleton stub: implemented by TASK-046.

`english` config for `en`, `portuguese` for `pt`; only chunks of that language are searched.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from api.messages import Lang


@dataclass(frozen=True)
class ScoredChunk:
    chunk_id: int
    score: float


async def search_fulltext(
    session: AsyncSession, query: str, language: Lang, k: int
) -> list[ScoredChunk]:
    raise NotImplementedError("TASK-046")
