"""Sentence-buffered streaming output filter (CT-75, DA-22, GRD-04, GRD-08).

`feed(delta)` buffers deltas and returns only complete, approved sentences, in order; the first
sentence is also topic-checked. The first blocked sentence stops all further release: `feed`
returns `[]` from then on and `finish()` returns `blocked=True` with the reason, so the service
replaces the whole answer with the standard message. `finish()` checks and releases the trailing
text without final punctuation.
"""

from dataclasses import dataclass

from api.guardrails.output_guard import OutputContext, OutputGuard


@dataclass(frozen=True)
class FilterOutcome:
    blocked: bool
    released: str
    reason: str | None = None


class StreamOutputFilter:
    def __init__(self, guard: OutputGuard, ctx: OutputContext) -> None:
        self.guard = guard
        self.ctx = ctx

    async def feed(self, delta: str) -> list[str]:
        raise NotImplementedError("TASK-104")

    async def finish(self) -> FilterOutcome:
        raise NotImplementedError("TASK-104")
