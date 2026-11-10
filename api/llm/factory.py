"""LLM client factory (CT-9, TASK-121, RES-05, RES-07, DA-24, DEMO-05).

`build_llm_client(settings)` builds the resilient chain the app uses:

    Fallback([
        ("primary",   Breaker(Retry(Instrumented(Chaos(<primary adapter>))))),
        ("secondary", Breaker(Retry(Instrumented(OpenAI)))),
    ])

- primary: the `ATLAS_LLM_PROVIDER` adapter (`anthropic`, the subscription CLIs `claude_cli` /
  `codex_cli` with no key (LAC-43), or the offline `fake`, whose empty script keeps the tests and
  `--provider fake` demos offline);
- chaos (`CHAOS_RATE`) wraps only the primary. With `fake` it is added only when the rate is
  above 0, so the fake answers exactly as scripted;
- secondary: OpenAI, only when both `OPENAI_API_KEY` and `ATLAS_SECONDARY_MODEL` are set (the
  adapter itself builds without them and fails on the call). Never behind `fake`: the offline
  provider never falls back to a paid API;
- retry: `ATLAS_LLM_MAX_RETRIES`, `ATLAS_RETRY_BASE_DELAY_SECONDS`, and a per-attempt timeout of
  `ATLAS_CLI_TIMEOUT_SECONDS` for the subscription CLIs (a CLI turn takes tens of seconds),
  `ATLAS_LLM_TIMEOUT_SECONDS` otherwise;
- breakers: one `CircuitBreaker` per provider and process (DA-24), named after the provider and
  shared by every client built here (the chat and the guardrail clients of the same provider see
  the same circuit). The threshold counts one failure per exhausted retry sequence. Each chain
  entry's client is a `BreakerLLMClient` over the same breaker the entry carries, so the chain
  reads the circuit that the client records.

The returned `ResilientLLMClient` names the primary's `model` and `provider`: the agent loop
charges a streamed call with them, and the chat service recognises the offline `fake` by its
`provider`. A call served by the secondary is still logged with its own identity by its
`InstrumentedLLMClient` (`llm_call`).
"""

from api.config import SUBSCRIPTION_PROVIDERS, MissingConfigError, Settings
from api.llm.anthropic_adapter import PROVIDER as ANTHROPIC_PROVIDER
from api.llm.anthropic_adapter import AnthropicAdapter
from api.llm.chaos import ChaosLLMClient
from api.llm.circuit_breaker import BreakerLLMClient, CircuitBreaker
from api.llm.claude_cli_adapter import DEFAULT_MODEL as CLAUDE_CLI_MODEL
from api.llm.claude_cli_adapter import PROVIDER as CLAUDE_CLI_PROVIDER
from api.llm.claude_cli_adapter import ClaudeCliAdapter
from api.llm.client import LLMClient
from api.llm.codex_cli_adapter import DEFAULT_MODEL as CODEX_CLI_MODEL
from api.llm.codex_cli_adapter import PROVIDER as CODEX_CLI_PROVIDER
from api.llm.codex_cli_adapter import CodexCliAdapter
from api.llm.fake import FAKE_MODEL, FAKE_PROVIDER, FakeLLMClient
from api.llm.fallback import FallbackLLMClient
from api.llm.instrumented import InstrumentedLLMClient
from api.llm.openai_adapter import PROVIDER as OPENAI_PROVIDER
from api.llm.openai_adapter import OpenAIAdapter
from api.llm.pricing import load_pricing_table
from api.llm.retry import RetryingLLMClient

PRIMARY = "primary"
SECONDARY = "secondary"

_breakers: dict[str, CircuitBreaker] = {}


class ResilientLLMClient(FallbackLLMClient):
    """The fallback chain, naming its primary's `model` and `provider`."""

    def __init__(
        self,
        chain: list[tuple[str, LLMClient, CircuitBreaker]],
        *,
        model: str,
        provider: str,
    ) -> None:
        super().__init__(chain)
        self.model = model
        self.provider = provider


def build_llm_client(settings: Settings) -> ResilientLLMClient:
    adapter, model, provider = primary_adapter(settings)
    if settings.chaos_rate > 0 or provider != FAKE_PROVIDER:
        adapter = ChaosLLMClient(adapter, settings.chaos_rate)
    timeout_s = (
        settings.cli_timeout_seconds
        if provider in SUBSCRIPTION_PROVIDERS
        else settings.llm_timeout_seconds
    )
    chain = [_entry(PRIMARY, adapter, model, provider, timeout_s, settings)]
    if provider != FAKE_PROVIDER and _secondary_configured(settings):
        secondary = OpenAIAdapter(settings)
        chain.append(
            _entry(
                SECONDARY,
                secondary,
                secondary.model,
                OPENAI_PROVIDER,
                settings.llm_timeout_seconds,
                settings,
            )
        )
    paid = [model] if provider in PAID_PROVIDERS else []
    if len(chain) > 1:
        paid.append(secondary.model)
    _require_pricing(settings, paid)
    return ResilientLLMClient(chain, model=model, provider=provider)


PAID_PROVIDERS = frozenset({ANTHROPIC_PROVIDER, OPENAI_PROVIDER})


def _require_pricing(settings: Settings, models: list[str]) -> None:
    """Fail fast when a paid model has no price while the spend ledger is on (DEMO-03, DA-15).

    The daily spend cap sums `llm_calls.cost_usd`; a paid model without an entry in the pricing
    file would be recorded at 0 and never reach the cap, so the client refuses to build.
    """
    if not settings.llm_ledger_enabled:
        return
    table = load_pricing_table(settings.pricing_file)
    for model in models:
        if model not in table:
            raise MissingConfigError(f"pricing entry for {model} in {settings.pricing_file}")


def breaker_for(provider: str, settings: Settings) -> CircuitBreaker:
    """The process-wide breaker of `provider`, created on first use with the settings' limits."""
    breaker = _breakers.get(provider)
    if breaker is None:
        breaker = CircuitBreaker(
            provider,
            failure_threshold=settings.breaker_failure_threshold,
            reset_seconds=settings.breaker_reset_seconds,
        )
        _breakers[provider] = breaker
    return breaker


def reset_breakers() -> None:
    """Forget every breaker of the process (tests and demos that need a closed circuit)."""
    _breakers.clear()


def primary_adapter(settings: Settings) -> tuple[LLMClient, str, str]:
    """The bare primary adapter of `ATLAS_LLM_PROVIDER` as `(adapter, model, provider)`.

    No chaos, retry, instrumentation or breaker: the caller wraps it (`build_llm_client`, or a
    demo that builds its own chain over the same primary). `fake` -> an empty-script
    `FakeLLMClient` charged as `fake-model`; `claude_cli` / `codex_cli` -> the CLI adapter with
    `ATLAS_PRIMARY_MODEL` or the CLI default (no key); `anthropic` -> `AnthropicAdapter`, raising
    `MissingConfigError` without `ANTHROPIC_API_KEY` or `ATLAS_PRIMARY_MODEL`.
    """
    if settings.llm_provider == "fake":
        return FakeLLMClient(script=[]), FAKE_MODEL, FAKE_PROVIDER
    if settings.llm_provider == "claude_cli":
        model = settings.primary_model or CLAUDE_CLI_MODEL
        return ClaudeCliAdapter(settings), model, CLAUDE_CLI_PROVIDER
    if settings.llm_provider == "codex_cli":
        model = settings.primary_model or CODEX_CLI_MODEL
        return CodexCliAdapter(settings), model, CODEX_CLI_PROVIDER
    settings.require_llm_keys()
    adapter = AnthropicAdapter(settings)
    return adapter, adapter.model, ANTHROPIC_PROVIDER


# Internal alias kept for existing callers; new code uses `primary_adapter`.
_primary = primary_adapter


def _secondary_configured(settings: Settings) -> bool:
    key = settings.openai_api_key
    return bool(key is not None and key.get_secret_value() and settings.secondary_model)


def _entry(
    name: str,
    adapter: LLMClient,
    model: str,
    provider: str,
    timeout_s: float,
    settings: Settings,
) -> tuple[str, LLMClient, CircuitBreaker]:
    breaker = breaker_for(provider, settings)
    retrying = RetryingLLMClient(
        InstrumentedLLMClient(adapter, model=model, provider=provider),
        max_retries=settings.llm_max_retries,
        base_delay_s=settings.retry_base_delay_seconds,
        timeout_s=timeout_s,
    )
    return name, BreakerLLMClient(retrying, breaker), breaker
