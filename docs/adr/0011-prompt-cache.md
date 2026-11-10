# ADR 0011: Cache-ordered prompt layout with one stable prefix and a single cache breakpoint

- Status: accepted
- Date: 2026-11-09
- Phase: 11

## Context

Every chat turn sends the same large block of text before the part that actually changes: the
definitions of the agent tools (`registry.specs()`), the system prompt (`prompts/system.md`)
and the untrusted-documents rule of phase 10. That stable part is about 1.2k tokens
(`api.llm.tokens.count_tokens`), while the variable part of a short turn (excerpts, history and
question) is often smaller. Providers can cache a repeated prompt prefix: Anthropic reads a
cached prefix at a fraction of the input price and with lower latency, but only when the request
asks for it (`cache_control`) and only when the prefix is **byte-identical** to the one cached
before (CACHE-01, CACHE-02).

Before this phase the prompt broke that rule from its first line:

- the reply-language instruction ("Always reply in English" / "Brazilian Portuguese") sat at the
  top of the system prompt, so an en and a pt conversation never shared a prefix;
- a prompt that puts the current date and time on top (a common habit, and the `naive` layout
  kept here as the "before") changes on every call, so **nothing** after it can ever be reused;
- no block carried a cache marker, so the provider never wrote a cache entry anyway;
- the logs had the cache token fields (`llm_call` with `cache_read_tokens` /
  `cache_write_tokens`) but nothing produced them, and the cost was always priced as plain
  input.

The phase requirement is to **measure and publish** the effect: `demo_11` runs the same
conversations with both layouts and writes the cost and latency per call and the cache hit rate
to the `| 11 |` row of `docs/results.md` (CACHE-04).

## Decision

**Order: stable to variable, one breakpoint at the end of the stable part.**
`api.chat.prompt_builder` builds every chat request (`build_request` with retrieval and
`build_base_request` without it) in the layout of `settings.prompt_layout`
(`ATLAS_PROMPT_LAYOUT`, default `cache_ordered`). The provider reads a request as
tools -> system -> messages, so with `cache_ordered`:

1. the tools, as given by the caller, in their order;
2. `system[0]` = `grounded_system_prompt()`: `prompts/system.md` plus the untrusted-documents
   rule, with `cache_breakpoint=True`. It holds no language, date, time, customer or
   conversation data, so it is the same bytes for every conversation and language;
3. `system[1]` = the variable system text: `language_instruction(language)` and, when the
   history window was summarised, the summary;
4. the history window, then the last user turn with the `<untrusted_document>` excerpts and the
   `<customer_question>`.

Both chat paths (with and without RAG) share that prefix for the same tools, so a conversation
that switches between them keeps hitting the cache. The untrusted-documents rule stays in the
prefix even when no document is sent: dropping it would split the prefix in two.

**The language goes after the prefix (DA-23).** The reply language is the most variable thing
in the old system prompt (it changes per turn, since it follows the language of the current
message), so it moved to `system[1]`. It is still a system instruction, just after the cache
point; the phase 11 evals (`evals/datasets/phase_11.jsonl`) check that the reply language is
still right, including a turn whose message is in the other language of the history.

**Date and time never go on top.** The `naive` layout (the "before" of the demo) puts
"Current date and time (UTC): ..." and the language instruction at the top of a single system
block with no breakpoint. Any dynamic value at the top of the prompt invalidates the whole
cache; if a future feature needs the date, it goes after the breakpoint, like the language.

**A single breakpoint.** `cache_breakpoint` marks the end of the only stable prefix. The
Anthropic adapter (`api/llm/anthropic_adapter.py`) turns it into
`cache_control: {"type": "ephemeral"}` on that system block (the API accepts at most 4; extra
markers would only add write cost for prefixes that are never reused, since everything after
the first variable block changes per turn). Providers without explicit markers ignore it.

**Accounting.** The adapters fill `Usage.cache_read_tokens` and `Usage.cache_write_tokens`
(Anthropic: `cache_read_input_tokens` / `cache_creation_input_tokens`; `claude_cli`: the same
fields of the CLI's JSON report). `estimate_cost` prices them with the `cache_read` and
`cache_write` prices of `config/pricing.json`; `input_tokens` already excludes them. The
`InstrumentedLLMClient` logs both counts and the cost in the `llm_call` event (CACHE-03). A
provider without cache support reports zeros and the call works as before (CACHE-90). A
subscription CLI provider (`claude_cli`, `codex_cli`) logs its cache tokens but a cost of
`None` (`pricing_error="subscription"`).

**Measurement.** `demo_11` runs `--n` conversations (default 100, en/pt alternating, 2-3 turns
each) through `ChatService` with the agent tools and the configured retrieval, once with
`naive` and once with `cache_ordered` (`settings.model_copy`). The guardrails are off in both
runs to isolate the effect: they do not touch the chat prompt, and the topic check calls another
model. A wrapper around the chat client records, per call, the latency, the usage and the cost
(`estimate_cost`, unknown for a subscription provider); a cache hit is a call with
`cache_read_tokens > 0`. `--provider fake` uses a simulated cache that reads a byte-identical
tools + system prefix seen before: with `--n 5` (12 calls per layout) it gives 0/12 hits for
`naive` and 11/12 for `cache_ordered` (only the first call writes), about 1.1k cached tokens per
call. `fake-model` is priced at 0, so the fake cost column is $0 by design; the real row comes
from `--provider real`.

## Alternatives considered

- **Keep the language (and the date) in the system prompt.** Simplest, and how the prompt was
  written before, but every language gets its own prefix and the date makes every call a miss.
  Rejected: CACHE-02 requires identical prefixes across conversations.
- **One cached prefix per language.** Two system prompts, each cached. It halves the hit rate
  at low traffic (each cache entry expires on its own after about 5 minutes), duplicates the
  prompt text and still breaks when a turn switches language. Rejected.
- **Put the language in the user turn instead of the system.** Also cache-safe, but a user-turn
  instruction is weaker than a system one and mixes our instruction with customer text that we
  escape as data. Rejected in favour of a second system block after the breakpoint.
- **Several breakpoints (tools, system, history).** Caching the history prefix would help long
  conversations, but each write costs more than plain input and the history changes every turn
  in a short support chat, so most of those writes would never be read. Rejected for now; a
  breakpoint at the end of the history is the natural next step if conversations get long.
- **Rely on automatic prefix caching only (OpenAI style).** Works for the phase 12 fallback
  provider without markers, but Anthropic only caches marked prefixes. The layout serves both:
  the stable part is first either way.

## Consequences

- Better: every chat call after the first in the cache window reads about 1.1k tokens from the
  cache instead of paying them as input, for both languages and both chat paths; the gain grows
  with the tool catalogue and the system prompt.
- Better: the cost in `llm_call` now reflects the cache price, so the daily spend cap counts
  cached tokens correctly (AS-2).
- Worse: the first call of each cache window pays the write price (above the input price), and
  a prefix shorter than the model's minimum cacheable length is not cached at all. The stable
  prefix is close to that minimum for some models, so trimming tools or the system prompt can
  silently turn the cache off; `demo_11` with `--provider real` shows it as a 0% hit rate.
- Worse: any change to the tools, their descriptions, their order or `prompts/system.md`
  invalidates the cache for every conversation until the next write. Dynamic data (date, time,
  customer name, feature flags) must never be added to the stable block; the
  `test_prompt_cache_layout.py` tests fail if the prefix stops being byte-identical.
- With `claude_cli` the CLI adds its own system prompt and caching, so its cache numbers measure
  the CLI's cache, not only ours, and the cost stays unknown (subscription).
- Follow-up: the phase 12 OpenAI adapter reads its automatic cache from
  `usage.prompt_tokens_details.cached_tokens` into the same `cache_read_tokens` field. OpenAI's
  `usage.prompt_tokens` already **includes** those cached tokens, while `Usage.input_tokens`
  must exclude them (`estimate_cost` prices cache tokens only by `cache_read`), so the adapter
  must set `input_tokens = prompt_tokens - cached_tokens`; otherwise the cached tokens are
  billed twice, as input and as cache read, and the spend cap overcounts (AS-2).
