"""Phase 07 demo: multi-step agent benchmark of 15 tasks, one step vs the default limit
(AGT-06, AGT-07, DOD-02).

1. **Tasks.** `demos/data/phase07_tasks.jsonl` holds 15 multi-step requests in pt and en, each
   with the persona that sends it (`alex` en, `bia` pt), the expected tool trajectory and the
   expected outcome (final status, the target order's status, whether it was refunded and the
   audited actions). Order numbers come from the table sequence and change on every reset, so
   messages and outcomes name orders by placeholders (`{paid_order_id}`, see `ORDER_REFS`)
   resolved against the persona's orders after each seed.
2. **Run.** For every task: reset the personas' commerce data (`seed_commerce`), resolve the
   placeholders, send the message through `ChatService` as the persona, and confirm every
   destructive call (`resolve_confirmation(..., 'confirm')`, the method behind
   `POST /chat/confirmations/{id}`) until the answer is `completed` or `limit_reached`. Every
   request runs in its own unit of work with a fresh trace id, as the HTTP middleware binds it.
   The trajectory is the sequence of `tool_start` events of all those requests; the outcome is
   read from the database: the order status, its refund and the successful `audit_log` rows of
   the task's trace ids (`list_successful_actions`).
3. **Metrics.** A task succeeds when its trajectory equals the expected one and its outcome
   matches. The demo reports the success rate and the mean of model calls (steps) per task.
   **Before** = the same tasks with `ATLAS_AGENT_MAX_STEPS=1`: the loop drops the tools asked
   by the call that used the last step, so no tool runs and every task ends `limit_reached`
   after one model call. **After** = the configured limit (default 8). The `| 07 |` row of
   `docs/results.md` gets both.

`--provider fake` scripts a `FakeLLMClient` per task that follows the expected trajectory (one
tool call per model step, then a final answer), so it proves the loop, the confirmations and
the limits, not the model's judgement. `real` uses the configured provider (including
`claude_cli`, which emulates tool calling) and reports what the model really did. A fake run
writes its results under `<tmp>/atlas-demo07/` unless `--results` is given, so it never replaces
the real `| 07 |` row. Retrieval is off: the demo is about the agent, not the corpus.

Warning: the demo resets the personas' orders, refunds and subscription in the configured
database (`ATLAS_DATABASE_URL`) before every task, exactly like the daily demo reset
(`python -m api.seed`). Conversations, messages, the outbox and the audit log are kept.

Usage:
    uv run python -m demos.demo_07_agent --provider fake --results /tmp/results.md
    uv run python -m demos.demo_07_agent --provider real   # writes docs/results.md
"""

import argparse
import asyncio
import json
import re
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select

from api.agent.events import AgentEvent, ChatStatus, ConfirmationPrompt, ToolStartEvent
from api.auth.deps import CurrentCustomer
from api.chat.schemas import ChatRequest, ChatResponse
from api.chat.service import ChatService
from api.config import Settings, get_settings
from api.db.engine import dispose_engine, session_scope
from api.llm.client import (
    LLMClient,
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    ToolCall,
    Usage,
)
from api.llm.fake import FAKE_MODEL, FAKE_PROVIDER
from api.messages import Lang
from api.observability.logging import bind_trace_id, new_trace_id, reset_trace_id
from api.seed.commerce import seed_commerce
from api.seed.personas import PERSONAS
from api.tools.catalog import TOOL_NAMES
from api.tools.models import AuditLog, Order, OrderStatus, Refund
from demos.common import DEFAULT_RESULTS_PATH, build_demo_llm, demo_parser, record_result
from demos.demo_01_stateless import seed_demo_principals

PHASE = 7
METRIC = "15 multi-step agent tasks (pt/en): success rate (trajectory and outcome) and avg steps"
TASKS_PATH = Path(__file__).resolve().parent / "data" / "phase07_tasks.jsonl"
BEFORE_MAX_STEPS = 1
LANGUAGES: tuple[Lang, ...] = ("en", "pt")
# Placeholders a task may use, resolved against the persona's seeded orders.
ORDER_REFS: tuple[str, ...] = (
    "pending_order_id",
    "paid_order_id",
    "shipped_order_id",
    "cancelled_order_id",
    "delivered_recent_order_id",  # delivered 3 days ago: refundable
    "delivered_old_order_id",  # delivered 10 days ago: outside the 7-day window
)
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_ORDER_TOOLS = frozenset({"get_order", "cancel_order", "check_refund_policy", "issue_refund"})
# Safety net for real providers: never confirm more than this per task.
_MAX_CONFIRMATIONS = 8
# Scripted texts of `--provider fake`.
_FAKE_FINAL: dict[Lang, str] = {
    "en": "Done: every step of your request is complete.",
    "pt": "Pronto: concluí todas as etapas do seu pedido.",
}
_FAKE_EMAIL: dict[Lang, tuple[str, str]] = {
    "en": ("Your Atlas request", "Here is the summary of what we did for your request."),
    "pt": ("Seu pedido no Atlas", "Aqui está o resumo do que fizemos no seu pedido."),
}


@dataclass(frozen=True)
class AgentTask:
    id: str
    language: Lang
    persona: str
    message: str
    expected_trajectory: tuple[str, ...]
    expected_outcome: dict[str, Any]


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    language: Lang
    expected_trajectory: tuple[str, ...]
    trajectory: tuple[str, ...]
    final_status: ChatStatus
    steps: int
    outcome_ok: bool
    outcome_detail: str

    @property
    def trajectory_ok(self) -> bool:
        return self.trajectory == self.expected_trajectory

    @property
    def succeeded(self) -> bool:
        return self.trajectory_ok and self.outcome_ok


@dataclass(frozen=True)
class RunSummary:
    successes: int
    total: int
    avg_steps: float
    by_language: dict[Lang, tuple[int, int]] = field(default_factory=dict)


# --- tasks -----------------------------------------------------------------------------------


def load_tasks(path: Path = TASKS_PATH) -> list[AgentTask]:
    """Read and check the task file: catalog tools only, known persona, pt/en."""
    personas = {persona.username: persona.language for persona in PERSONAS}
    tasks: list[AgentTask] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        raw = json.loads(line)
        trajectory = tuple(str(tool) for tool in raw["expected_trajectory"])
        unknown = set(trajectory) - set(TOOL_NAMES)
        if unknown:
            raise ValueError(f"{path}:{number}: unknown tools {sorted(unknown)}")
        if raw["language"] not in LANGUAGES or raw["persona"] not in personas:
            raise ValueError(f"{path}:{number}: unknown language or persona")
        tasks.append(
            AgentTask(
                id=str(raw["id"]),
                language=cast(Lang, raw["language"]),
                persona=str(raw["persona"]),
                message=str(raw["message"]),
                expected_trajectory=trajectory,
                expected_outcome=dict(raw["expected_outcome"]),
            )
        )
    return tasks


def resolve_refs(text: str, refs: dict[str, int]) -> str:
    """Replace `{<ref>}` placeholders with the seeded order numbers; unknown ones raise."""
    return _PLACEHOLDER.sub(lambda match: str(refs[match.group(1)]), text)


async def order_refs(principal: CurrentCustomer) -> dict[str, int]:
    """The persona's order numbers by role, looked up after every seed."""
    async with session_scope() as session:
        orders = list(
            await session.scalars(
                select(Order).where(Order.customer_id == principal.id).order_by(Order.id)
            )
        )
    by_status: dict[str, int] = {}
    delivered: list[Order] = []
    for order in orders:
        by_status.setdefault(order.status, order.id)
        if order.status == OrderStatus.DELIVERED.value and order.delivered_at is not None:
            delivered.append(order)
    delivered.sort(key=lambda order: cast(Any, order.delivered_at))
    if len(delivered) < 2:
        raise RuntimeError(f"Commerce seed missing delivered orders for {principal.id}")
    refs = {
        "pending_order_id": by_status.get(OrderStatus.PENDING.value),
        "paid_order_id": by_status.get(OrderStatus.PAID.value),
        "shipped_order_id": by_status.get(OrderStatus.SHIPPED.value),
        "cancelled_order_id": by_status.get(OrderStatus.CANCELLED.value),
        "delivered_recent_order_id": delivered[-1].id,
        "delivered_old_order_id": delivered[0].id,
    }
    missing = [name for name, value in refs.items() if value is None]
    if missing:
        raise RuntimeError(f"Commerce seed missing orders {missing} for {principal.id}")
    return {name: int(cast(int, value)) for name, value in refs.items()}


# --- fake provider ---------------------------------------------------------------------------


def _fake_response(text: str, calls: list[ToolCall] | None = None) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=list(calls or []),
        usage=Usage(input_tokens=0, output_tokens=0),
        model=FAKE_MODEL,
        provider=FAKE_PROVIDER,
        stop_reason="tool_use" if calls else "end_turn",
    )


def _fake_arguments(tool: str, task: AgentTask, refs: dict[str, int]) -> dict[str, Any]:
    if tool == "list_orders":
        return {"limit": 10}
    if tool == "send_email":
        subject, body = _FAKE_EMAIL[task.language]
        return {"subject": subject, "body": body}
    if tool in _ORDER_TOOLS:
        target = task.expected_outcome.get("order")
        if target is None:
            raise ValueError(f"{task.id}: {tool} needs an expected_outcome.order")
        return {"order_id": int(resolve_refs(str(target), refs))}
    return {}


def fake_script(task: AgentTask, refs: dict[str, int]) -> list[LLMResponse | LLMError]:
    """One tool call per model step along the expected trajectory, then the final answer."""
    script: list[LLMResponse | LLMError] = [
        _fake_response(
            "",
            [
                ToolCall(
                    id=f"{task.id}-{index}",
                    name=tool,
                    arguments=_fake_arguments(tool, task, refs),
                )
            ],
        )
        for index, tool in enumerate(task.expected_trajectory, start=1)
    ]
    script.append(_fake_response(_FAKE_FINAL[task.language]))
    return script


class StepCounter:
    """Counts the model calls (agent steps) of one task."""

    def __init__(self, inner: LLMClient) -> None:
        self.inner = inner
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return await self.inner.complete(request)

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        self.calls += 1
        return self.inner.stream(request)


# --- requests --------------------------------------------------------------------------------


@contextmanager
def _request_trace() -> Iterator[str]:
    """A fresh trace id for one simulated HTTP request, as the middleware binds it."""
    trace_id = new_trace_id()
    token = bind_trace_id(trace_id)
    try:
        yield trace_id
    finally:
        reset_trace_id(token)


async def _tap(events: AsyncIterator[AgentEvent], started: list[str]) -> AsyncIterator[AgentEvent]:
    """Pass the events through, recording the tool of every `tool_start`."""
    async for event in events:
        if isinstance(event, ToolStartEvent):
            started.append(event.tool)
        yield event


async def send_message(
    llm: LLMClient,
    settings: Settings,
    principal: CurrentCustomer,
    request: ChatRequest,
    started: list[str],
) -> ChatResponse:
    """`POST /chat`: one message in its own unit of work and trace."""
    with _request_trace():
        async with session_scope() as session:
            service = ChatService(llm, settings, session)
            return await service.collect(_tap(service.handle_message(request, principal), started))


async def confirm(
    llm: LLMClient,
    settings: Settings,
    principal: CurrentCustomer,
    confirmation: ConfirmationPrompt,
    started: list[str],
) -> ChatResponse:
    """`POST /chat/confirmations/{id}` with `{"decision": "confirm"}`."""
    with _request_trace():
        async with session_scope() as session:
            service = ChatService(llm, settings, session)
            return await service.collect(
                _tap(service.resolve_confirmation(confirmation.id, "confirm", principal), started)
            )


async def seed_demo_commerce() -> None:
    """Reset the personas' orders, refunds and subscription (TOOL-01)."""
    async with session_scope() as session:
        await seed_commerce(session)


async def audited_actions(trace_ids: list[str]) -> list[str]:
    """Tools of the successful `audit_log` rows written under `trace_ids`, oldest first."""
    if not trace_ids:
        return []
    async with session_scope() as session:
        rows = await session.scalars(
            select(AuditLog)
            .where(AuditLog.trace_id.in_(trace_ids), AuditLog.outcome == "success")
            .order_by(AuditLog.created_at, AuditLog.id)
        )
        return [row.tool for row in rows]


async def order_state(order_id: int) -> tuple[str, bool]:
    """Status of the order and whether it has a refund."""
    async with session_scope() as session:
        status = await session.scalar(select(Order.status).where(Order.id == order_id))
        refund = await session.scalar(select(Refund.id).where(Refund.order_id == order_id))
    return (str(status) if status is not None else "missing", refund is not None)


async def check_outcome(
    task: AgentTask, refs: dict[str, int], final_status: ChatStatus, trace_ids: list[str]
) -> tuple[bool, str]:
    """Compare the expected outcome with the database; the detail names every mismatch."""
    expected = task.expected_outcome
    problems: list[str] = []
    if final_status != expected["final_status"]:
        problems.append(f"final_status {final_status}")
    target = expected.get("order")
    if target is not None:
        status, refunded = await order_state(int(resolve_refs(str(target), refs)))
        if status != expected["order_status"]:
            problems.append(f"order {status}")
        if refunded != bool(expected["refunded"]):
            problems.append(f"refunded {refunded}")
    audited = await audited_actions(trace_ids)
    if audited != list(expected["audited"]):
        problems.append(f"audited {audited}")
    return (not problems, "; ".join(problems) or "ok")


async def run_task(
    llm: LLMClient,
    settings: Settings,
    principal: CurrentCustomer,
    task: AgentTask,
    refs: dict[str, int],
) -> TaskResult:
    counter = StepCounter(llm)
    request = ChatRequest(message=resolve_refs(task.message, refs), ui_language=task.language)
    started: list[str] = []
    response = await send_message(counter, settings, principal, request, started)
    trace_ids = [response.trace_id]
    confirmations = 0
    while (
        response.status == "awaiting_confirmation"
        and response.confirmation is not None
        and confirmations < _MAX_CONFIRMATIONS
    ):
        confirmations += 1
        response = await confirm(counter, settings, principal, response.confirmation, started)
        trace_ids.append(response.trace_id)
    outcome_ok, detail = await check_outcome(task, refs, response.status, trace_ids)
    return TaskResult(
        task_id=task.id,
        language=task.language,
        expected_trajectory=task.expected_trajectory,
        trajectory=tuple(started),
        final_status=response.status,
        steps=counter.calls,
        outcome_ok=outcome_ok,
        outcome_detail=detail,
    )


async def run_mode(
    label: str,
    provider: str,
    settings: Settings,
    principals: dict[str, CurrentCustomer],
    tasks: list[AgentTask],
    shared_llm: LLMClient | None,
) -> list[TaskResult]:
    """Every task from a fresh seed, with the step limit of `settings`."""
    results: list[TaskResult] = []
    for task in tasks:
        await seed_demo_commerce()
        principal = principals[task.persona]
        refs = await order_refs(principal)
        llm = shared_llm or build_demo_llm(provider, script=fake_script(task, refs))
        result = await run_task(llm, settings, principal, task, refs)
        verdict = "ok" if result.succeeded else f"FAIL ({_failure(result)})"
        print(
            f"[{label}] {task.id} [{task.language}]: {result.final_status}, "
            f"{result.steps} steps, {' > '.join(result.trajectory) or '-'}: {verdict}"
        )
        results.append(result)
    return results


def _failure(result: TaskResult) -> str:
    problems = []
    if not result.trajectory_ok:
        problems.append(f"expected {' > '.join(result.expected_trajectory)}")
    if not result.outcome_ok:
        problems.append(result.outcome_detail)
    return "; ".join(problems)


async def _principals_by_persona(settings: Settings) -> dict[str, CurrentCustomer]:
    by_language = await seed_demo_principals(settings)
    return {persona.username: by_language[persona.language] for persona in PERSONAS}


async def run_demo(
    provider: str, settings: Settings, tasks: list[AgentTask]
) -> tuple[list[TaskResult], list[TaskResult]]:
    try:
        principals = await _principals_by_persona(settings)
        # `real`: one configured client for every task; `fake`: one script per task.
        shared = build_demo_llm(provider) if provider == "real" else None
        before_settings = settings.model_copy(update={"agent_max_steps": BEFORE_MAX_STEPS})
        before = await run_mode("before", provider, before_settings, principals, tasks, shared)
        after = await run_mode("after", provider, settings, principals, tasks, shared)
        return before, after
    finally:
        # The engine's pooled connections belong to this event loop; close them here.
        await dispose_engine()


# --- report ----------------------------------------------------------------------------------


def summarize(results: list[TaskResult]) -> RunSummary:
    total = len(results)
    by_language: dict[Lang, tuple[int, int]] = {}
    for language in LANGUAGES:
        of_language = [r for r in results if r.language == language]
        if of_language:
            by_language[language] = (sum(r.succeeded for r in of_language), len(of_language))
    return RunSummary(
        successes=sum(r.succeeded for r in results),
        total=total,
        avg_steps=sum(r.steps for r in results) / total if total else 0.0,
        by_language=by_language,
    )


def run_cell(summary: RunSummary, *, max_steps: int) -> str:
    rate = 100 * summary.successes / summary.total if summary.total else 0.0
    languages = ", ".join(
        f"{language} {ok}/{total}" for language, (ok, total) in summary.by_language.items()
    )
    return (
        f"max_steps={max_steps}: success {summary.successes}/{summary.total} ({rate:.0f}%), "
        f"avg {summary.avg_steps:.1f} steps ({languages})"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = demo_parser("Phase 07: multi-step agent benchmark, 1 step vs the default limit")
    parser.set_defaults(results=None)
    return parser


def _provider_note(provider: str, settings: Settings) -> str:
    if provider == "fake":
        return "provider=fake (scripted calls along each expected trajectory)"
    return f"provider={settings.llm_provider}"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.results is None:
        # A fake run never replaces the real `| 07 |` row (same rule as demos 03 to 06).
        fake_results = Path(tempfile.gettempdir()) / "atlas-demo07" / "results.md"
        args.results = DEFAULT_RESULTS_PATH if args.provider == "real" else fake_results

    settings = get_settings()
    tasks = load_tasks(TASKS_PATH)
    before, after = asyncio.run(run_demo(args.provider, settings, tasks))
    after_summary = summarize(after)
    record_result(
        PHASE,
        METRIC,
        run_cell(summarize(before), max_steps=BEFORE_MAX_STEPS),
        run_cell(after_summary, max_steps=settings.agent_max_steps),
        notes=(
            f"Before: ATLAS_AGENT_MAX_STEPS={BEFORE_MAX_STEPS}; After: "
            f"ATLAS_AGENT_MAX_STEPS={settings.agent_max_steps}; success = tool_start trajectory "
            "equals the expected one and the outcome (order status, refund, audit_log success "
            "rows) matches; steps = model calls per task; destructive calls confirmed through "
            "ChatService.resolve_confirmation; commerce reseeded per task; personas alex (en) "
            f"and bia (pt); {_provider_note(args.provider, settings)}"
        ),
        results_path=args.results,
    )
    print(f"Recorded phase {PHASE:02d} in {args.results}")
    # Exit 1 when a task of the "after" run did not follow its trajectory to its outcome.
    return 0 if after_summary.successes == after_summary.total else 1


if __name__ == "__main__":
    sys.exit(main())
