"""Phase 11 demo: prompt cache, `naive` vs `cache_ordered` prompt layout (CACHE-04, DOD-02).

1. **Conversations.** `--n` conversations (default 100), alternating en (persona `alex`) and pt
   (persona `bia`), in pairs (one en, one pt) of 2 turns, then of 3 turns, and so on, taken
   from `QUESTIONS`. Every conversation is new; its later turns carry `conversation_id`, so the
   history grows inside a conversation like a real chat.
2. **Run.** Every turn goes through `ChatService` as its persona, in its own unit of work and
   trace, like `POST /chat`, with the default chat path: the agent tools (`registry.specs()`)
   and the retriever `get_chat_service` wires (`ATLAS_RETRIEVAL_MODE`). The same conversations
   run twice, changing only `prompt_layout` (`settings.model_copy`):
   - **Before** = `naive`: one system block that starts with the current UTC date and time and
     the reply-language instruction, no cache breakpoint;
   - **After** = `cache_ordered`: tools + `grounded_system_prompt()` as the stable prefix with
     the cache breakpoint, then the language instruction, the excerpts, the history and the
     question (CACHE-01, DA-23).
   The guardrails are **off** in both runs (`guardrails_enabled=False`): the input and output
   checks do not touch the chat prompt, and the topic check calls another model, so leaving them
   on would only add calls and latency that are the same in both layouts.
3. **Metrics.** `MeteredLLM` wraps the chat client (inside it, `InstrumentedLLMClient` still
   logs every `llm_call` with its cache tokens, CACHE-03) and keeps, per call, the latency, the
   provider usage and the cost (`estimate_cost` on the provider usage; unknown for a
   subscription CLI provider or a model missing from the pricing table). The `| 11 |` row of
   `docs/results.md` gets, per layout, the mean cost and latency per call, the cache hit rate
   (calls with `cache_read_tokens > 0`) and the mean input / cache read / cache write tokens.
   Each answer must come back in the conversation's language (`detect_language`): the language
   instruction left the system prompt in `cache_ordered`, so this is the behaviour regression
   check of the phase (the run exits 1 on a mismatch or a turn that did not complete).

`--provider fake` answers with `SimulatedCacheLLM`, which simulates a provider prompt cache
(`PromptCacheSimulator`): the prefix of a request is the serialized tools plus the system blocks
up to the last `cache_breakpoint` (the whole system when there is none); a call whose prefix is
byte-identical to the prefix of an earlier call reads it from the cache (`cache_read_tokens` =
its tokens), otherwise it is a miss, and a miss on a request with a breakpoint writes it
(`cache_write_tokens`). Tokens are counted with `api.llm.tokens.count_tokens`. Each layout
starts with an empty cache. The fake answers in the language named by the request's
reply-language instruction and fails when there is none. `fake-model` is priced at 0 in
`config/pricing.json`, so the fake cost is $0 by design (the token columns show the shift) and
its latency is in-process time only. A fake run writes its results under `<tmp>/atlas-demo11/`
unless `--results` is given, so it never replaces the real `| 11 |` row.

`real` uses the configured provider. With `anthropic` the adapter sends
`cache_control: ephemeral` on the breakpoint block and reads `cache_read_input_tokens` /
`cache_creation_input_tokens`; a prefix shorter than the model's minimum cacheable length is
not cached. With `claude_cli` the cache tokens are the ones the CLI reports (the CLI adds its
own system prompt and caching) and the cost is unknown (subscription, LAC-43).

Usage:
    uv run python -m demos.demo_11_prompt_cache --provider fake --n 5 --results /tmp/results.md
    uv run python -m demos.demo_11_prompt_cache --provider real   # writes docs/results.md
"""

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

from api.auth.deps import CurrentCustomer
from api.chat.language import detect_language
from api.chat.prompt_builder import LANGUAGE_NAMES, language_instruction
from api.chat.schemas import ChatRequest, ChatResponse
from api.chat.service import ChatService
from api.config import SUBSCRIPTION_PROVIDERS, Settings, get_settings
from api.db.engine import dispose_engine, get_sessionmaker, session_scope
from api.llm.client import LLMClient, LLMRequest, LLMResponse, LLMStreamEvent, Usage
from api.llm.fake import FAKE_MODEL, FAKE_PROVIDER, split_deltas
from api.llm.instrumented import InstrumentedLLMClient
from api.llm.pricing import UnknownModelPricing, estimate_cost
from api.llm.tokens import count_tokens
from api.messages import Lang
from api.observability.logging import bind_trace_id, new_trace_id, reset_trace_id
from api.rag.embeddings import build_embedder
from api.rag.retriever import Retriever, is_vector_mode
from demos.common import DEFAULT_RESULTS_PATH, build_demo_llm, demo_parser, record_result
from demos.demo_01_stateless import seed_demo_principals

PHASE = 11
Layout = Literal["naive", "cache_ordered"]
LAYOUTS: tuple[Layout, ...] = ("naive", "cache_ordered")
LANGUAGES: tuple[Lang, ...] = ("en", "pt")

QUESTIONS: dict[Lang, tuple[str, ...]] = {
    "en": (
        "How do I change the card used for my subscription?",
        "Can I pause my plan instead of cancelling it?",
        "How long does a refund take to show up on my card?",
    ),
    "pt": (
        "Como eu troco o cartão usado na minha assinatura?",
        "Posso pausar o meu plano em vez de cancelar?",
        "Quanto tempo o reembolso demora para aparecer no meu cartão?",
    ),
}
# Answers of `--provider fake`, chosen by the reply-language instruction of the request.
FAKE_REPLIES: dict[Lang, str] = {
    "en": (
        "Thanks for reaching out. You can manage your subscription, your payment card and "
        "your orders from the account page at any time."
    ),
    "pt": (
        "Obrigado pelo contato. Você pode gerenciar a sua assinatura, o seu cartão de "
        "pagamento e os seus pedidos pela página da sua conta a qualquer momento."
    ),
}


@dataclass(frozen=True)
class Conversation:
    language: Lang
    turns: tuple[str, ...]


@dataclass(frozen=True)
class CallRecord:
    latency_ms: float
    usage: Usage
    cost_usd: Decimal | None  # None: unknown (subscription provider or unpriced model)


@dataclass(frozen=True)
class RunSummary:
    calls: int
    cache_hits: int
    avg_cost_usd: Decimal | None
    unpriced_calls: int
    avg_latency_ms: float
    avg_input_tokens: float
    avg_cache_read_tokens: float
    avg_cache_write_tokens: float


@dataclass(frozen=True)
class LayoutRun:
    records: list[CallRecord]
    language_mismatches: int
    incomplete_turns: int


def conversations_for(n: int) -> list[Conversation]:
    """`n` conversations, en and pt alternating; each pair has 2 turns, the next pair 3."""
    conversations: list[Conversation] = []
    for index in range(n):
        language = LANGUAGES[index % 2]
        turns = 2 + (index // 2) % 2
        conversations.append(Conversation(language, QUESTIONS[language][:turns]))
    return conversations


# --- fake provider with a simulated prompt cache ---------------------------------------------


def _tools_text(request: LLMRequest) -> str:
    tools = [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in request.tools
    ]
    return json.dumps(tools, ensure_ascii=False, sort_keys=True)


def _split_prefix(request: LLMRequest) -> tuple[str, str, bool]:
    """(prefix, rest, has_breakpoint): tools + system up to the last breakpoint, then the rest."""
    marks = [i for i, block in enumerate(request.system) if block.cache_breakpoint]
    end = marks[-1] + 1 if marks else len(request.system)
    prefix = "\n".join([_tools_text(request), *(b.text for b in request.system[:end])])
    rest = "\n".join(
        [*(b.text for b in request.system[end:]), *(m.content for m in request.messages)]
    )
    return prefix, rest, bool(marks)


class PromptCacheSimulator:
    """Remembers the prefixes already sent; a byte-identical prefix is a cache read."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def usage(self, request: LLMRequest, reply: str) -> Usage:
        prefix, rest, has_breakpoint = _split_prefix(request)
        key = hashlib.sha256(prefix.encode("utf-8")).hexdigest()
        prefix_tokens = count_tokens(prefix)
        hit = key in self._seen
        self._seen.add(key)
        return Usage(
            input_tokens=count_tokens(rest) + (0 if hit or has_breakpoint else prefix_tokens),
            output_tokens=count_tokens(reply),
            cache_read_tokens=prefix_tokens if hit else 0,
            cache_write_tokens=prefix_tokens if not hit and has_breakpoint else 0,
        )


def reply_language(request: LLMRequest) -> Lang:
    """The language named by the reply-language instruction of the request's system blocks."""
    system = "\n".join(block.text for block in request.system)
    found = [lang for lang in LANGUAGE_NAMES if language_instruction(lang) in system]
    if len(found) != 1:
        raise ValueError(f"expected one reply-language instruction in the request, got {found}")
    return found[0]


class SimulatedCacheLLM:
    """`--provider fake` chat model: a fixed answer per language, usage from the simulator."""

    def __init__(self, cache: PromptCacheSimulator) -> None:
        self.cache = cache

    async def complete(self, request: LLMRequest) -> LLMResponse:
        text = FAKE_REPLIES[reply_language(request)]
        return LLMResponse(
            text=text,
            tool_calls=[],
            usage=self.cache.usage(request, text),
            model=FAKE_MODEL,
            provider=FAKE_PROVIDER,
            stop_reason="end_turn",
        )

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        response = await self.complete(request)
        for chunk in split_deltas(response.text):
            yield LLMStreamEvent(kind="text_delta", text=chunk)
        yield LLMStreamEvent(kind="usage", usage=response.usage)
        yield LLMStreamEvent(kind="stop", stop_reason=response.stop_reason)


# --- metering --------------------------------------------------------------------------------


def call_cost(model: str, provider: str, usage: Usage) -> Decimal | None:
    """`estimate_cost` of one call; None (unknown) for a subscription provider or no price."""
    if provider in SUBSCRIPTION_PROVIDERS:
        return None
    try:
        return estimate_cost(model, usage)
    except UnknownModelPricing:
        return None


class MeteredLLM:
    """Keeps latency, usage and cost of every call; `provider` and `inner` are exposed so the
    chat service still recognises the fake provider behind it."""

    def __init__(self, inner: LLMClient, model: str, provider: str) -> None:
        self.inner = inner
        self.model = model
        self.provider = provider
        self.records: list[CallRecord] = []

    def _record(self, started: float, usage: Usage) -> None:
        latency_ms = (time.perf_counter() - started) * 1000
        cost = call_cost(self.model, self.provider, usage)
        self.records.append(CallRecord(latency_ms=latency_ms, usage=usage, cost_usd=cost))

    async def complete(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        response = await self.inner.complete(request)
        self._record(started, response.usage)
        return response

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        started = time.perf_counter()
        usage = Usage(input_tokens=0, output_tokens=0)
        async for event in self.inner.stream(request):
            if event.kind == "usage" and event.usage is not None:
                usage = event.usage
            yield event
        self._record(started, usage)


# --- run -------------------------------------------------------------------------------------


@contextmanager
def _request_trace() -> Iterator[str]:
    """A fresh trace id for one simulated HTTP request, as the middleware binds it."""
    trace_id = new_trace_id()
    token = bind_trace_id(trace_id)
    try:
        yield trace_id
    finally:
        reset_trace_id(token)


async def send_message(
    llm: LLMClient,
    retriever: Retriever | None,
    settings: Settings,
    principal: CurrentCustomer,
    request: ChatRequest,
) -> ChatResponse:
    """`POST /chat`: one message in its own unit of work and trace."""
    with _request_trace():
        async with session_scope() as session:
            service = ChatService(llm, settings, session, retriever=retriever)
            return await service.collect(service.handle_message(request, principal))


def _metered(provider: str, settings: Settings) -> MeteredLLM:
    """A fresh chat client per layout; the fake one gets an empty simulated cache."""
    if provider == "fake":
        fake = SimulatedCacheLLM(PromptCacheSimulator())
        client: LLMClient = InstrumentedLLMClient(fake, model=FAKE_MODEL, provider=FAKE_PROVIDER)
        return MeteredLLM(client, FAKE_MODEL, FAKE_PROVIDER)
    client = build_demo_llm("real")
    model = str(getattr(client, "model", settings.primary_model or "unknown"))
    return MeteredLLM(client, model, settings.llm_provider)


async def run_layout(
    layout: Layout,
    provider: str,
    settings: Settings,
    principals: dict[Lang, CurrentCustomer],
    conversations: list[Conversation],
    retriever: Retriever | None,
) -> LayoutRun:
    llm = _metered(provider, settings)
    layout_settings = settings.model_copy(
        update={"prompt_layout": layout, "guardrails_enabled": False}
    )
    mismatches = incomplete = 0
    for number, conversation in enumerate(conversations, start=1):
        conversation_id = None
        for turn, message in enumerate(conversation.turns, start=1):
            first_call = len(llm.records)
            request = ChatRequest(
                message=message,
                conversation_id=conversation_id,
                ui_language=conversation.language,
            )
            response = await send_message(
                llm, retriever, layout_settings, principals[conversation.language], request
            )
            conversation_id = response.conversation_id
            answered = detect_language(response.content, fallback=conversation.language)
            mismatches += answered != conversation.language
            incomplete += response.status != "completed"
            read = sum(r.usage.cache_read_tokens for r in llm.records[first_call:])
            print(
                f"[{layout}] conversation {number} [{conversation.language}] turn {turn}: "
                f"{response.status}, answer {answered}, cache read {read} tokens"
            )
    return LayoutRun(llm.records, mismatches, incomplete)


def _retriever(settings: Settings) -> Retriever:
    """The retriever `get_chat_service` wires for `ATLAS_RETRIEVAL_MODE`."""
    embedder = build_embedder(settings) if is_vector_mode(settings.retrieval_mode) else None
    return Retriever(get_sessionmaker(), embedder, settings)


async def run_demo(
    provider: str, settings: Settings, conversations: list[Conversation]
) -> dict[Layout, LayoutRun]:
    try:
        principals = await seed_demo_principals(settings)
        retriever = _retriever(settings)
        return {
            layout: await run_layout(
                layout, provider, settings, principals, conversations, retriever
            )
            for layout in LAYOUTS
        }
    finally:
        # The engine's pooled connections belong to this event loop; close them here.
        await dispose_engine()


# --- report ----------------------------------------------------------------------------------


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize(records: list[CallRecord]) -> RunSummary:
    costs = [r.cost_usd for r in records if r.cost_usd is not None]
    unpriced = len(records) - len(costs)
    avg_cost = sum(costs, Decimal(0)) / len(costs) if costs and not unpriced else None
    return RunSummary(
        calls=len(records),
        cache_hits=sum(r.usage.cache_read_tokens > 0 for r in records),
        avg_cost_usd=avg_cost,
        unpriced_calls=unpriced,
        avg_latency_ms=_mean([r.latency_ms for r in records]),
        avg_input_tokens=_mean([r.usage.input_tokens for r in records]),
        avg_cache_read_tokens=_mean([r.usage.cache_read_tokens for r in records]),
        avg_cache_write_tokens=_mean([r.usage.cache_write_tokens for r in records]),
    )


def _percent(part: int, total: int) -> str:
    return f"{100 * part / total:.0f}%" if total else "n/a"


def metrics_cell(summary: RunSummary) -> str:
    if summary.avg_cost_usd is not None:
        cost = f"${summary.avg_cost_usd:.6f}"
    else:
        cost = f"unknown ({summary.unpriced_calls}/{summary.calls} calls unpriced)"
    return (
        f"cost/call {cost}, latency/call {summary.avg_latency_ms:.1f} ms, "
        f"cache hit {summary.cache_hits}/{summary.calls} "
        f"({_percent(summary.cache_hits, summary.calls)}), "
        f"tokens/call: input {summary.avg_input_tokens:.0f}, "
        f"cache read {summary.avg_cache_read_tokens:.0f}, "
        f"cache write {summary.avg_cache_write_tokens:.0f}"
    )


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {number}")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = demo_parser("Phase 11: prompt cache, naive vs cache_ordered layout")
    parser.add_argument(
        "--n", type=_positive_int, default=100, help="conversations per layout (default 100)"
    )
    parser.set_defaults(results=None)
    return parser


def _provider_note(provider: str, settings: Settings) -> str:
    if provider == "fake":
        return (
            "provider=fake (simulated prompt cache: a byte-identical tools + system prefix "
            "seen before is read; fake-model priced at 0, so the fake cost is $0 by design)"
        )
    if settings.llm_provider in SUBSCRIPTION_PROVIDERS:
        return (
            f"provider={settings.llm_provider} (cache tokens as reported by the CLI; "
            "subscription, cost unknown)"
        )
    return f"provider={settings.llm_provider}, cost from config/pricing.json with cache prices"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.results is None:
        # A fake run never replaces the real `| 11 |` row (same rule as demos 03 to 10).
        fake_results = Path(tempfile.gettempdir()) / "atlas-demo11" / "results.md"
        args.results = DEFAULT_RESULTS_PATH if args.provider == "real" else fake_results

    settings = get_settings()
    conversations = conversations_for(args.n)
    runs = asyncio.run(run_demo(args.provider, settings, conversations))
    mismatches = sum(run.language_mismatches for run in runs.values())
    incomplete = sum(run.incomplete_turns for run in runs.values())
    record_result(
        PHASE,
        (
            f"Prompt cache over {args.n} conversations pt/en (2-3 turns): cost and latency per "
            "call and cache hit rate"
        ),
        metrics_cell(summarize(runs["naive"].records)),
        metrics_cell(summarize(runs["cache_ordered"].records)),
        notes=(
            "Before: ATLAS_PROMPT_LAYOUT=naive (UTC date/time and language on top, no "
            "breakpoint); After: ATLAS_PROMPT_LAYOUT=cache_ordered (tools + system as stable "
            "prefix with the breakpoint, language after it); cache hit = call with "
            "cache_read_tokens > 0; means per chat model call; agent tools and retrieval as "
            "configured, guardrails off in both runs; personas alex (en) and bia (pt); "
            f"language mismatches {mismatches}, incomplete turns {incomplete}; "
            f"{_provider_note(args.provider, settings)}"
        ),
        results_path=args.results,
    )
    print(f"Recorded phase {PHASE:02d} in {args.results}")
    # Exit 1 when an answer came back in the wrong language or a turn did not complete.
    return 0 if mismatches == 0 and incomplete == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
