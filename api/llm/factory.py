"""LLM client factory (CT-9).

Builds the `LLMClient` the app uses from settings: `Instrumented(Anthropic)` or
`Instrumented(Fake)` by `ATLAS_LLM_PROVIDER`. The resilience chain is added in phase 12.
"""

from api.config import Settings
from api.llm.anthropic_adapter import PROVIDER as ANTHROPIC_PROVIDER
from api.llm.anthropic_adapter import AnthropicAdapter
from api.llm.client import LLMClient
from api.llm.fake import FAKE_MODEL, FAKE_PROVIDER, FakeLLMClient
from api.llm.instrumented import InstrumentedLLMClient


def build_llm_client(settings: Settings) -> LLMClient:
    if settings.llm_provider == "fake":
        return InstrumentedLLMClient(
            FakeLLMClient(script=[]), model=FAKE_MODEL, provider=FAKE_PROVIDER
        )
    settings.require_llm_keys()
    adapter = AnthropicAdapter(settings)
    return InstrumentedLLMClient(adapter, model=adapter.model, provider=ANTHROPIC_PROVIDER)
