"""Resilient client factory (TASK-121, CT-9, RES-05, RES-07, DA-24, DEMO-05).

`build_llm_client` returns `Fallback([Breaker(Retry(Instrumented(Chaos(primary)))),
Breaker(Retry(Instrumented(OpenAI)))])`: chaos only on the primary, the secondary only with
`OPENAI_API_KEY` and `ATLAS_SECONDARY_MODEL`, the CLI timeout per attempt for the subscription
providers, and one breaker per provider shared by every client of the process.
"""

from collections.abc import Iterator
from typing import Any

import pytest

from api.chat.service import OfflineTopicClient, _default_guardrail_llm
from api.config import MissingConfigError, Settings
from api.llm.anthropic_adapter import AnthropicAdapter
from api.llm.chaos import ChaosLLMClient
from api.llm.circuit_breaker import BreakerLLMClient, CircuitBreaker
from api.llm.claude_cli_adapter import ClaudeCliAdapter
from api.llm.client import LLMClient
from api.llm.codex_cli_adapter import CodexCliAdapter
from api.llm.factory import (
    ResilientLLMClient,
    build_llm_client,
    primary_adapter,
    reset_breakers,
)
from api.llm.fake import FakeLLMClient
from api.llm.fallback import FallbackLLMClient
from api.llm.instrumented import InstrumentedLLMClient
from api.llm.openai_adapter import OpenAIAdapter
from api.llm.retry import RetryingLLMClient


@pytest.fixture(autouse=True)
def _fresh_breakers() -> Iterator[None]:
    reset_breakers()
    yield
    reset_breakers()


def _settings(**values: Any) -> Settings:
    # `tests/conftest.py` sets `ATLAS_LLM_PROVIDER=fake` in the environment: name it here.
    defaults: dict[str, Any] = {
        "llm_provider": "anthropic",
        "anthropic_api_key": "sk-ant-test",
        "primary_model": "claude-test",
        "openai_api_key": None,
        "secondary_model": None,
    }
    defaults.update(values)
    return Settings(_env_file=None, **defaults)  # type: ignore[call-arg]


def _with_secondary(**values: Any) -> Settings:
    return _settings(openai_api_key="sk-openai-test", secondary_model="gpt-test", **values)


def _layers(client: LLMClient) -> list[object]:
    """The wrappers of one chain entry, outermost first, down to the adapter."""
    layers: list[object] = []
    current: object | None = client
    while current is not None:
        layers.append(current)
        current = getattr(current, "inner", None)
    return layers


def _entry(client: LLMClient, index: int) -> tuple[str, LLMClient, CircuitBreaker]:
    assert isinstance(client, FallbackLLMClient)
    return client.chain[index]


def test_chain_shape_with_primary_and_secondary() -> None:
    client = build_llm_client(_with_secondary())

    assert isinstance(client, ResilientLLMClient)
    assert [name for name, _, _ in client.chain] == ["primary", "secondary"]
    _, primary, primary_breaker = _entry(client, 0)
    _, secondary, secondary_breaker = _entry(client, 1)
    breaker, retry, instrumented, chaos, adapter = _layers(primary)
    assert isinstance(breaker, BreakerLLMClient)
    assert breaker.breaker is primary_breaker
    assert isinstance(retry, RetryingLLMClient)
    assert isinstance(instrumented, InstrumentedLLMClient)
    assert instrumented.provider == "anthropic"
    assert instrumented.model == "claude-test"
    assert isinstance(chaos, ChaosLLMClient)
    assert isinstance(adapter, AnthropicAdapter)
    breaker, retry, instrumented, adapter = _layers(secondary)
    assert isinstance(breaker, BreakerLLMClient)
    assert breaker.breaker is secondary_breaker
    assert isinstance(retry, RetryingLLMClient)
    assert isinstance(instrumented, InstrumentedLLMClient)
    assert instrumented.provider == "openai"
    assert instrumented.model == "gpt-test"
    assert isinstance(adapter, OpenAIAdapter)


def test_chaos_rate_wraps_only_the_primary() -> None:
    client = build_llm_client(_with_secondary(chaos_rate=0.3))

    _, primary, _ = _entry(client, 0)
    _, secondary, _ = _entry(client, 1)
    chaos = [layer for layer in _layers(primary) if isinstance(layer, ChaosLLMClient)]
    assert len(chaos) == 1
    assert chaos[0].rate == 0.3
    assert not any(isinstance(layer, ChaosLLMClient) for layer in _layers(secondary))


@pytest.mark.parametrize(
    ("key", "model"),
    [(None, None), ("sk-openai-test", None), (None, "gpt-test"), ("", "gpt-test")],
)
def test_no_secondary_without_openai_key_and_model(key: str | None, model: str | None) -> None:
    client = build_llm_client(_settings(openai_api_key=key, secondary_model=model))

    assert isinstance(client, FallbackLLMClient)
    assert [name for name, _, _ in client.chain] == ["primary"]


def test_retry_uses_the_settings() -> None:
    settings = _with_secondary(
        llm_max_retries=2, retry_base_delay_seconds=0.25, llm_timeout_seconds=12.0
    )
    client = build_llm_client(settings)

    for index in (0, 1):
        _, entry, _ = _entry(client, index)
        retry = _layers(entry)[1]
        assert isinstance(retry, RetryingLLMClient)
        assert retry.max_retries == 2
        assert retry.base_delay_s == 0.25
        assert retry.timeout_s == 12.0


@pytest.mark.parametrize(
    ("provider", "adapter_type"),
    [("claude_cli", ClaudeCliAdapter), ("codex_cli", CodexCliAdapter)],
)
def test_subscription_primary_uses_the_cli_timeout(provider: str, adapter_type: type) -> None:
    settings = _with_secondary(
        llm_provider=provider, cli_timeout_seconds=150.0, llm_timeout_seconds=20.0
    )
    client = build_llm_client(settings)

    _, primary, _ = _entry(client, 0)
    _, secondary, _ = _entry(client, 1)
    primary_layers = _layers(primary)
    assert isinstance(primary_layers[-1], adapter_type)
    primary_retry = primary_layers[1]
    secondary_retry = _layers(secondary)[1]
    assert isinstance(primary_retry, RetryingLLMClient)
    assert isinstance(secondary_retry, RetryingLLMClient)
    assert primary_retry.timeout_s == 150.0
    assert secondary_retry.timeout_s == 20.0


def test_breakers_are_one_per_provider_and_shared_in_the_process() -> None:
    settings = _with_secondary(breaker_failure_threshold=4, breaker_reset_seconds=9.0)
    first = build_llm_client(settings)
    second = build_llm_client(settings.model_copy(update={"primary_model": "guardrail-model"}))

    _, _, first_primary = _entry(first, 0)
    _, _, first_secondary = _entry(first, 1)
    assert _entry(second, 0)[2] is first_primary
    assert _entry(second, 1)[2] is first_secondary
    assert first_primary is not first_secondary
    assert first_primary.name == "anthropic"
    assert first_secondary.name == "openai"
    assert first_primary.failure_threshold == 4
    assert first_primary.reset_seconds == 9.0


def test_client_names_the_primary_model_and_provider() -> None:
    client = build_llm_client(_with_secondary())

    assert isinstance(client, ResilientLLMClient)
    assert client.provider == "anthropic"
    assert client.model == "claude-test"


def test_fake_keeps_the_chain_without_chaos_or_secondary() -> None:
    client = build_llm_client(_with_secondary(llm_provider="fake"))

    assert isinstance(client, ResilientLLMClient)
    assert client.provider == "fake"
    assert client.model == "fake-model"
    assert [name for name, _, _ in client.chain] == ["primary"]
    _, primary, breaker = _entry(client, 0)
    layers = _layers(primary)
    assert isinstance(layers[0], BreakerLLMClient)
    assert layers[0].breaker is breaker
    assert not any(isinstance(layer, ChaosLLMClient) for layer in layers)
    assert isinstance(layers[-1], FakeLLMClient)


def test_fake_gets_chaos_when_the_rate_is_set() -> None:
    client = build_llm_client(_settings(llm_provider="fake", chaos_rate=0.5))

    _, primary, _ = _entry(client, 0)
    assert any(isinstance(layer, ChaosLLMClient) for layer in _layers(primary))


def test_fake_chain_is_still_detected_as_the_offline_fake() -> None:
    client = build_llm_client(_settings(llm_provider="fake"))

    assert isinstance(_default_guardrail_llm(client), OfflineTopicClient)


@pytest.mark.parametrize(
    ("values", "adapter_type", "model", "provider"),
    [
        ({"llm_provider": "fake"}, FakeLLMClient, "fake-model", "fake"),
        ({}, AnthropicAdapter, "claude-test", "anthropic"),
        ({"llm_provider": "claude_cli"}, ClaudeCliAdapter, "claude-test", "claude_cli"),
        ({"llm_provider": "codex_cli", "primary_model": None}, CodexCliAdapter, None, "codex_cli"),
    ],
)
def test_primary_adapter_is_the_bare_configured_adapter(
    values: dict[str, Any], adapter_type: type, model: str | None, provider: str
) -> None:
    adapter, got_model, got_provider = primary_adapter(_settings(chaos_rate=0.5, **values))

    assert type(adapter) is adapter_type  # no chaos, retry, instrumentation or breaker
    assert got_provider == provider
    assert got_model == (model or "codex-subscription")


def test_primary_adapter_anthropic_without_key_raises_missing_config() -> None:
    with pytest.raises(MissingConfigError, match="ANTHROPIC_API_KEY"):
        primary_adapter(_settings(anthropic_api_key=None))


def test_build_llm_client_wraps_the_primary_adapter() -> None:
    settings = _settings()
    client = build_llm_client(settings)

    _, primary, _ = _entry(client, 0)
    expected, model, provider = primary_adapter(settings)
    assert type(_layers(primary)[-1]) is type(expected)
    assert (client.model, client.provider) == (model, provider)


def _pricing(tmp_path: Any, *models: str) -> str:
    import json

    price = {"input": 1, "output": 2, "cache_read": 0, "cache_write": 0}
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps({model: price for model in models}), encoding="utf-8")
    return str(path)


def test_paid_model_without_price_refuses_to_build_with_the_ledger_on(tmp_path: Any) -> None:
    settings = _with_secondary(
        llm_ledger_enabled=True, pricing_file=_pricing(tmp_path, "claude-test")
    )
    with pytest.raises(MissingConfigError, match="gpt-test"):
        build_llm_client(settings)


def test_paid_models_with_prices_build_with_the_ledger_on(tmp_path: Any) -> None:
    settings = _with_secondary(
        llm_ledger_enabled=True, pricing_file=_pricing(tmp_path, "claude-test", "gpt-test")
    )
    assert isinstance(build_llm_client(settings), ResilientLLMClient)


def test_ledger_off_skips_the_pricing_check(tmp_path: Any) -> None:
    settings = _settings(llm_ledger_enabled=False, pricing_file=_pricing(tmp_path, "other"))
    assert isinstance(build_llm_client(settings), ResilientLLMClient)
