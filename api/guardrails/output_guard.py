"""Output guardrail (CT-55, GRD-04, GRD-08, GRD-93).

Per sentence (`check_sentence`): blocks an e-mail address other than `ctx.own_email`, an order
number (`#123`, "order 123", "pedido 123") not in `ctx.own_order_ids` (GRD-04), and a promise of
an action of a tool (`ACTION_PROMISE_PATTERNS`) that is not in `ctx.successful_tools` (the audit
of the request, GRD-08). Topic (`check_topic`): once per answer, a structured classification
(`ATLAS_GUARDRAIL_MODEL`, prompt `topic_check`) of the question and the first sentence; off topic
-> block. Any classifier error -> block (fail-closed).
"""

import uuid
from dataclasses import dataclass
from typing import Literal

from api.config import Settings
from api.llm.client import LLMClient
from api.messages import Lang

OFF_TOPIC = "off_topic"
OTHER_CUSTOMER_EMAIL = "other_customer_email"
OTHER_CUSTOMER_ORDER = "other_customer_order"
UNFULFILLED_PROMISE = "unfulfilled_promise"
GUARD_FAILURE = "guard_failure"


@dataclass(frozen=True)
class OutputContext:
    customer_id: uuid.UUID
    own_order_ids: set[int]
    own_email: str
    successful_tools: set[str]
    question: str
    language: Lang


@dataclass(frozen=True)
class OutputVerdict:
    action: Literal["allow", "block"]
    reason: str | None = None


class OutputGuard:
    def __init__(self, llm: LLMClient, settings: Settings) -> None:
        self.llm = llm
        self.settings = settings

    async def check_sentence(self, sentence: str, ctx: OutputContext) -> OutputVerdict:
        raise NotImplementedError("TASK-103")

    async def check_topic(self, question: str, first_sentence: str, lang: Lang) -> OutputVerdict:
        raise NotImplementedError("TASK-103")
