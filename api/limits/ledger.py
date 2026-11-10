"""Ledger of the estimated cost of every LLM call (CT-67, DEMO-03, AS-2).

Contract (orchestrator skeleton; TASK-142 implements it without changing the signature):

- `record_llm_call(*, trace_id, provider, model, cost_usd, created_at)`: inserts one `LlmCall`
  row and commits it in its **own** session (`api.db.engine.get_sessionmaker()`), so the row
  exists even when the request's transaction rolls back, and the spend cap sees it at once.
- Never raises: any error (database down, bad value) only logs the error event
  `llm_ledger_failed` with the error class, never the row values, and the LLM answer is
  returned as usual.
- Called by `InstrumentedLLMClient` (chat, summary, triage, topic judge) when
  `Settings.llm_ledger_enabled` is true, for every call that reached the provider: with the cost
  of its usage, or, when it was cancelled or failed before any usage (Anthropic and OpenAI send
  it only at the end of a stream), with an input-only lower bound (locally counted input tokens
  at the model's input price; output tokens already streamed are not counted). A call the
  provider rejected (non-recoverable 4xx) or that failed locally before it was sent is not
  recorded. A call whose cost is unknown (subscription CLI or fake provider,
  `UnknownModelPricing`) is recorded with `cost_usd = 0` and the warning event
  `llm_cost_unknown` (provider and model only).
- The cost is rounded **up** to the column's 6 decimal places (`COST_QUANTUM`), so the ledger
  never under-reports what was spent.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import ROUND_CEILING, Decimal

from api.db.engine import get_sessionmaker
from api.limits.models import LlmCall
from api.observability.logging import LOGGER_NAME

LEDGER_FAILED_EVENT = "llm_ledger_failed"
COST_UNKNOWN_EVENT = "llm_cost_unknown"
# `llm_calls.cost_usd` is NUMERIC(12,6): the cost is rounded up to it, so the spend cap never
# sees less than was spent (the database alone would round half away from zero).
COST_QUANTUM = Decimal("0.000001")


def log_cost_unknown(*, provider: str, model: str) -> None:
    """Warning `llm_cost_unknown`: the call is recorded with cost 0 (provider and model only)."""
    logging.getLogger(LOGGER_NAME).warning(
        COST_UNKNOWN_EVENT,
        extra={"event": COST_UNKNOWN_EVENT, "provider": provider, "model": model},
    )


async def record_llm_call(
    *,
    trace_id: str,
    provider: str,
    model: str,
    cost_usd: Decimal,
    created_at: datetime,
) -> None:
    try:
        # NUMERIC accepts NaN and negatives, which would poison or shrink the spend cap's sum.
        if not cost_usd.is_finite() or cost_usd < 0:
            raise ValueError("cost_usd must be a finite, non-negative amount")
        stored_cost = cost_usd.quantize(COST_QUANTUM, rounding=ROUND_CEILING)
        async with get_sessionmaker()() as session:
            session.add(
                LlmCall(
                    trace_id=trace_id,
                    provider=provider,
                    model=model,
                    cost_usd=stored_cost,
                    created_at=created_at,
                )
            )
            await session.commit()
    except Exception as exc:
        # Only the error class: the message may carry the row values or the database host.
        logging.getLogger(LOGGER_NAME).error(
            LEDGER_FAILED_EVENT,
            extra={"event": LEDGER_FAILED_EVENT, "error_type": type(exc).__name__},
        )
