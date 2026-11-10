"""Shared types for the feedback contract tests (uniquely named to avoid module collisions)."""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

PERSONA_PASSWORD = "feedback-contract-persona-pass-7c3a"  # noqa: S105 - fictitious credential


@dataclass
class FakeLangfuse:
    """Records every `push_score` (value and `score_id`); raises instead when `fail` is set."""

    fail: bool = False
    scores: list[tuple[str, int]] = field(default_factory=list)
    score_ids: list[str | None] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return True

    async def push_score(
        self,
        trace_id: str,
        value: int,
        comment: str | None = None,
        score_id: str | None = None,
    ) -> bool:
        if self.fail:
            raise RuntimeError("langfuse is down")
        self.scores.append((trace_id, value))
        self.score_ids.append(score_id)
        return True

    async def aclose(self) -> None:
        return None


@dataclass(frozen=True)
class SeededMessage:
    id: int
    trace_id: str
    customer_id: uuid.UUID


MessageFactory = Callable[..., Awaitable[SeededMessage]]
