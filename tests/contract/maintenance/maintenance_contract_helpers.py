"""Shared types of the maintenance contract tests (unique module name, imported bare)."""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from api.config import Settings
from api.observability.langfuse_api import LangfuseApi

PERSONA_PASSWORD = "maintenance-contract-pass-5b2e"  # noqa: S105 - fictitious test credential


class FakeLangfuse(LangfuseApi):
    """Enabled Langfuse that records every cutoff it is asked to purge; never touches the network.

    The first call reports `traces` deleted traces, later calls report 0 (nothing left).
    """

    def __init__(self, settings: Settings, traces: int = 3) -> None:
        super().__init__(settings)
        self.cutoffs: list[datetime] = []
        self._traces = traces

    @property
    def enabled(self) -> bool:
        return True

    async def delete_traces_older_than(self, cutoff: datetime) -> int:
        self.cutoffs.append(cutoff)
        deleted, self._traces = self._traces, 0
        return deleted


@dataclass(frozen=True)
class Persona:
    id: uuid.UUID
    username: str
    email: str


@dataclass
class Rows:
    """Ids of the rows a test created, per table, to check what is left afterwards."""

    conversations: list[uuid.UUID] = field(default_factory=list)
    messages: list[int] = field(default_factory=list)
    feedback: list[int] = field(default_factory=list)
    confirmations: list[uuid.UUID] = field(default_factory=list)
    audit_rows: list[int] = field(default_factory=list)
    emails: list[int] = field(default_factory=list)
    tickets: list[uuid.UUID] = field(default_factory=list)
    llm_calls: list[int] = field(default_factory=list)


ActivityFn = Callable[[Persona, datetime], Awaitable[Rows]]
ExistingFn = Callable[[Rows], Awaitable[Rows]]
