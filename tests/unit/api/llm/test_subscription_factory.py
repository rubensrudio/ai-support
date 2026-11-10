"""The factory builds the subscription CLI adapters without any API key (LAC-43)."""

from collections.abc import Iterator
from typing import Any

import pytest

from api.config import Settings
from api.llm.claude_cli_adapter import ClaudeCliAdapter
from api.llm.codex_cli_adapter import CodexCliAdapter
from api.llm.factory import ResilientLLMClient, build_llm_client, reset_breakers
from api.llm.instrumented import InstrumentedLLMClient


@pytest.fixture(autouse=True)
def _fresh_breakers() -> Iterator[None]:
    reset_breakers()
    yield
    reset_breakers()


def _settings(**values: Any) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None, anthropic_api_key=None, openai_api_key=None, **values
    )


def _instrumented(client: ResilientLLMClient) -> InstrumentedLLMClient:
    """The primary's `InstrumentedLLMClient` inside `Breaker(Retry(...))`."""
    current: object | None = client.chain[0][1]
    while current is not None and not isinstance(current, InstrumentedLLMClient):
        current = getattr(current, "inner", None)
    assert isinstance(current, InstrumentedLLMClient)
    return current


def _adapter(instrumented: InstrumentedLLMClient) -> object:
    current: object = instrumented.inner
    while getattr(current, "inner", None) is not None:
        current = current.inner  # type: ignore[attr-defined]
    return current


@pytest.mark.parametrize(
    ("provider", "adapter_type", "default_model"),
    [
        ("claude_cli", ClaudeCliAdapter, "claude-subscription"),
        ("codex_cli", CodexCliAdapter, "codex-subscription"),
    ],
)
def test_factory_builds_subscription_adapter_without_key(
    provider: str, adapter_type: type, default_model: str
) -> None:
    client = build_llm_client(_settings(llm_provider=provider))

    assert isinstance(client, ResilientLLMClient)
    assert client.provider == provider
    assert client.model == default_model
    instrumented = _instrumented(client)
    assert isinstance(_adapter(instrumented), adapter_type)
    assert instrumented.provider == provider
    assert instrumented.model == default_model


@pytest.mark.parametrize("provider", ["claude_cli", "codex_cli"])
def test_factory_uses_primary_model_when_set(provider: str) -> None:
    client = build_llm_client(_settings(llm_provider=provider, primary_model="my-model"))

    assert client.model == "my-model"
    assert _instrumented(client).model == "my-model"
