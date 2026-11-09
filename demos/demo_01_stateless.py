"""Phase 01 demo: a stateless LLM call forgets the user's name (CHAT-02, DOD-02).

Each language sends two independent messages through `ChatService`, both without a
`conversation_id`: first the user states their name, then asks for it. With no history the
second answer cannot contain the name. Calls without `conversation_id` always start a new
conversation (DA-16), so the demo reproduces the problem on the `phase-01` tag and on HEAD.

From phase 02 on the service persists conversations, so the demo needs the database
(`ATLAS_DATABASE_URL`). It seeds the demo personas (`alex` for en, `bia` for pt) and sends
each message as that customer, one unit of work per message, like one HTTP request.

Usage:
    uv run python -m demos.demo_01_stateless --provider fake --results /tmp/results.md
    uv run python -m demos.demo_01_stateless --provider real   # writes docs/results.md
"""

import asyncio
import sys
from dataclasses import dataclass
from uuid import UUID

from api.auth.deps import CurrentCustomer
from api.auth.models import Customer
from api.chat.schemas import ChatRequest, ChatResponse
from api.chat.service import ChatService
from api.config import Settings, get_settings
from api.db.engine import dispose_engine, session_scope
from api.llm.client import LLMClient, LLMError, LLMResponse, Usage
from api.llm.fake import FAKE_MODEL, FAKE_PROVIDER
from api.messages import Lang
from api.seed.personas import PERSONAS, seed_personas
from demos.common import build_demo_llm, parse_demo_args, record_result

PHASE = 1
NAME = "Rubens"
METRIC = "Name recalled on a 2nd independent call"
AFTER = "Fixed in phase 02 (conversation history): demo_02 reuses the conversation_id"

DIALOGUES: dict[Lang, tuple[str, str]] = {
    "en": (f"My name is {NAME}.", "What is my name?"),
    "pt": (f"Meu nome é {NAME}.", "Qual é o meu nome?"),
}


@dataclass(frozen=True)
class DialogueResult:
    language: Lang
    first_answer: str
    second_answer: str
    conversation_ids: tuple[UUID | None, UUID | None]

    @property
    def name_remembered(self) -> bool:
        return NAME.lower() in self.second_answer.lower()


def _fake_reply(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        usage=Usage(input_tokens=20, output_tokens=12),
        model=FAKE_MODEL,
        provider=FAKE_PROVIDER,
        stop_reason="end_turn",
    )


def default_fake_script() -> list[LLMResponse | LLMError]:
    """Offline answers that behave like a stateless model: the 2nd answer lacks the name."""
    return [
        _fake_reply(f"Nice to meet you, {NAME}! How can I help you today?"),
        _fake_reply("I'm sorry, I don't know your name. Could you tell me?"),
        _fake_reply(f"Prazer em conhecer você, {NAME}! Como posso ajudar?"),
        _fake_reply("Desculpe, não sei o seu nome. Pode me dizer?"),
    ]


def _principal(customer: Customer) -> CurrentCustomer:
    language: Lang = "pt" if customer.language == "pt" else "en"
    return CurrentCustomer(
        id=customer.id,
        email=customer.email,
        display_name=customer.display_name,
        language=language,
    )


async def seed_demo_principals(settings: Settings) -> dict[Lang, CurrentCustomer]:
    """Seed the demo personas (idempotent) and return one customer per language."""
    async with session_scope() as session:
        ids = await seed_personas(session, settings.persona_password)
        principals: dict[Lang, CurrentCustomer] = {}
        for persona, customer_id in zip(PERSONAS, ids, strict=True):
            customer = await session.get(Customer, customer_id)
            if customer is None:  # pragma: no cover - seed_personas just wrote it
                raise RuntimeError(f"Persona {persona.username!r} was not seeded")
            principals.setdefault(persona.language, _principal(customer))
    return principals


async def send_message(
    llm: LLMClient,
    settings: Settings,
    principal: CurrentCustomer,
    request: ChatRequest,
) -> ChatResponse:
    """One message in its own unit of work, as `POST /chat` does with `get_db_session`."""
    async with session_scope() as session:
        return await _collect(ChatService(llm, settings, session), request, principal)


async def _collect(
    service: ChatService, request: ChatRequest, principal: CurrentCustomer
) -> ChatResponse:
    return await service.collect(service.handle_message(request, principal))


async def run_dialogues(llm: LLMClient, settings: Settings) -> list[DialogueResult]:
    principals = await seed_demo_principals(settings)
    results = []
    for language, (introduce, ask_name) in DIALOGUES.items():
        principal = principals[language]
        # No conversation_id: every call is a brand-new conversation, so no history is sent.
        first = await send_message(
            llm, settings, principal, ChatRequest(message=introduce, ui_language=language)
        )
        second = await send_message(
            llm, settings, principal, ChatRequest(message=ask_name, ui_language=language)
        )
        results.append(
            DialogueResult(
                language,
                first.content,
                second.content,
                (first.conversation_id, second.conversation_id),
            )
        )
    return results


async def _run(llm: LLMClient, settings: Settings) -> list[DialogueResult]:
    try:
        return await run_dialogues(llm, settings)
    finally:
        # The engine's pooled connections belong to this event loop; close them here.
        await dispose_engine()


def _summary(results: list[DialogueResult]) -> str:
    return ", ".join(f"{r.language}: {'yes' if r.name_remembered else 'no'}" for r in results)


def main(
    argv: list[str] | None = None, *, script: list[LLMResponse | LLMError] | None = None
) -> int:
    """Run the demo. `script` overrides the offline answers used with `--provider fake`."""
    args = parse_demo_args(
        "Phase 01: two independent chat calls; does the 2nd answer recall the name?", argv
    )
    fake_script = default_fake_script() if script is None else script
    llm: LLMClient = build_demo_llm(args.provider, script=fake_script)

    results = asyncio.run(_run(llm, get_settings()))
    for result in results:
        introduce, ask_name = DIALOGUES[result.language]
        print(f"[{result.language}] user: {introduce}")
        print(f"[{result.language}] assistant: {result.first_answer}")
        print(f"[{result.language}] user: {ask_name}")
        print(f"[{result.language}] assistant: {result.second_answer}")
        print(f"[{result.language}] name_remembered={result.name_remembered}")

    remembered = any(result.name_remembered for result in results)
    print(f"name_remembered={remembered}")
    record_result(
        PHASE,
        METRIC,
        _summary(results),
        AFTER,
        notes=f"provider={args.provider}; calls without conversation_id",
        results_path=args.results,
    )
    print(f"Recorded phase {PHASE:02d} in {args.results}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
