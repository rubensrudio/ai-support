"""Ledger of the estimated cost of every LLM call (CT-67, DEMO-03, AS-2).

Contract (orchestrator skeleton; TASK-142 implements it without changing the signature):

- `record_llm_call(*, trace_id, provider, model, cost_usd, created_at)`: inserts one `LlmCall`
  row and commits it in its **own** session (`api.db.engine.get_sessionmaker()`), so the row
  exists even when the request's transaction rolls back, and the spend cap sees it at once.
- Never raises: any error (database down, bad value) only logs the error event
  `llm_ledger_failed` with the error class, never the row values, and the LLM answer is
  returned as usual.
- Called by `InstrumentedLLMClient` after every `complete()` and every finished `stream()`
  (chat, summary, triage, topic judge) when `Settings.llm_ledger_enabled` is true. A call whose
  cost is unknown (subscription CLI or fake provider, `UnknownModelPricing`) is recorded with
  `cost_usd = 0` and the warning event `llm_cost_unknown` (provider and model only).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

LEDGER_FAILED_EVENT = "llm_ledger_failed"
COST_UNKNOWN_EVENT = "llm_cost_unknown"


async def record_llm_call(
    *,
    trace_id: str,
    provider: str,
    model: str,
    cost_usd: Decimal,
    created_at: datetime,
) -> None:
    raise NotImplementedError("TASK-142")
