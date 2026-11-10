"""Output guardrail (CT-55, GRD-04, GRD-08, GRD-93).

Per sentence (`check_sentence`): blocks an e-mail address other than `ctx.own_email`, an order
number (`#123`, "order 123", "pedido 123") not in `ctx.own_order_ids` (GRD-04), and a promise of
an action of a tool (`ACTION_PROMISE_PATTERNS`) that is not in `ctx.successful_tools` (the audit
of the request, GRD-08). Topic (`check_topic`): once per answer, a structured classification
(`ATLAS_GUARDRAIL_MODEL`, prompt `topic_check`) of the question and the first sentence; off topic
-> block. Any classifier error -> block (fail-closed).

Order numbers: `#n` (not after "step", "passo", "item", "ticket"...) or "order/pedido" followed
by a marker ("number", "nº", "no.", `#`) or by a bare number that is not a quantity, a duration
or a year ("pedidos de 100 reais", "Pedidos de 2024"). Ids the customer typed in the question
(`question_order_ids`) may be repeated. Promises come from `find_action_promises` (both
languages, whatever `ctx.language` is): a promise citing an order already cancelled / refunded
(`cancelled_order_ids` / `refunded_order_ids`) is a report of the real state; with no order
cited, only the tool in `successful_tools` backs it. Every promise of the sentence must be backed;
`cancel_subscription` (no such tool) never is; any other promise needs its tool in
`successful_tools`. `LLMRequest` carries no model, so
the topic check runs on the injected client (`settings.guardrail_model` selects the model only
when the app wires a client built for it).

Phase 14 (TASK-133, OBS-01): `check_sentence` and `check_topic` are each one `guardrail` span
(CT-62) with the type (`output`), the check (`sentence`/`topic`), the verdict and the reason;
never the sentence or the question.
"""

import contextlib
import json
import re
import unicodedata
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Literal

from opentelemetry.trace import Span

from api.config import Settings
from api.guardrails.patterns import find_action_promises
from api.llm.client import ChatMessage, LLMClient, LLMRequest, SystemBlock
from api.messages import Lang
from api.observability.logging import log_event
from api.observability.tracing import start_span
from api.prompts import load_prompt

OFF_TOPIC = "off_topic"
OTHER_CUSTOMER_EMAIL = "other_customer_email"
OTHER_CUSTOMER_ORDER = "other_customer_order"
UNFULFILLED_PROMISE = "unfulfilled_promise"
GUARD_FAILURE = "guard_failure"

TOPIC_PROMPT = "topic_check"
TOPIC_PURPOSE = "guardrail"
TOPIC_MAX_TOKENS = 200
TOPIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "on_topic": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["on_topic", "reason"],
    "additionalProperties": False,
}

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_ORDER = re.compile(
    r"\b(?:orders?|pedidos?|encomendas?|compras?)\s+(?P<de>de\s+)?"
    r"(?P<marker>(?:number|numero|num\.?|no\.?|n\.)\s*#?\s*|#\s*)?(?P<word>\d+)\b"
    r"|(?<![\w&#])#\s?(?P<hash>\d+)\b",
    re.IGNORECASE,
)
# A bare number after "order"/"pedido" that is a quantity, a duration or a year is not an id.
_NOT_AN_ID_AFTER = re.compile(
    r"\s*(?:reais|real|r\$|dolares|dolar|dollars?|usd|brl|euros?|dias|dia|days?|horas|hours?|"
    r"semanas|weeks?|meses|mes|months?|anos|years?|itens|item|items|unidades|units?|produtos|"
    r"products?|caixas|boxes|ou\s+mais|or\s+more|or\s+less|ou\s+menos)(?!\w)|\s*%",
    re.IGNORECASE,
)
CANCEL_SUBSCRIPTION = "cancel_subscription"
_YEAR = re.compile(r"(?:19|20)\d\d")
# "#n" after these words numbers a step, an item or a ticket, not an order.
_HASH_NOT_ORDER = frozenset(
    {
        "step",
        "steps",
        "passo",
        "passos",
        "etapa",
        "etapas",
        "item",
        "itens",
        "items",
        "option",
        "opcao",
        "tip",
        "dica",
        "rule",
        "regra",
        "question",
        "pergunta",
        "line",
        "linha",
        "section",
        "secao",
        "page",
        "pagina",
        "chapter",
        "capitulo",
        "version",
        "versao",
        "ticket",
        "chamado",
        "issue",
        "priority",
        "prioridade",
        "rank",
        "top",
        "number",
        "numero",
        "faq",
        "sku",
        "plan",
        "plano",
    }
)
_WORD_BEFORE = re.compile(r"(\w+)\W*$")
_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "`": "'", "´": "'"})
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


@dataclass(frozen=True)
class OutputContext:
    """Per-answer facts the output guard checks sentences against (CT-55).

    Orchestrator additions (user decisions, additive): `cancelled_order_ids` /
    `refunded_order_ids` are the customer's orders already cancelled / refunded in the database,
    so a sentence reporting that state is not an unfulfilled promise; `question_order_ids` are
    the order numbers the customer wrote in the question, which the answer may repeat (e.g. "I
    couldn't find order #13") even when they are not the customer's.
    """

    customer_id: uuid.UUID
    own_order_ids: set[int]
    own_email: str
    successful_tools: set[str]
    question: str
    language: Lang
    cancelled_order_ids: set[int] = field(default_factory=set)
    refunded_order_ids: set[int] = field(default_factory=set)
    question_order_ids: set[int] = field(default_factory=set)


@dataclass(frozen=True)
class OutputVerdict:
    action: Literal["allow", "block"]
    reason: str | None = None


_ALLOW = OutputVerdict(action="allow")


class TopicOutputError(ValueError):
    """The topic classifier answered something that is not a valid verdict."""


def _normalize(text: str) -> str:
    """NFKD without accents (so `nº` reads `no`) and with plain apostrophes."""
    decomposed = unicodedata.normalize("NFKD", text)
    kept = "".join(ch for ch in decomposed if unicodedata.category(ch) not in {"Mn", "Cf"})
    return kept.translate(_APOSTROPHES)


def _escape(text: str) -> str:
    """Customer and model text is data: it must not open or close the prompt tags."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _block(reason: str) -> OutputVerdict:
    return OutputVerdict(action="block", reason=reason)


def _guard_span(check: str) -> AbstractContextManager[Span]:
    return start_span(
        "guardrail", **{"atlas.guardrail.type": "output", "atlas.guardrail.check": check}
    )


def _record_verdict(span: Span, verdict: OutputVerdict) -> None:
    span.set_attribute("atlas.guardrail.verdict", verdict.action)
    if verdict.reason is not None:
        span.set_attribute("atlas.guardrail.reason", verdict.reason)


def _log_failure(check: str, exc: BaseException) -> None:
    with contextlib.suppress(Exception):
        log_event("guardrail.output.failure", check=check, error_type=type(exc).__name__)


def _parse_topic(text: str) -> bool:
    stripped = text.strip()
    fenced = _FENCE.match(stripped)
    if fenced:
        stripped = fenced.group(1)
    try:
        raw = json.loads(stripped)
    except json.JSONDecodeError as error:
        raise TopicOutputError("topic verdict is not JSON") from error
    if not isinstance(raw, dict):
        raise TopicOutputError("topic verdict is not a JSON object")
    on_topic = raw.get("on_topic")
    if not isinstance(on_topic, bool) or not isinstance(raw.get("reason"), str):
        raise TopicOutputError("topic verdict does not match the schema")
    return on_topic


def _order_ids(text: str) -> list[int]:
    """Order ids cited in `text`; quantities, durations, years and step numbers are skipped."""
    ids: list[int] = []
    for match in _ORDER.finditer(text):
        if match.group("hash") is not None:
            before = _WORD_BEFORE.search(text, 0, match.start())
            if before and before.group(1).casefold() in _HASH_NOT_ORDER:
                continue
            ids.append(int(match.group("hash")))
            continue
        digits = match.group("word")
        if match.group("marker") is None and (
            match.group("de") is not None
            or _YEAR.fullmatch(digits)
            or _NOT_AN_ID_AFTER.match(text, match.end())
        ):
            continue
        ids.append(int(digits))
    return ids


# Fail closed: every promised tool is checked unless it is known to be read-only.
READ_ONLY_TOOLS = frozenset({"list_orders", "get_order", "get_subscription", "check_refund_policy"})


def _promise_is_backed(tool: str, order_id: int | None, ctx: OutputContext) -> bool:
    """One claimed action is backed by a successful call of its tool in this request, or by the
    real state of the customer's orders (a report, not a promise):

    - a cited order must already be in the claimed state (`cancel_order` -> cancelled,
      `issue_refund` -> refunded);
    - with no order cited, only the tool itself backs it: a read tool plus one order in that
      state is not enough ("I've cancelled your order." is the core GRD-08 case);
    - `cancel_subscription` has no tool and is never backed;
    - read-only tools (`list_orders`, `get_order`, `get_subscription`, `check_refund_policy`)
      are not actions (GRD-08 is about side effects) and are never audited, so a claim such as
      "I checked your orders" is always allowed.
    """
    if tool in READ_ONLY_TOOLS:
        return True
    if tool == CANCEL_SUBSCRIPTION:
        return False
    if tool in ctx.successful_tools:
        return True
    in_state = _orders_in_claimed_state(tool, ctx)
    if in_state is None:
        return False
    return order_id is not None and order_id in in_state


def _orders_in_claimed_state(tool: str, ctx: OutputContext) -> set[int] | None:
    if tool == "cancel_order":
        return ctx.cancelled_order_ids
    if tool == "issue_refund":
        return ctx.refunded_order_ids
    return None


class OutputGuard:
    def __init__(self, llm: LLMClient, settings: Settings) -> None:
        self.llm = llm
        self.settings = settings

    async def check_sentence(self, sentence: str, ctx: OutputContext) -> OutputVerdict:
        with _guard_span("sentence") as span:
            try:
                verdict = self._check_sentence(sentence, ctx)
            except Exception as exc:  # fail-closed (GRD-93, LAC-28): never re-raise
                _log_failure("sentence", exc)
                verdict = _block(GUARD_FAILURE)
            _record_verdict(span, verdict)
            return verdict

    def _check_sentence(self, sentence: str, ctx: OutputContext) -> OutputVerdict:
        text = _normalize(sentence)
        own_email = ctx.own_email.strip().casefold()
        if any(match.group(0).casefold() != own_email for match in _EMAIL.finditer(text)):
            return _block(OTHER_CUSTOMER_EMAIL)
        allowed_ids = (
            ctx.own_order_ids
            | ctx.question_order_ids
            | ctx.cancelled_order_ids
            | ctx.refunded_order_ids
        )
        if any(order_id not in allowed_ids for order_id in _order_ids(text)):
            return _block(OTHER_CUSTOMER_ORDER)
        for tool, order_id in find_action_promises(text):
            if not _promise_is_backed(tool, order_id, ctx):
                return _block(UNFULFILLED_PROMISE)
        return _ALLOW

    async def check_topic(self, question: str, first_sentence: str, lang: Lang) -> OutputVerdict:
        with _guard_span("topic") as span:
            verdict = await self._check_topic(question, first_sentence, lang)
            _record_verdict(span, verdict)
            return verdict

    async def _check_topic(self, question: str, first_sentence: str, lang: Lang) -> OutputVerdict:
        try:
            response = await self.llm.complete(self._topic_request(question, first_sentence, lang))
            on_topic = _parse_topic(response.text)
        except Exception as exc:  # fail-closed (GRD-93, LAC-28): never re-raise
            _log_failure("topic", exc)
            return _block(GUARD_FAILURE)
        return _ALLOW if on_topic else _block(OFF_TOPIC)

    def _topic_request(self, question: str, first_sentence: str, lang: Lang) -> LLMRequest:
        content = (
            f"<language>{lang}</language>\n\n"
            f"<question>\n{_escape(question)}\n</question>\n\n"
            f"<answer_start>\n{_escape(first_sentence)}\n</answer_start>"
        )
        return LLMRequest(
            system=[SystemBlock(text=load_prompt(TOPIC_PROMPT))],
            messages=[ChatMessage(role="user", content=content)],
            response_schema=TOPIC_SCHEMA,
            max_tokens=TOPIC_MAX_TOKENS,
            purpose=TOPIC_PURPOSE,
        )
