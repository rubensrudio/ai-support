from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr

from api.config import MissingConfigError, Settings, get_settings

ENV_VARS = (
    "ATLAS_LLM_PROVIDER",
    "ANTHROPIC_API_KEY",
    "ATLAS_PRIMARY_MODEL",
    "ATLAS_CONTEXT_BUDGET_TOKENS",
    "ATLAS_MAX_MESSAGE_CHARS",
    "ATLAS_RATE_LIMIT_PER_HOUR",
    "ATLAS_DAILY_SPEND_CAP_USD",
    "CHAOS_RATE",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()


def _settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_defaults() -> None:
    settings = _settings()
    assert settings.context_budget_tokens == 8000
    assert settings.max_message_chars == 4000
    assert settings.rate_limit_per_hour == 20
    assert settings.daily_spend_cap_usd == Decimal("2.00")
    assert settings.chaos_rate == 0.0


@pytest.mark.parametrize(
    ("env_name", "raw", "field", "expected"),
    [
        ("ATLAS_CONTEXT_BUDGET_TOKENS", "1200", "context_budget_tokens", 1200),
        ("ATLAS_MAX_MESSAGE_CHARS", "2000", "max_message_chars", 2000),
        ("ATLAS_RATE_LIMIT_PER_HOUR", "50", "rate_limit_per_hour", 50),
        ("ATLAS_DAILY_SPEND_CAP_USD", "5.50", "daily_spend_cap_usd", Decimal("5.50")),
        ("CHAOS_RATE", "0.3", "chaos_rate", 0.3),
    ],
)
def test_env_overrides(
    monkeypatch: pytest.MonkeyPatch, env_name: str, raw: str, field: str, expected: object
) -> None:
    monkeypatch.setenv(env_name, raw)
    assert getattr(_settings(), field) == expected


def test_missing_anthropic_key_names_the_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # no `.env` here, so a developer's local key cannot leak in
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "anthropic")
    with pytest.raises(MissingConfigError) as exc_info:
        get_settings().require_llm_keys()
    assert "ANTHROPIC_API_KEY" in str(exc_info.value)
    assert exc_info.value.var_name == "ANTHROPIC_API_KEY"


def test_fake_provider_needs_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "fake")
    _settings().require_llm_keys()


def test_secrets_are_hidden_from_repr_and_str(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk-ant-super-secret-value"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    monkeypatch.setenv("ATLAS_OPERATOR_API_KEY", "operator-secret-value")
    settings = _settings()
    assert isinstance(settings.anthropic_api_key, SecretStr)
    for rendered in (repr(settings), str(settings)):
        assert secret not in rendered
        assert "operator-secret-value" not in rendered
    assert set(settings.secret_values()) == {secret, "operator-secret-value"}


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()
