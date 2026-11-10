# ADR 0014: OpenTelemetry traces exported over OTLP to Langfuse, feedback as a score, observability feeding the evals

- Status: accepted
- Date: 2026-11-10
- Phase: 14

## Context

Until phase 13 every request already had a `trace_id` (32 hex characters, bound by
`TraceIdMiddleware`, returned in the `X-Trace-Id` header and written on every structured log
line with its tokens and cost). That is enough to grep one request, not to answer the questions
an Operator asks when a customer complains: which chunks did retrieval return, with which
scores, which model answered, how many tokens and how much did it cost, did a fallback fire,
which tool ran with which arguments?

The requirements of the phase (OBS-01..07, OBS-91..93):

- one trace per chat request with spans for the guardrail, `retrieval` (chunks and scores),
  every LLM call (model, tokens, cost, latency), every tool and the fallback (OBS-01);
- thumbs up / down on an answer, stored and linked to the trace of that answer (OBS-02), and a
  customer can never rate another customer's answer (OBS-92);
- a dashboard with cost per conversation, p95 latency and fallback rate that lives only in the
  observability tool, for the Operator (OBS-03, OBS-07, LAC-17); the customer sees only the
  `trace_id` next to each answer (OBS-06);
- personal data in a trace is masked as in GRD-07, never the original value (OBS-91);
- when the observability tool is down the chat keeps working, the telemetry of that request is
  dropped with a local warning and the local log keeps `trace_id`, tokens and cost (OBS-93,
  LAC-25);
- `demo_14` shows the loop complaint -> trace -> wrong chunk -> fix -> new eval case passing
  (OBS-05).

The project is a learning portfolio: the source brief asks for OpenTelemetry, and the backend
should stay replaceable (DA-25). The self-hosted Langfuse stack (web, worker, Postgres,
ClickHouse, Redis, MinIO) is heavy, and the public demo runs on a small PaaS (LAC-36).

## Decision

**OpenTelemetry SDK + OTLP HTTP, no Langfuse SDK.** `api/observability/tracing.py` owns a
`TracerProvider` (it never installs a global one). `init_tracing(settings)`, called by
`create_app`, adds a `BatchSpanProcessor` with the OTLP HTTP exporter pointed at
`{LANGFUSE_HOST}/api/public/otel/v1/traces` (Basic auth `public:secret`) only when
`LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are all set; without them spans
are still created (tests attach an `InMemorySpanExporter` with `add_span_exporter`) and nothing
leaves the process. Code instruments itself with `start_span(name, **attributes)`: the
retriever opens a `retrieval` span with `atlas.retrieval.mode`, `atlas.retrieval.k`,
`atlas.retrieval.chunk_ids`, `atlas.retrieval.doc_keys` and `atlas.retrieval.scores`; the
instrumented LLM client records `gen_ai.request.model`, `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens` and `atlas.cost_usd`; the fallback chain sets `atlas.fallback`;
guardrails and tools have spans of their own.

**One id from the log line to the Langfuse UI.** A custom `IdGenerator` gives every root span
the request trace id (`int(get_trace_id(), 16)`), so the `trace_id` of the logs, the
`X-Trace-Id` header, `messages.trace_id` and the id shown under each answer in the chat UI are
the OTel trace id that Langfuse displays. Pasting it in the Langfuse search opens the trace.

**Session = conversation.** `ChatService` binds the conversation id with `bind_session_id`, and
every span of the request carries it as `langfuse.session.id`, so Langfuse groups the turns of
one conversation and the dashboard can sum cost per conversation (OBS-03).

**Masking before export.** Every string attribute passes through `mask_pii` (the GRD-07 masker
of `api.guardrails.pii`) inside `start_span`, and exceptions are recorded with their type and
masked message only, never the SDK's raw `record_exception` stack trace (OBS-91).

**Export is best effort.** The OTLP exporter is wrapped so an exception or a failed export only
logs the warning event `telemetry_dropped` (failure class and span count, never payloads); the
batch processor exports off the request path, so a slow or absent Langfuse never delays an
answer (OBS-93). The local structured log still has `trace_id`, tokens and cost.

**Feedback is a score on the trace.** `POST /messages/{message_id}/feedback` with
`{"rating": "up" | "down"}` upserts one `feedback` row per message (migration `0010_feedback`)
with the message `trace_id`, and pushes the Langfuse score `user_feedback` (up = 1,
down = -1) in a background task through `LangfuseApi.push_score` (public API, best effort). One
ownership query joins `conversations` on the session customer and the assistant role, so a
missing message, a user message and another customer's message all get the same 404
`MESSAGE_NOT_FOUND` and nothing is written (OBS-92, AS-1).

**Observability feeds the evals.** A negative score is the entry point of a regression case:
the Operator filters the traces by `user_feedback = -1`, opens the `retrieval` span, sees which
`doc_key` and chunk the answer was grounded on, fixes the document, re-ingests and adds the
question to the eval dataset with the document that should have been cited. `demo_14` runs that
loop end to end: an outdated shipping-fee bulletin in a temporary copy of the corpus wins
retrieval, the answer quotes the old shipping fee, the customer rates it down through the
feedback route, the `retrieval` span (in-memory exporter, or the Langfuse API with `--langfuse`) names the culprit
`doc_key`, the bulletin is corrected and re-ingested, and the case is written to
`evals/datasets/phase_14.jsonl` (a temporary copy in the fake run) and passes the default `rag`
grader (citation + faithfulness) after failing it on the stale answer. The cases of
`phase_14.jsonl` (pt and en) are those regression cases.

**Langfuse local by profile, Langfuse Cloud in the demo.** The self-hosted stack runs only with
the compose profile `observability` (`docker compose --profile observability up`); the default
`docker compose up` stays light (DA-18). The public demo points `LANGFUSE_HOST` at Langfuse
Cloud, and the retention of the public demo deletes traces older than 7 days through the public
API (`LangfuseApi.delete_traces_older_than`, LAC-36). The dashboard of OBS-03 is built in Langfuse
and documented in `docs/observability.md`; the app has no dashboard screen (OBS-07).

## Alternatives considered

- **The Langfuse Python SDK (decorators / `@observe`).** Less code for the first trace and
  native Langfuse concepts, but it ties every instrumented module to one vendor and teaches a
  proprietary API instead of the OpenTelemetry one the brief asks for. With OTLP the same spans
  can go to Jaeger, Tempo or any OTel backend by changing one endpoint (DA-25).
- **A global tracer provider (`trace.set_tracer_provider`) plus auto-instrumentation of FastAPI,
  httpx and SQLAlchemy.** Many spans for free, but most of them are noise for an LLM trace, they
  carry raw URLs and SQL that bypass `mask_pii`, and a global provider makes tests leak
  exporters into each other. A module-owned provider with explicit spans keeps the trace small,
  masked and testable.
- **Random OTel trace ids with the request id as an attribute.** Simpler, but the id the
  customer reads under an answer would not open the trace directly; the Operator would need a
  metadata search for every complaint.
- **Storing feedback only in the app database.** Enough for OBS-02, but the Operator could not
  filter traces by bad feedback in the same tool where the spans are; the score is cheap and the
  database row stays the source of truth if the push fails.
- **Langfuse always on in compose.** Six more containers for everyone who only wants to run the
  chat; rejected for the profile (DA-18, LAC-36).

## Consequences

- Every complaint can be traced from the id on screen to the exact chunks, model, tokens, cost
  and fallback of that answer, and a bad rating becomes a reproducible eval case instead of an
  anecdote (`demo_14`, `phase_14.jsonl`).
- Telemetry can be lost: with Langfuse down, or the process killed before the batch flushes,
  spans are dropped by design (the log line with `trace_id`, tokens and cost remains). The
  feedback score push is also best effort; the `feedback` row is the durable record.
- Masking is only as good as `mask_pii`: personal data it does not detect reaches the trace.
  New span attributes must stay strings that go through `start_span`, never raw `set_attribute`
  calls with user text.
- The Langfuse public API routes (scores, traces, deletion) and the attribute mapping of its
  OTLP endpoint can change between versions (risk R-6); they live in module constants and must
  be re-checked when the Langfuse image is upgraded. The `--langfuse` mode of `demo_14` reads
  the trace back through the same API and therefore needs a few seconds of ingestion delay.
- Dashboards (cost per conversation, p95, fallback rate) are configuration in Langfuse, not
  code: they are documented in `docs/observability.md` and must be recreated by hand on a new
  Langfuse project.
