# ADR 0012: Retry, per-provider circuit breaker and fallback before the first token

- Status: accepted
- Date: 2026-11-09
- Phase: 12

## Context

Until this phase every chat turn depended on one call to one provider. Any 429, 5xx, timeout or
dropped connection reached the customer as `LLM_UNAVAILABLE` (HTTP 502, or an `error` event on
the stream), and a provider outage meant a dead chat. Real providers fail in exactly these ways,
and most of the failures are short: a retry a moment later usually works, and when it does not,
another provider usually does.

The requirements of the phase (RES-01..08, RES-91..93):

- abort a call that exceeds the timeout and treat it as recoverable (RES-01);
- retry recoverable failures (429, 5xx, timeout) with exponential backoff and jitter, up to a
  configured number of attempts, and never retry a non-recoverable one such as 400 or 401
  (RES-02, RES-03);
- stop calling a provider that keeps failing, per provider, until a re-evaluation period has
  passed (RES-04);
- serve the turn from a secondary model of another provider when the primary is out (RES-05);
- when both fail, answer with a degraded message and open a ticket with the conversation, the
  reason and the `trace_id` (RES-06), also when every circuit is open and no provider is called
  at all (RES-92);
- never run a tool twice because of a retry or a fallback (RES-91), and never switch provider
  after the customer already saw part of the answer (RES-93, decision LAC-29);
- inject failures on demand to show all of this (`CHAOS_RATE`, RES-07) and publish the effect:
  `demo_12` runs `--n` messages with 30% injected failures without and with the chain and writes
  the final success rate and the mean extra latency to the `| 12 |` row of `docs/results.md`
  (RES-08). The public demo runs with `CHAOS_RATE` off (DEMO-05).

## Decision

**One chain, built in one place.** `api.llm.factory.build_llm_client(settings)` returns

```
Fallback([
    ("primary",   Breaker(Retry(Instrumented(Chaos(<primary adapter>))))),
    ("secondary", Breaker(Retry(Instrumented(OpenAI)))),
])
```

Every client the app builds through the factory goes through it: the chat with its
summarisation, the guardrail topic check of `ATLAS_GUARDRAIL_MODEL`, and the triage
(`api/triage`), whose service turns an `AllProvidersFailed` into `AtlasError(TRIAGE_UNAVAILABLE,
503)` like any other provider failure (it has no degraded answer: the caller gets the error
envelope). The provider SDKs run with `max_retries=0` (DA-14): the retry is ours, and a second,
hidden retry inside the SDK would multiply the attempts and distort the numbers of `demo_12`.

**Cost is charged to the provider that answered.** Each chain entry has its own
`InstrumentedLLMClient`, so a call served by the secondary is logged (`llm_call`) and priced
with the secondary's model and provider, not the primary's. On a stream, that instrumented
client also sets `model` and `provider` on the `usage` event (an additive field of CT-6), so the
agent loop charges a streamed turn to the model that actually produced it even though the chain
itself names the primary.

**Retry only what is recoverable, with jitter and a timeout per attempt.**
`api.llm.retry.RetryingLLMClient` (CT-56) runs each attempt under `asyncio.timeout(timeout_s)`;
a timeout becomes `LLMTimeoutError`, which is recoverable. Only `LLMError.recoverable` errors
(429, 5xx, timeout, connection) are retried, up to `ATLAS_LLM_MAX_RETRIES` retries (default 3,
so 4 attempts), sleeping `base * 2**n * uniform(0.5, 1.5)` before retry `n`
(`ATLAS_RETRY_BASE_DELAY_SECONDS`, default 0.5 s). The jitter spreads the retries of concurrent
requests so they do not hit a recovering provider at the same instant. A 400 or 401 is raised at
once (RES-03). A cancellation of the caller is never turned into an error nor retried. Each retry
logs `llm_retry`.

**The timeout of the subscription CLIs.** The per-attempt timeout is `ATLAS_LLM_TIMEOUT_SECONDS`
(default 30 s) for the API providers and `ATLAS_CLI_TIMEOUT_SECONDS` (`cli_timeout_seconds`,
default 120 s) for `claude_cli` and `codex_cli`: a CLI turn starts a process and routinely takes
tens of seconds, so the API timeout would kill healthy calls and then retry them, paying the
same slow turn several times. The CLI adapters keep their own process timeout with the same
value. These providers are for local manual testing only (LAC-43), never CI or the public demo.

**A circuit breaker per provider and per process.** `api.llm.circuit_breaker.CircuitBreaker`
(CT-57): `closed` -> `open` after `ATLAS_BREAKER_FAILURE_THRESHOLD` consecutive failures
(default 5), `open` -> `half_open` after `ATLAS_BREAKER_RESET_SECONDS` (default 30 s), then one
trial call: success closes it, failure opens it again. A failure is one exhausted retry
sequence, so a single flaky attempt never trips it. `BreakerLLMClient` raises `CircuitOpen`
(recoverable) without calling the provider while it is open, and only recoverable errors count
as failures: a 400 says nothing about the provider's health. The factory keeps one breaker per
provider name in the process (`breaker_for`), shared by every client of that provider: the chat
and the guardrail clients see the same circuit. The state lives in memory, never in the database
or Redis (DA-24): the demo runs one instance, and a per-instance breaker is the usual trade-off
(each instance learns about an outage on its own, a few calls later).

**Fallback only before the first token.** `api.llm.fallback.FallbackLLMClient` (CT-59) tries
the providers in order, skipping those whose breaker does not allow a call. A recoverable failure
moves to the next provider (`llm_fallback` with `from`, `to` and `error_type`); a non-recoverable
one propagates. On a stream the chain switches provider only while the failed stream has emitted
no event at all (LAC-29, RES-93): once a token, a tool call or a usage event reached the
customer, replaying the turn on another provider would mix two answers or repeat a tool call, so
the error propagates as an `error` event and the customer may resend the message. The retry
follows the same rule: it retries a stream only before its first event. The secondary is OpenAI
(`api.llm.openai_adapter`, tools translated to its format), configured only when both
`OPENAI_API_KEY` and `ATLAS_SECONDARY_MODEL` are set, and never behind the offline `fake`
provider.

**Degraded answer with a ticket.** When every provider failed, or every circuit was open and none
was called, the chain raises `AllProvidersFailed`. `ChatService` turns it into
`user_message(DEGRADED_RESPONSE, language)` ("Please try again in a moment. We opened a ticket
and our team will follow up on your case."), stored with `status='degraded'`, a
`done{status: 'degraded'}`, and `open_ticket(reason='all_providers_failed', trace_id)` in the
same transaction, so the answer and the ticket are committed together (RES-06, RES-92). A
customer never sees a raw provider error for an outage the chain could not absorb.

**No tool is run again (RES-91).** The chain wraps model calls only, never the agent loop: a
retry or a fallback repeats the model call with the same request, including the tool results
already in it. Tools that ran in the turn keep their effects and audit rows, and the degraded
path commits them with the answer instead of rolling them back.

**Failure injection.** `api.llm.chaos.ChaosLLMClient` (CT-58) wraps only the primary adapter and,
with probability `CHAOS_RATE`, raises `LLMError(recoverable=True, status=503)` before calling it
(for a stream, before its first event). `CHAOS_RATE` defaults to 0, so nothing is injected unless
it is set; `demo_12` passes the rate to the clients it builds and never sets the variable.

**Measurement.** `demo_12` sends `--n` messages (default 50, en and pt) through `ChatService`
three times: a baseline through the chain with no injected failure, "before" with the primary
alone behind 30% injected failures (no retry, breaker, fallback or timeout) and "after" with the
full chain and the same rate. A success is an answer with `status='completed'`; the extra latency
is the mean time of a completed answer minus the baseline mean. With `--provider fake` (seeded
chaos, a scripted secondary simulating OpenAI, 0.01 s base delay) and `--n 20` the primary alone
completes 13/20 messages and the chain 20/20, paying a few milliseconds of backoff per retried
message. The real row comes from `--provider real`; with `claude_cli` and no OpenAI key there is
no secondary, so the gain there comes from the retry alone.

## Alternatives considered

- **Let the SDKs retry.** Both SDKs retry 429/5xx with backoff on their own. Rejected: it does not
  cover the CLI providers, it hides the attempts from our logs and from the breaker, and stacked
  with our own retry it would multiply the attempts (DA-14).
- **Retry every error.** Simpler, but a 400 or 401 fails the same way every time: retrying it
  only adds latency and cost, and a broken key would look like an outage. Rejected (RES-03).
- **One global circuit breaker, or none.** A global breaker would stop the secondary because the
  primary is down, the opposite of what the fallback needs; without a breaker every request in an
  outage pays the full retry sequence before falling back. Rejected for a breaker per provider.
- **Breaker state in the database or Redis.** Shared across instances, but it adds a dependency
  and a network call to every model call for a single-instance demo. Rejected (DA-24); it is the
  natural change when the API runs on several instances.
- **Fall back at any point of a stream.** Would save more turns, but the customer would see the
  answer restart from another model, and a tool call already emitted could run twice. Rejected
  (LAC-29, RES-93): an `error` event and a resend are honest and safe.
- **Return the provider error when everything fails.** What the app did before. Rejected: the
  customer gets no path forward and the team never hears about the failed conversation; the
  ticket closes that gap.

## Consequences

- Better: short provider failures are invisible to the customer; a provider outage costs one
  retry sequence per process until the breaker opens, then the turn goes straight to the
  secondary; a total outage ends in a polite answer and a ticket with the `trace_id`, never a raw
  error. `llm_retry`, `llm_breaker`, `llm_fallback` and `chat_degraded` make every step visible
  in the logs, and each `llm_call` still names the provider that served it.
- Worse: a recovered failure adds the backoff to the latency of that turn (seconds with the
  default delays, the "after" extra latency of `demo_12`); a call that times out may still have
  been billed (`cost_unknown`); the secondary answers with another model, so tone and tool use may
  differ slightly; and with several API instances each one keeps its own breaker.
- Follow-up: the phase 13 evals should run the degraded cases (`evals/datasets/phase_12.jsonl`)
  against the real chat target; a shared breaker store and a per-tenant retry budget are the next
  steps if the API ever scales out.
