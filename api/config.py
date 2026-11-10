"""Typed application settings (CT-1).

Every environment variable the project reads is declared here (DA-4): the rest of the code
only reads `get_settings()`. Secret values are `SecretStr`, so they never show up in `repr`,
`str` or logs (FND-90).
"""

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["local", "ci", "demo", "test"]
LLMProvider = Literal["anthropic", "fake", "claude_cli", "codex_cli"]
SUBSCRIPTION_PROVIDERS: frozenset[str] = frozenset({"claude_cli", "codex_cli"})
"""Providers that run through a logged-in CLI subscription (LAC-43): no API key, cost unknown."""
HistoryMode = Literal["full", "summarized"]
RetrievalMode = Literal["none", "fulltext", "vector_bruteforce", "vector_hnsw", "hybrid"]
TriageMode = Literal["prompt_only", "schema"]
PromptLayout = Literal["naive", "cache_ordered"]


class MissingConfigError(RuntimeError):
    """A required configuration variable is missing. The message names only the variable."""

    def __init__(self, var_name: str) -> None:
        super().__init__(f"Missing required configuration: {var_name}")
        self.var_name = var_name


def _env(name: str) -> AliasChoices:
    """Accept the exact environment variable name (case-insensitive) and the field name."""
    return AliasChoices(name, name.lower())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # App and database
    app_env: AppEnv = Field(default="local", validation_alias=_env("ATLAS_APP_ENV"))
    database_url: str = Field(
        default="postgresql+psycopg://atlas:atlas@localhost:5432/atlas",
        validation_alias=_env("ATLAS_DATABASE_URL"),
    )
    log_level: str = Field(default="INFO", validation_alias=_env("ATLAS_LOG_LEVEL"))

    # LLM providers
    llm_provider: LLMProvider = Field(
        default="anthropic", validation_alias=_env("ATLAS_LLM_PROVIDER")
    )
    anthropic_api_key: SecretStr | None = Field(
        default=None, validation_alias=_env("ANTHROPIC_API_KEY")
    )
    openai_api_key: SecretStr | None = Field(default=None, validation_alias=_env("OPENAI_API_KEY"))
    primary_model: str | None = Field(default=None, validation_alias=_env("ATLAS_PRIMARY_MODEL"))
    secondary_model: str | None = Field(
        default=None, validation_alias=_env("ATLAS_SECONDARY_MODEL")
    )
    guardrail_model: str | None = Field(
        default=None, validation_alias=_env("ATLAS_GUARDRAIL_MODEL")
    )
    judge_model: str | None = Field(default=None, validation_alias=_env("ATLAS_JUDGE_MODEL"))
    embedding_provider: Literal["openai", "fake"] = Field(
        default="openai", validation_alias=_env("ATLAS_EMBEDDING_PROVIDER")
    )
    embedding_model: str = Field(
        default="text-embedding-3-small", validation_alias=_env("ATLAS_EMBEDDING_MODEL")
    )
    embedding_dimensions: int = Field(
        default=1536, validation_alias=_env("ATLAS_EMBEDDING_DIMENSIONS")
    )
    pricing_file: str = Field(
        default="config/pricing.json", validation_alias=_env("ATLAS_PRICING_FILE")
    )

    # Subscription CLI providers (LAC-43): local manual testing only, never CI or public demo
    claude_cli_path: str = Field(default="claude", validation_alias=_env("ATLAS_CLAUDE_CLI_PATH"))
    codex_cli_path: str = Field(default="codex", validation_alias=_env("ATLAS_CODEX_CLI_PATH"))
    cli_timeout_seconds: float = Field(
        default=120.0, gt=0, validation_alias=_env("ATLAS_CLI_TIMEOUT_SECONDS")
    )

    # Resilience (phase 12)
    llm_timeout_seconds: float = Field(
        default=30.0, validation_alias=_env("ATLAS_LLM_TIMEOUT_SECONDS")
    )
    llm_max_retries: int = Field(default=3, validation_alias=_env("ATLAS_LLM_MAX_RETRIES"))
    retry_base_delay_seconds: float = Field(
        default=0.5, validation_alias=_env("ATLAS_RETRY_BASE_DELAY_SECONDS")
    )
    breaker_failure_threshold: int = Field(
        default=5, validation_alias=_env("ATLAS_BREAKER_FAILURE_THRESHOLD")
    )
    breaker_reset_seconds: float = Field(
        default=30.0, validation_alias=_env("ATLAS_BREAKER_RESET_SECONDS")
    )
    chaos_rate: float = Field(default=0.0, ge=0.0, le=1.0, validation_alias=_env("CHAOS_RATE"))

    # Conversation
    context_budget_tokens: int = Field(
        default=8000, gt=0, validation_alias=_env("ATLAS_CONTEXT_BUDGET_TOKENS")
    )
    history_mode: HistoryMode = Field(
        default="summarized", validation_alias=_env("ATLAS_HISTORY_MODE")
    )
    max_message_chars: int = Field(
        default=4000, gt=0, validation_alias=_env("ATLAS_MAX_MESSAGE_CHARS")
    )

    # Retrieval
    retrieval_mode: RetrievalMode = Field(
        default="hybrid",
        validation_alias=_env("ATLAS_RETRIEVAL_MODE"),
    )
    retrieval_top_k: int = Field(default=5, gt=0, validation_alias=_env("ATLAS_RETRIEVAL_TOP_K"))
    hnsw_m: int = Field(default=16, gt=0, validation_alias=_env("ATLAS_HNSW_M"))
    hnsw_ef_construction: int = Field(
        default=64, gt=0, validation_alias=_env("ATLAS_HNSW_EF_CONSTRUCTION")
    )
    hnsw_ef_search: int = Field(default=40, gt=0, validation_alias=_env("ATLAS_HNSW_EF_SEARCH"))
    rrf_k: int = Field(default=60, gt=0, validation_alias=_env("ATLAS_RRF_K"))

    # Agent
    agent_tools_enabled: bool = Field(
        default=True, validation_alias=_env("ATLAS_AGENT_TOOLS_ENABLED")
    )
    agent_max_steps: int = Field(default=8, gt=0, validation_alias=_env("ATLAS_AGENT_MAX_STEPS"))
    agent_max_cost_usd: Decimal = Field(
        default=Decimal("0.25"), validation_alias=_env("ATLAS_AGENT_MAX_COST_USD")
    )
    confirmation_ttl_minutes: int = Field(
        default=15, gt=0, validation_alias=_env("ATLAS_CONFIRMATION_TTL_MINUTES")
    )

    # Triage
    triage_mode: TriageMode = Field(default="schema", validation_alias=_env("ATLAS_TRIAGE_MODE"))
    triage_max_repairs: int = Field(
        default=2, ge=0, validation_alias=_env("ATLAS_TRIAGE_MAX_REPAIRS")
    )

    # Guardrails and prompt cache
    guardrails_enabled: bool = Field(
        default=True, validation_alias=_env("ATLAS_GUARDRAILS_ENABLED")
    )
    ingest_injection_scan: bool = Field(
        default=True, validation_alias=_env("ATLAS_INGEST_INJECTION_SCAN")
    )
    prompt_layout: PromptLayout = Field(
        default="cache_ordered", validation_alias=_env("ATLAS_PROMPT_LAYOUT")
    )

    # Auth and demo personas
    session_ttl_hours: int = Field(
        default=24, gt=0, validation_alias=_env("ATLAS_SESSION_TTL_HOURS")
    )
    cookie_secure: bool = Field(default=False, validation_alias=_env("ATLAS_COOKIE_SECURE"))
    persona_password: str = Field(
        default="atlas-demo", validation_alias=_env("ATLAS_PERSONA_PASSWORD")
    )
    operator_api_key: SecretStr | None = Field(
        default=None, validation_alias=_env("ATLAS_OPERATOR_API_KEY")
    )

    # Public demo limits and maintenance
    rate_limit_per_hour: int = Field(
        default=20, gt=0, validation_alias=_env("ATLAS_RATE_LIMIT_PER_HOUR")
    )
    daily_spend_cap_usd: Decimal = Field(
        default=Decimal("2.00"), validation_alias=_env("ATLAS_DAILY_SPEND_CAP_USD")
    )
    retention_days: int = Field(default=7, gt=0, validation_alias=_env("ATLAS_RETENTION_DAYS"))
    maintenance_enabled: bool = Field(
        default=False, validation_alias=_env("ATLAS_MAINTENANCE_ENABLED")
    )
    maintenance_interval_seconds: int = Field(
        default=300, gt=0, validation_alias=_env("ATLAS_MAINTENANCE_INTERVAL_SECONDS")
    )
    # Every LLM call writes its estimated cost to `llm_calls` (DEMO-03). The test suite turns it
    # off so unit tests never write to the developer's database; tests that need it turn it on.
    llm_ledger_enabled: bool = Field(
        default=True, validation_alias=_env("ATLAS_LLM_LEDGER_ENABLED")
    )

    # Observability
    langfuse_host: str | None = Field(default=None, validation_alias=_env("LANGFUSE_HOST"))
    langfuse_public_key: SecretStr | None = Field(
        default=None, validation_alias=_env("LANGFUSE_PUBLIC_KEY")
    )
    langfuse_secret_key: SecretStr | None = Field(
        default=None, validation_alias=_env("LANGFUSE_SECRET_KEY")
    )

    # Screen QA credentials (names only; values are a persona and its password)
    qa_username: str | None = Field(default=None, validation_alias=_env("ATLAS_QA_USERNAME"))
    qa_password: SecretStr | None = Field(default=None, validation_alias=_env("ATLAS_QA_PASSWORD"))

    @field_validator("database_url")
    @classmethod
    def _psycopg_driver(cls, value: str) -> str:
        """Managed Postgres (Render) hands out `postgres://` / `postgresql://` URLs; the app
        always talks through the async psycopg driver (DA-27)."""
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix) :]
        return value

    def require_llm_keys(self) -> None:
        """Fail fast when the configured LLM provider lacks its key or model.

        `fake` and the subscription CLI providers (`claude_cli`, `codex_cli`) need no key; the
        CLI providers use `primary_model` only when it is set (the CLI default otherwise).
        """
        if self.llm_provider == "anthropic":
            if self.anthropic_api_key is None or not self.anthropic_api_key.get_secret_value():
                raise MissingConfigError("ANTHROPIC_API_KEY")
            if not self.primary_model:
                raise MissingConfigError("ATLAS_PRIMARY_MODEL")

    def secret_values(self) -> list[str]:
        """Non-empty secret values, used by the log filter to redact them (FND-90)."""
        secrets = (
            self.anthropic_api_key,
            self.openai_api_key,
            self.operator_api_key,
            self.langfuse_public_key,
            self.langfuse_secret_key,
            self.qa_password,
        )
        return [s.get_secret_value() for s in secrets if s is not None and s.get_secret_value()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
