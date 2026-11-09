"""LLM client factory (CT-9).

Builds the `LLMClient` the app uses from settings, by `ATLAS_LLM_PROVIDER`:
`Instrumented(Anthropic)`, `Instrumented(Fake)`, or one of the subscription CLI adapters
`Instrumented(ClaudeCli)` / `Instrumented(CodexCli)` (LAC-43, no key needed). The resilience
chain is added in phase 12.
"""

from api.config import Settings
from api.llm.anthropic_adapter import PROVIDER as ANTHROPIC_PROVIDER
from api.llm.anthropic_adapter import AnthropicAdapter
from api.llm.claude_cli_adapter import DEFAULT_MODEL as CLAUDE_CLI_MODEL
from api.llm.claude_cli_adapter import PROVIDER as CLAUDE_CLI_PROVIDER
from api.llm.claude_cli_adapter import ClaudeCliAdapter
from api.llm.client import LLMClient
from api.llm.codex_cli_adapter import DEFAULT_MODEL as CODEX_CLI_MODEL
from api.llm.codex_cli_adapter import PROVIDER as CODEX_CLI_PROVIDER
from api.llm.codex_cli_adapter import CodexCliAdapter
from api.llm.fake import FAKE_MODEL, FAKE_PROVIDER, FakeLLMClient
from api.llm.instrumented import InstrumentedLLMClient


def build_llm_client(settings: Settings) -> LLMClient:
    if settings.llm_provider == "fake":
        return InstrumentedLLMClient(
            FakeLLMClient(script=[]), model=FAKE_MODEL, provider=FAKE_PROVIDER
        )
    if settings.llm_provider == "claude_cli":
        return InstrumentedLLMClient(
            ClaudeCliAdapter(settings),
            model=settings.primary_model or CLAUDE_CLI_MODEL,
            provider=CLAUDE_CLI_PROVIDER,
        )
    if settings.llm_provider == "codex_cli":
        return InstrumentedLLMClient(
            CodexCliAdapter(settings),
            model=settings.primary_model or CODEX_CLI_MODEL,
            provider=CODEX_CLI_PROVIDER,
        )
    settings.require_llm_keys()
    adapter = AnthropicAdapter(settings)
    return InstrumentedLLMClient(adapter, model=adapter.model, provider=ANTHROPIC_PROVIDER)
