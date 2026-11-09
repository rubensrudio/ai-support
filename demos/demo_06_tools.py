"""Phase 06 demo: cancel an order through tool calling, with confirmation and audit
(TOOL-11, DOD-02).

1. **Seed.** Seeds the personas `alex` (en) and `bia` (pt) (`seed_demo_principals`, as the
   earlier demos) and resets their commerce data with `seed_commerce` (TOOL-01). Order numbers
   come from the table sequence and change on every reset, so the demo looks up each persona's
   `paid` order after seeding.
2. **Before** (`ATLAS_AGENT_TOOLS_ENABLED=false`, the phase-05 behaviour): sends
   "cancel my order #N" / "cancela meu pedido #N" as the persona through `ChatService`. The
   model only answers with text; the demo checks that the order is still `paid`.
3. **After** (tools on): sends the same message in a new conversation. The model calls
   `cancel_order`, a destructive tool, so the agent loop suspends and the service answers
   `awaiting_confirmation` with a `ConfirmationPrompt` (`target='#N'`, TOOL-05). The demo
   confirms it through `ChatService.resolve_confirmation`, the method behind
   `POST /chat/confirmations/{id}`, in a new unit of work, as a second HTTP request would.
   It then reads the order (`cancelled`) and the audit log (`cancel_order`, outcome
   `success`, with the `trace_id` of the confirmation request).
4. **Results.** Records the `| 06 |` row of `docs/results.md`: Before = text only, order
   unchanged; After = order cancelled and audited, per language.

Every request runs in its own unit of work (`session_scope`, like `get_db_session`) with a
fresh trace id, as the HTTP middleware would bind. The identity is always the persona's
session principal; nothing in the messages names a customer. Retrieval is off: the demo is
about tools, not the corpus.

`--provider fake` scripts a `FakeLLMClient` per language: a text answer for the "before"
request, a `cancel_order` tool call, and the final answer after the tool result. `real` uses
the configured provider (including `claude_cli`, which emulates tool calling); the model
decides by itself, so the row reports what really happened (for instance, no confirmation
requested). A fake run writes its results under `<tmp>/atlas-demo06/` unless `--results` is
given, so it never replaces the real `| 06 |` row.

Warning: the demo resets the personas' orders, refunds and subscription in the configured
database (`ATLAS_DATABASE_URL`), exactly like the daily demo reset (`python -m api.seed`).
Conversations, messages and the audit log are kept.

Usage:
    uv run python -m demos.demo_06_tools --provider fake --results /tmp/results.md
    uv run python -m demos.demo_06_tools --provider real   # writes docs/results.md
"""

import argparse
import asyncio
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from api.agent.events import ChatStatus, ConfirmationPrompt
from api.auth.deps import CurrentCustomer
from api.chat.schemas import ChatRequest, ChatResponse
from api.chat.service import ChatService
from api.config import Settings, get_settings
from api.db.engine import dispose_engine, session_scope
from api.llm.client import LLMClient, LLMError, LLMResponse, ToolCall, Usage
from api.llm.fake import FAKE_MODEL, FAKE_PROVIDER
from api.messages import Lang
from api.observability.logging import bind_trace_id, new_trace_id, reset_trace_id
from api.seed.commerce import seed_commerce
from api.tools.models import AuditLog, Order, OrderStatus
from demos.common import DEFAULT_RESULTS_PATH, build_demo_llm, demo_parser, record_result
from demos.demo_01_stateless import seed_demo_principals

PHASE = 6
METRIC = "Cancel a paid order by chat: tools off vs tool calling with confirmation and audit"
LANGUAGES: tuple[Lang, ...] = ("en", "pt")
TOOL = "cancel_order"
MESSAGES: dict[Lang, str] = {
    "en": "cancel my order #{order_id}",
    "pt": "cancela meu pedido #{order_id}",
}
# Scripted answers of `--provider fake`.
_FAKE_BEFORE: dict[Lang, str] = {
    "en": (
        "I can't change orders from here. To cancel order #{order_id}, please use the "
        "Orders page of your account."
    ),
    "pt": (
        "Não consigo alterar pedidos por aqui. Para cancelar o pedido #{order_id}, use a "
        "página de Pedidos da sua conta."
    ),
}
_FAKE_AFTER: dict[Lang, str] = {
    "en": "Done: order #{order_id} is cancelled.",
    "pt": "Pronto: o pedido #{order_id} foi cancelado.",
}


@dataclass(frozen=True)
class LanguageResult:
    language: Lang
    order_id: int
    before_status: ChatStatus
    order_after_before: str
    after_status: ChatStatus
    confirmation_target: str | None
    final_status: ChatStatus | None
    order_after: str
    audit_outcome: str | None
    trace_id: str | None


@contextmanager
def _request_trace() -> Iterator[str]:
    """A fresh trace id for one simulated HTTP request, as the middleware binds it."""
    trace_id = new_trace_id()
    token = bind_trace_id(trace_id)
    try:
        yield trace_id
    finally:
        reset_trace_id(token)


async def seed_demo_commerce() -> None:
    """Reset the personas' orders, refunds and subscription (TOOL-01)."""
    async with session_scope() as session:
        await seed_commerce(session)


async def paid_order_id(principal: CurrentCustomer) -> int:
    """The persona's cancellable `paid` order, looked up after the seed."""
    async with session_scope() as session:
        order_id = await session.scalar(
            select(Order.id)
            .where(Order.customer_id == principal.id, Order.status == OrderStatus.PAID.value)
            .order_by(Order.id)
            .limit(1)
        )
    if order_id is None:
        raise RuntimeError(f"No paid order seeded for customer {principal.id}")
    return int(order_id)


async def order_status(order_id: int) -> str:
    async with session_scope() as session:
        status = await session.scalar(select(Order.status).where(Order.id == order_id))
    return str(status) if status is not None else "missing"


async def audit_outcome(order_id: int, trace_id: str) -> str | None:
    """Outcome of the `cancel_order` audit row of `order_id` written under `trace_id`."""
    async with session_scope() as session:
        rows = await session.scalars(
            select(AuditLog)
            .where(AuditLog.tool == TOOL, AuditLog.trace_id == trace_id)
            .order_by(AuditLog.id)
        )
        for row in rows:
            if row.arguments.get("order_id") == order_id:
                return row.outcome
    return None


async def send_message(
    llm: LLMClient, settings: Settings, principal: CurrentCustomer, request: ChatRequest
) -> ChatResponse:
    """`POST /chat`: one message in its own unit of work and trace."""
    with _request_trace():
        async with session_scope() as session:
            service = ChatService(llm, settings, session)
            return await service.collect(service.handle_message(request, principal))


async def confirm(
    llm: LLMClient,
    settings: Settings,
    principal: CurrentCustomer,
    confirmation: ConfirmationPrompt,
) -> ChatResponse:
    """`POST /chat/confirmations/{id}` with `{"decision": "confirm"}`."""
    with _request_trace():
        async with session_scope() as session:
            service = ChatService(llm, settings, session)
            return await service.collect(
                service.resolve_confirmation(confirmation.id, "confirm", principal)
            )


def fake_script(order_ids: dict[Lang, int]) -> list[LLMResponse | LLMError]:
    """Per language: the "before" text, the `cancel_order` call, the final answer."""
    script: list[LLMResponse | LLMError] = []
    for language in LANGUAGES:
        order_id = order_ids[language]
        call = ToolCall(id=f"call-cancel-{language}", name=TOOL, arguments={"order_id": order_id})
        script += [
            _fake_response(_FAKE_BEFORE[language].format(order_id=order_id)),
            _fake_response("", [call]),
            _fake_response(_FAKE_AFTER[language].format(order_id=order_id)),
        ]
    return script


def _fake_response(text: str, calls: list[ToolCall] | None = None) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=list(calls or []),
        usage=Usage(input_tokens=0, output_tokens=0),
        model=FAKE_MODEL,
        provider=FAKE_PROVIDER,
        stop_reason="tool_use" if calls else "end_turn",
    )


async def run_language(
    llm: LLMClient,
    settings: Settings,
    principal: CurrentCustomer,
    language: Lang,
    order_id: int,
) -> LanguageResult:
    message = MESSAGES[language].format(order_id=order_id)
    request = ChatRequest(message=message, ui_language=language)

    # Before: tools off (ATLAS_AGENT_TOOLS_ENABLED=false), text only.
    tools_off = settings.model_copy(update={"agent_tools_enabled": False})
    before = await send_message(llm, tools_off, principal, request)
    order_after_before = await order_status(order_id)
    print(f"[{language}] before (tools off) {message!r}: {before.status}: {before.content!r}")

    # After: tools on; a destructive call waits for the customer's confirmation.
    tools_on = settings.model_copy(update={"agent_tools_enabled": True})
    waiting = await send_message(llm, tools_on, principal, request)
    print(f"[{language}] after {message!r}: {waiting.status}: {waiting.content!r}")
    final: ChatResponse | None = None
    target: str | None = None
    if waiting.status == "awaiting_confirmation" and waiting.confirmation is not None:
        target = waiting.confirmation.target
        print(f"[{language}] confirmation {waiting.confirmation.tool} {target}: confirm")
        final = await confirm(llm, tools_on, principal, waiting.confirmation)
        print(f"[{language}] final: {final.status}: {final.content!r}")
    status = await order_status(order_id)
    trace_id = final.trace_id if final is not None else None
    outcome = await audit_outcome(order_id, trace_id) if trace_id else None
    print(
        f"[{language}] order #{order_id}: {status}; audit {TOOL}: {outcome or 'none'} "
        f"(trace_id={trace_id or '-'})"
    )
    return LanguageResult(
        language=language,
        order_id=order_id,
        before_status=before.status,
        order_after_before=order_after_before,
        after_status=waiting.status,
        confirmation_target=target,
        final_status=final.status if final is not None else None,
        order_after=status,
        audit_outcome=outcome,
        trace_id=trace_id,
    )


async def run_demo(provider: str, settings: Settings) -> list[LanguageResult]:
    try:
        principals = await seed_demo_principals(settings)
        await seed_demo_commerce()
        order_ids = {lang: await paid_order_id(principals[lang]) for lang in LANGUAGES}
        script = fake_script(order_ids) if provider == "fake" else None
        llm = build_demo_llm(provider, script=script)
        return [
            await run_language(llm, settings, principals[lang], lang, order_ids[lang])
            for lang in LANGUAGES
        ]
    finally:
        # The engine's pooled connections belong to this event loop; close them here.
        await dispose_engine()


def before_cell(results: list[LanguageResult]) -> str:
    return "; ".join(
        f"{r.language}: text only, order {r.order_after_before} (#{r.order_id})" for r in results
    )


def after_cell(results: list[LanguageResult]) -> str:
    cells = []
    for r in results:
        if r.confirmation_target is None:
            cells.append(f"{r.language}: no confirmation requested, order {r.order_after}")
            continue
        audit = f"audit {r.audit_outcome}" if r.audit_outcome else "no audit row"
        cells.append(
            f"{r.language}: order {r.order_after}, {audit} "
            f"(confirmation {TOOL} {r.confirmation_target})"
        )
    return "; ".join(cells)


def succeeded(result: LanguageResult) -> bool:
    return (
        result.order_after_before == OrderStatus.PAID.value
        and result.order_after == OrderStatus.CANCELLED.value
        and result.audit_outcome == "success"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = demo_parser("Phase 06: cancel an order by chat, tools off vs tool calling")
    parser.set_defaults(results=None)
    return parser


def _provider_note(provider: str, settings: Settings) -> str:
    if provider == "fake":
        return "provider=fake (scripted cancel_order call)"
    return f"provider={settings.llm_provider}"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.results is None:
        # A fake run never replaces the real `| 06 |` row (same rule as demos 03 to 05).
        fake_results = Path(tempfile.gettempdir()) / "atlas-demo06" / "results.md"
        args.results = DEFAULT_RESULTS_PATH if args.provider == "real" else fake_results

    settings = get_settings()
    results = asyncio.run(run_demo(args.provider, settings))
    record_result(
        PHASE,
        METRIC,
        before_cell(results),
        after_cell(results),
        notes=(
            "Before: same message with ATLAS_AGENT_TOOLS_ENABLED=false; After: tools on, "
            "confirmed through ChatService.resolve_confirmation (POST /chat/confirmations/{id}); "
            "order status and audit_log (outcome, trace_id) read from the database; personas "
            f"alex (en) and bia (pt); {_provider_note(args.provider, settings)}"
        ),
        results_path=args.results,
    )
    print(f"Recorded phase {PHASE:02d} in {args.results}")
    # Exit 1 when a language did not end with the order cancelled and audited (TOOL-11).
    return 0 if all(succeeded(r) for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
