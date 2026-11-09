"""The factory builds the subscription CLI adapters without any API key (LAC-43)."""

from typing import Any

import pytest

from api.config import Settings
from api.llm.claude_cli_adapter import ClaudeCliAdapter
from api.llm.codex_cli_adapter import CodexCliAdapter
from api.llm.factory import build_llm_client
from api.llm.instrumented import InstrumentedLLMClient


def _settings(**values: Any) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None, anthropic_api_key=None, openai_api_key=None, **values
    )


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

    assert isinstance(client, InstrumentedLLMClient)
    assert isinstance(client.inner, adapter_type)
    assert client.provider == provider
    assert client.model == default_model


@pytest.mark.parametrize("provider", ["claude_cli", "codex_cli"])
def test_factory_uses_primary_model_when_set(provider: str) -> None:
    client = build_llm_client(_settings(llm_provider=provider, primary_model="my-model"))

    assert isinstance(client, InstrumentedLLMClient)
    assert client.model == "my-model"
