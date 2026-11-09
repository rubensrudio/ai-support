# ADR 0001: Call the LLM through our own provider-independent client

- Status: accepted
- Date: 2026-11-09
- Phase: 01

## Context

Phase 01 is the first time Atlas talks to a language model. The support agent will later add
retries, fallback to a second provider, prompt caching, streaming, tool calling, cost tracking and
tracing (phases 06 to 14), so every one of those concerns needs a single place to attach to.
The project also has two hard constraints: the test suite and CI must run offline with no API
key, and secrets must never reach the code, the logs or the repository.

The phase demo, `demos/demo_01_stateless.py`, sends "My name is Rubens" and then "What is my
name?" as two independent calls. Each call carries only the current message, so the second answer
cannot contain the name: a raw LLM call has no memory. The result is logged in the phase 01 row
of `docs/results.md`, and phase 02 adds conversation history to fix it.

## Decision

We call the model through our own small interface, `LLMClient` in `api/llm/client.py`
(`complete` and `stream` over provider-neutral `LLMRequest`, `LLMResponse`, `Usage`, `ToolCall`
types). Callers such as `api/chat/service.py` never import a provider SDK.

- One adapter per provider translates our types to the provider's API. The first one is
  `AnthropicAdapter` in `api/llm/anthropic_adapter.py`.
- `build_llm_client(settings)` in `api/llm/factory.py` picks the adapter from
  `ATLAS_LLM_PROVIDER` and wraps it in `InstrumentedLLMClient`, which logs one `llm_call` event
  with tokens, latency and cost from `config/pricing.json`.
- Keys and model ids come only from environment variables read by `api/config.py`
  (`ANTHROPIC_API_KEY`, `ATLAS_PRIMARY_MODEL`). They are `SecretStr` values, redacted from logs,
  and `require_llm_keys()` fails fast with the variable name when one is missing.
- `FakeLLMClient` in `api/llm/fake.py` replays a scripted list of responses and errors and
  records every request. It backs every test, CI (`ATLAS_LLM_PROVIDER=fake`) and the
  `--provider fake` mode of the demos.
- For local manual testing only (LAC-43), two more adapters run the logged-in subscription CLIs
  behind the same interface: `claude_cli` (Claude Code) and `codex_cli` (Codex CLI). They need no
  API key; the binaries and timeout come from `ATLAS_CLAUDE_CLI_PATH`, `ATLAS_CODEX_CLI_PATH` and
  `ATLAS_CLI_TIMEOUT_SECONDS`. Their limits are accepted on purpose:
  - cost is not measured: calls log `cost_usd=None` with `pricing_error="subscription"`;
  - there is no control over prompt caching;
  - tool calling is not supported in this phase (a request with tools is rejected);
  - latency is higher than a direct API call;
  - they are for personal local use only, never for CI, the measured demos or the public demo.

## Alternatives considered

- **Use the provider SDK directly in the chat service.** Fewer lines today, but every later
  concern (retry, fallback, caching, cost, tracing) would be tangled with one vendor's types, and
  tests would need to mock SDK internals or call the network.
- **Use an agent framework or a multi-provider wrapper library.** It would hide exactly the
  mechanics this project exists to teach, and its abstractions would decide how retries, caching
  and tool calls work instead of us.
- **Mock HTTP responses in tests instead of a fake client.** Possible with `httpx.MockTransport`
  and still used for the adapter's own tests, but a scripted fake at the interface level is
  simpler for every other test and lets the demos run offline.
- **API keys only, no subscription CLIs.** Simpler, but it forces paid API usage for every manual
  check. The CLI adapters sit behind the same interface, so they cost little to keep and nothing
  else depends on them.

## Consequences

- The chat service, the demos and the evals depend only on `LLMClient`, so adding the second
  provider, the resilience chain (phase 12) and streaming (phase 09) are decorators or adapters,
  not rewrites.
- Tests and CI never need a key or the network; a forgotten key fails at start-up with a clear
  message instead of on the first request.
- We maintain our own types and adapters, and each new provider feature (tool calls, cache
  breakpoints, structured output) has to be mapped by hand.
- Numbers recorded with a subscription CLI provider have no cost and different latency, so the
  measured demos and `docs/results.md` must always use an API-key provider.
- Phase 01 calls are stateless by design; conversation history is the follow-up (phase 02).
