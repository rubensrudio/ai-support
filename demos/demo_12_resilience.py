"""Phase 12 demo: resilience under injected failures, primary only vs the full chain
(RES-08, DOD-02).

1. **Messages.** `--n` chat messages (default 50), en (persona `alex`) and pt (persona `bia`)
   alternating, taken from `QUESTIONS`. Every message opens a new conversation, so each one is a
   single chat model call.
2. **Run.** Every message goes through `ChatService` as its persona, in its own unit of work and
   trace, like `POST /chat`, with the agent tools as configured, no retriever (the chain only
   wraps model calls, so retrieval would only add the same latency to every mode) and the
   guardrails off (the topic check would be one more model call per message). Three modes run
   the same messages, each with its breakers reset first (`reset_breakers`), after one untimed
   warm-up message through the baseline client:
   - **baseline**: the full chain with no injected failure, for the latency reference;
   - **Before** = no resilience: `InstrumentedLLMClient(ChaosLLMClient(<primary>))`, i.e. the
     primary only with `CHAOS_RATE` = 0.3, no retry, no breaker, no fallback, no timeout;
   - **After** = the full chain of `api.llm.factory` with the same injected rate on the primary:
     `Fallback([primary: Breaker(Retry(Instrumented(Chaos(<primary>)))), secondary: ...])`.
3. **Metrics.** Final success rate (answers with `status='completed'`; a `degraded` answer, with
   its ticket, or an `error` is not a success) and the mean extra latency of a completed answer
   against the baseline's mean (end-to-end time of the message, database included). The
   `| 12 |` row of `docs/results.md` gets both, before and after.

The rate is passed to the clients built here (`ChaosLLMClient(rate=...)` and
`settings.model_copy(update={"chaos_rate": ...})`); the demo never sets `CHAOS_RATE` in the
environment, so nothing outside it ever sees failures injected (DEMO-05).

`--provider fake`: the primary is a `ScriptedProvider` named `fake` and the secondary a
`ScriptedProvider` named `openai` that simulates the OpenAI fallback (the factory never puts a
paid secondary behind the offline provider, so the demo builds the same chain itself, with the
process-wide breakers of `breaker_for`). Both always answer; the chaos draws come from
`random.Random(--seed)`, so a fake run is reproducible. Sleeps and clocks are real, with
`retry_base_delay_seconds` = 0.01 s so the retry backoff shows in the latency without slowing
the run. A fake run writes its results under `<tmp>/atlas-demo12/` unless `--results` is given,
so it never replaces the real `| 12 |` row.

`real` uses the configured provider (`build_llm_client`) and its settings: retries, delays and
timeouts as configured. The secondary exists only with both `OPENAI_API_KEY` and
`ATLAS_SECONDARY_MODEL`; with a subscription CLI (`claude_cli`, `codex_cli`) and no OpenAI key
there is no secondary, so the whole gain of the "after" mode comes from the retry (a message
whose four attempts all fail ends degraded, with a ticket). A real run makes about 3 x `--n`
model calls plus the retries.

Warning: the demo writes conversations, messages and (for degraded answers) tickets for the
demo personas in the configured database (`ATLAS_DATABASE_URL`).

Usage:
    uv run python -m demos.demo_12_resilience --provider fake --n 20 --results /tmp/results.md
    uv run python -m demos.demo_12_resilience --provider real   # writes docs/results.md
"""

import argparse
import asyncio
import random
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from api.auth.deps import CurrentCustomer
from api.chat.prompt_builder import language_instruction
from api.chat.schemas import ChatRequest
from api.chat.service import ChatService
from api.config import SUBSCRIPTION_PROVIDERS, Settings, get_settings
from api.db.engine import dispose_engine, session_scope
from api.errors import AtlasError
from api.llm.chaos import ChaosLLMClient
from api.llm.circuit_breaker import BreakerLLMClient, CircuitBreaker
from api.llm.client import LLMClient, LLMError, LLMRequest, LLMResponse, LLMStreamEvent, Usage
from api.llm.factory import (
    PRIMARY,
    SECONDARY,
    ResilientLLMClient,
    breaker_for,
    build_llm_client,
    primary_adapter,
    reset_breakers,
)
from api.llm.fake import FAKE_MODEL, FAKE_PROVIDER, split_deltas
from api.llm.instrumented import InstrumentedLLMClient
from api.llm.openai_adapter import PROVIDER as OPENAI_PROVIDER
from api.llm.retry import RetryingLLMClient
from api.messages import Lang
from api.observability.logging import bind_trace_id, new_trace_id, reset_trace_id
from demos.common import DEFAULT_RESULTS_PATH, demo_parser, record_result
from demos.demo_01_stateless import seed_demo_principals

PHASE = 12
CHAOS_RATE = 0.3
DEFAULT_N = 50
DEFAULT_SEED = 12
FAKE_RETRY_BASE_DELAY_S = 0.01
LANGUAGES: tuple[Lang, ...] = ("en", "pt")
Outcome = Literal["completed", "degraded", "error"]
Mode = Literal["baseline", "before", "after"]

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
    "en": "You can manage your subscription, your card and your orders from your account page.",
    "pt": "Você pode gerenciar a sua assinatura, o seu cartão e os seus pedidos na sua conta.",
}


@dataclass(frozen=True)
class DemoMessage:
    language: Lang
    text: str


@dataclass(frozen=True)
class MessageResult:
    language: Lang
    outcome: Outcome
    latency_ms: float


@dataclass(frozen=True)
class ModeSummary:
    messages: int
    completed: int
    degraded: int
    errors: int
    mean_latency_ms: float
    mean_completed_latency_ms: float | None  # None: no completed answer


@dataclass
class DemoRun:
    baseline: list[MessageResult]
    before: list[MessageResult]
    after: list[MessageResult]
    # `--provider fake`: answers served by each scripted provider in the "after" mode.
    served: dict[str, int] = field(default_factory=dict)


def messages_for(n: int) -> list[DemoMessage]:
    """`n` messages, en and pt alternating, cycling through each language's questions."""
    return [
        DemoMessage(
            LANGUAGES[index % 2],
            QUESTIONS[LANGUAGES[index % 2]][(index // 2) % len(QUESTIONS["en"])],
        )
        for index in range(n)
    ]


# --- fake providers and clients --------------------------------------------------------------


def _reply_language(request: LLMRequest) -> Lang:
    system = "\n".join(block.text for block in request.system)
    return "pt" if language_instruction("pt") in system else "en"


class ScriptedProvider:
    """`--provider fake` model: always answers, unless `fail` is set (then a 503)."""

    def __init__(self, provider: str, model: str = FAKE_MODEL) -> None:
        self.provider = provider
        self.model = model
        self.calls = 0
        self.fail = False

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if self.fail:
            raise LLMError(f"{self.provider} unavailable", recoverable=True, status=503)
        self.calls += 1
        return LLMResponse(
            text=FAKE_REPLIES[_reply_language(request)],
            tool_calls=[],
            usage=Usage(input_tokens=0, output_tokens=0),
            model=self.model,
            provider=self.provider,
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


def no_resilience(
    adapter: LLMClient, *, model: str, provider: str, rate: float, seed: int
) -> LLMClient:
    """The "before" client: the primary with injected failures, nothing around it."""
    chaos = ChaosLLMClient(adapter, rate, random.Random(seed))  # noqa: S311 - fault injection
    return InstrumentedLLMClient(chaos, model=model, provider=provider)


def _entry(
    name: str, adapter: LLMClient, model: str, provider: str, settings: Settings
) -> tuple[str, LLMClient, CircuitBreaker]:
    """One chain entry as the factory builds it: Breaker(Retry(Instrumented(adapter)))."""
    breaker = breaker_for(provider, settings)
    retrying = RetryingLLMClient(
        InstrumentedLLMClient(adapter, model=model, provider=provider),
        max_retries=settings.llm_max_retries,
        base_delay_s=settings.retry_base_delay_seconds,
        timeout_s=settings.llm_timeout_seconds,
    )
    return name, BreakerLLMClient(retrying, breaker), breaker


def fake_chain(
    settings: Settings, *, rate: float, seed: int
) -> tuple[ResilientLLMClient, ScriptedProvider, ScriptedProvider]:
    """The factory's chain over scripted providers: chaos on the primary (`fake`), the secondary
    simulating OpenAI. Returns the chain and both providers (to count who answered)."""
    primary = ScriptedProvider(FAKE_PROVIDER)
    secondary = ScriptedProvider(OPENAI_PROVIDER)
    chaos = ChaosLLMClient(primary, rate, random.Random(seed))  # noqa: S311 - fault injection
    chain = [
        _entry(PRIMARY, chaos, FAKE_MODEL, FAKE_PROVIDER, settings),
        _entry(SECONDARY, secondary, secondary.model, OPENAI_PROVIDER, settings),
    ]
    return ResilientLLMClient(chain, model=FAKE_MODEL, provider=FAKE_PROVIDER), primary, secondary


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
    llm: LLMClient, settings: Settings, principal: CurrentCustomer, message: DemoMessage
) -> MessageResult:
    """`POST /chat`: one message in its own unit of work and trace, timed end to end."""
    request = ChatRequest(message=message.text, ui_language=message.language)
    started = time.perf_counter()
    outcome: Outcome
    with _request_trace():
        try:
            async with session_scope() as session:
                service = ChatService(llm, settings, session)
                response = await service.collect(service.handle_message(request, principal))
        except AtlasError:  # the provider error reached the customer (LLM_UNAVAILABLE)
            outcome = "error"
        else:
            outcome = "degraded" if response.status == "degraded" else "completed"
            if response.status not in ("completed", "degraded"):
                outcome = "error"
    latency_ms = (time.perf_counter() - started) * 1000
    return MessageResult(language=message.language, outcome=outcome, latency_ms=latency_ms)


async def run_mode(
    mode: Mode,
    llm: LLMClient,
    settings: Settings,
    principals: dict[Lang, CurrentCustomer],
    messages: list[DemoMessage],
) -> list[MessageResult]:
    results: list[MessageResult] = []
    for number, message in enumerate(messages, start=1):
        result = await send_message(llm, settings, principals[message.language], message)
        print(
            f"[{mode}] message {number} [{message.language}]: {result.outcome} "
            f"({result.latency_ms:.1f} ms)"
        )
        results.append(result)
    return results


def _clients(
    provider: str, settings: Settings, seed: int
) -> tuple[dict[Mode, LLMClient], dict[str, ScriptedProvider]]:
    """The client of each mode (built after the breakers were reset) and, for `fake`, the
    scripted providers of the "after" mode."""
    if provider == "fake":
        baseline, _, _ = fake_chain(settings, rate=0.0, seed=seed)
        before = no_resilience(
            ScriptedProvider(FAKE_PROVIDER),
            model=FAKE_MODEL,
            provider=FAKE_PROVIDER,
            rate=CHAOS_RATE,
            seed=seed,
        )
        after, primary, secondary = fake_chain(settings, rate=CHAOS_RATE, seed=seed)
        clients: dict[Mode, LLMClient] = {"baseline": baseline, "before": before, "after": after}
        return clients, {PRIMARY: primary, SECONDARY: secondary}
    adapter, model, primary_provider = primary_adapter(settings)
    return {
        "baseline": build_llm_client(settings.model_copy(update={"chaos_rate": 0.0})),
        "before": no_resilience(
            adapter, model=model, provider=primary_provider, rate=CHAOS_RATE, seed=seed
        ),
        "after": build_llm_client(settings.model_copy(update={"chaos_rate": CHAOS_RATE})),
    }, {}


async def run_demo(provider: str, settings: Settings, n: int, seed: int) -> DemoRun:
    messages = messages_for(n)
    run_settings = settings.model_copy(update={"guardrails_enabled": False, "chaos_rate": 0.0})
    if provider == "fake":
        run_settings = run_settings.model_copy(
            update={"retry_base_delay_seconds": FAKE_RETRY_BASE_DELAY_S}
        )
    results: dict[Mode, list[MessageResult]] = {}
    served: dict[str, int] = {}
    try:
        principals = await seed_demo_principals(run_settings)
        # One untimed message first: the first one pays the connection pool and lazy imports,
        # which would otherwise inflate the baseline and make the extra latency look negative.
        warmup, _ = _clients(provider, run_settings, seed)
        first = messages[0]
        await send_message(warmup["baseline"], run_settings, principals[first.language], first)
        for mode in ("baseline", "before", "after"):
            reset_breakers()  # every mode starts with closed circuits
            clients, providers = _clients(provider, run_settings, seed)
            results[mode] = await run_mode(mode, clients[mode], run_settings, principals, messages)
            if mode == "after":
                served = {name: p.calls for name, p in providers.items()}
        return DemoRun(results["baseline"], results["before"], results["after"], served)
    finally:
        reset_breakers()  # leave no open circuit behind in this process
        # The engine's pooled connections belong to this event loop; close them here.
        await dispose_engine()


# --- report ----------------------------------------------------------------------------------


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize(results: list[MessageResult]) -> ModeSummary:
    completed = [r.latency_ms for r in results if r.outcome == "completed"]
    return ModeSummary(
        messages=len(results),
        completed=len(completed),
        degraded=sum(r.outcome == "degraded" for r in results),
        errors=sum(r.outcome == "error" for r in results),
        mean_latency_ms=_mean([r.latency_ms for r in results]),
        mean_completed_latency_ms=_mean(completed) if completed else None,
    )


def _percent(part: int, total: int) -> str:
    return f"{100 * part / total:.0f}%" if total else "n/a"


def rate_cell(summary: ModeSummary, baseline_ms: float) -> str:
    success = (
        f"success {summary.completed}/{summary.messages} "
        f"({_percent(summary.completed, summary.messages)}; "
        f"degraded {summary.degraded}, errors {summary.errors})"
    )
    if summary.mean_completed_latency_ms is None:
        return f"{success}, extra latency n/a (no completed answer)"
    extra = summary.mean_completed_latency_ms - baseline_ms
    return (
        f"{success}, extra latency {extra:+.1f} ms per completed answer "
        f"({summary.mean_completed_latency_ms:.1f} ms vs {baseline_ms:.1f} ms without chaos)"
    )


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {number}")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = demo_parser("Phase 12: resilience under 30% injected failures, primary only vs chain")
    parser.add_argument(
        "--n", type=_positive_int, default=DEFAULT_N, help="messages per mode (default 50)"
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help="seed of the injected failures of the 'before' mode (and of every fake mode)",
    )
    parser.set_defaults(results=None)
    return parser


def _provider_note(provider: str, settings: Settings, run: DemoRun) -> str:
    if provider == "fake":
        return (
            "provider=fake (scripted primary 'fake' and a scripted secondary simulating "
            f"'openai'; answers in the after mode: primary {run.served.get(PRIMARY, 0)}, "
            f"secondary {run.served.get(SECONDARY, 0)}; retry base delay "
            f"{FAKE_RETRY_BASE_DELAY_S}s, real sleeps; latency is in-process plus database)"
        )
    has_secondary = bool(
        settings.openai_api_key is not None
        and settings.openai_api_key.get_secret_value()
        and settings.secondary_model
    )
    secondary = (
        f"secondary openai {settings.secondary_model}"
        if has_secondary
        else "no secondary (OPENAI_API_KEY / ATLAS_SECONDARY_MODEL unset): the gain comes "
        "from the retry only"
    )
    cli = " (subscription CLI)" if settings.llm_provider in SUBSCRIPTION_PROVIDERS else ""
    return f"provider={settings.llm_provider}{cli}; {secondary}"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.results is None:
        # A fake run never replaces the real `| 12 |` row (same rule as demos 03 to 11).
        fake_results = Path(tempfile.gettempdir()) / "atlas-demo12" / "results.md"
        args.results = DEFAULT_RESULTS_PATH if args.provider == "real" else fake_results

    settings = get_settings()
    run = asyncio.run(run_demo(args.provider, settings, args.n, args.seed))
    baseline_ms = _mean([r.latency_ms for r in run.baseline])
    before, after = summarize(run.before), summarize(run.after)
    record_result(
        PHASE,
        (
            f"Resilience over {args.n} chat messages pt/en with 30% injected primary failures: "
            "final success rate and mean extra latency"
        ),
        rate_cell(before, baseline_ms),
        rate_cell(after, baseline_ms),
        notes=(
            f"CHAOS_RATE={CHAOS_RATE} on the primary, applied inside the demo only; "
            "Before: primary only (no retry, breaker, fallback or timeout); After: retry "
            f"(max {settings.llm_max_retries} retries, exponential backoff with jitter) + "
            "per-provider circuit breaker + fallback to the secondary, as built by "
            "api.llm.factory; success = answer status 'completed' (degraded answers open a "
            "ticket); extra latency = mean end-to-end time of a completed answer minus the "
            "mean of the same messages through the chain without chaos; one new conversation "
            "per message; agent tools as configured, no retrieval, guardrails off; personas "
            f"alex (en) and bia (pt); seed {args.seed}; "
            f"{_provider_note(args.provider, settings, run)}"
        ),
        results_path=args.results,
    )
    print(f"Recorded phase {PHASE:02d} in {args.results}")
    # Exit 1 when the chain did not do at least as well as the primary alone.
    return 0 if after.completed >= before.completed else 1


if __name__ == "__main__":
    sys.exit(main())
