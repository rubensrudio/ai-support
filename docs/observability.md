# Observability

Atlas traces every chat request with OpenTelemetry and exports the spans over OTLP HTTP to
[Langfuse](https://langfuse.com) (`api/observability/tracing.py`, DA-25). The dashboard required
by `OBS-03` (cost per conversation, p95 latency and fallback rate) lives **only** in Langfuse:
the app has no dashboard screen, and Langfuse access is restricted to the Operator (`OBS-07`).
The customer only sees the discreet `trace_id` next to each assistant answer (`OBS-06`).

If Langfuse is down or not configured, chat keeps working: the telemetry of that request is
dropped with a local `telemetry_dropped` warning, and the local structured log keeps the
`trace_id`, tokens and cost (`OBS-93`).

## What a trace contains

One trace per HTTP request. Its OTel trace id (32 hex chars) is the same `trace_id` written to the
logs, returned in the `X-Trace-Id` header and shown in the chat UI.

| Span | Main attributes |
| --- | --- |
| request (root) | `langfuse.session.id` = conversation id |
| `guardrail` | verdict |
| `retrieval` | chunk ids and scores |
| `llm` | `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `atlas.cost_usd`, masked prompt |
| `tool` | arguments and result |
| `fallback` | only when the provider was switched (`atlas.fallback`) |

Every span carries `langfuse.session.id`, so a Langfuse **session is one Atlas conversation**.
String attributes are masked by the PII guardrail before export (`GRD-07`, `OBS-91`). Customer
feedback becomes the numeric score `user_feedback` on the answer's trace (`OBS-02`).

## Run Langfuse locally

The self-hosted Langfuse v3 stack (web, worker, ClickHouse, Redis, MinIO and its own Postgres)
lives behind the `observability` compose profile, so a plain `docker compose up` never starts it.

```bash
docker compose --profile observability up -d
```

The first start takes a minute or two while the Langfuse migrations run. Every port binds to
`127.0.0.1` only: the UI at `http://localhost:3000` (`LANGFUSE_PORT`) and the MinIO S3 API at
`http://localhost:9090` (`LANGFUSE_MINIO_PORT`, used by the browser for media). Nothing is
reachable from other machines.

The profile bootstraps headlessly an organization and a project (`atlas-local`), an admin user and
a project API key pair. All of them are **local development values only**; never reuse them
anywhere else:

| Item | Default |
| --- | --- |
| Admin e-mail | `operator@atlas.local` (`LANGFUSE_INIT_USER_EMAIL`) |
| Admin password | `atlas-local-dev` (`LANGFUSE_INIT_USER_PASSWORD`) |
| Public key | `pk-lf-local-dev` |
| Secret key | `sk-lf-local-dev` |

Sign-up is disabled, so the bootstrap admin (the Operator) is the only user.

Stop it with `docker compose --profile observability down`; add `-v` to also wipe the
`langfuse-*` volumes.

## Configure the API

Set in `.env`:

```bash
LANGFUSE_HOST=http://localhost:3000
LANGFUSE_PUBLIC_KEY=pk-lf-local-dev
LANGFUSE_SECRET_KEY=sk-lf-local-dev
```

`LANGFUSE_HOST=http://localhost:3000` is for an API running on the host (`uv run uvicorn ...`).
When the API itself runs in compose, use `LANGFUSE_HOST=http://langfuse-web:3000`, because
`localhost` inside the container is the container. With the three variables set, `init_tracing`
exports to `{LANGFUSE_HOST}/api/public/otel/v1/traces`; with any of them empty, export is off and
nothing else changes.

Open `http://localhost:3000`, sign in as the Operator and pick the `atlas-support` project.
Traces show up a few seconds after a chat message.

## From a chat answer to its trace

1. In the chat UI, copy the `trace_id` shown under the assistant answer (or read the
   `X-Trace-Id` response header, or the `trace_id` field of the local log line).
2. In Langfuse, open **Tracing → Traces** and paste it in the search box (search by ID), or go
   straight to `http://localhost:3000/project/atlas-local/traces/<trace_id>`.
3. The trace view shows the span tree (`guardrail`, `retrieval`, `llm`, `tool`, `fallback`) with
   the masked inputs, the retrieved chunks and scores, tokens and cost.
4. **Sessions** groups every trace of the same conversation under the conversation id.

## Dashboard

Create it once per Langfuse project: **Dashboards → New dashboard**, name it `Atlas`, then add the
widgets below with **Add widget**. Every widget uses the dashboard date range (default: last 7
days, matching the retention) and, unless stated otherwise, no extra filter.

Cost note: Langfuse computes the cost of each `llm` observation from its model
(`gen_ai.request.model`) and token usage, using the model prices of the project
(**Settings → Models**). Register there the models and prices of `config/pricing.json` so the
Langfuse cost matches Atlas. The cost Atlas computed itself (`api/llm/pricing.py`) is on every
`llm` span as the attribute `atlas.cost_usd` (visible in the observation metadata) and in the
local log; use it to audit the Langfuse number.

### Cost per conversation

- **Widget 1 — "Cost per conversation"**
  - View: **Traces**
  - Metric: **Total Cost** (USD), aggregation **Sum**
  - Breakdown (dimension): **Session ID** (= `langfuse.session.id` = conversation id)
  - Chart: **Horizontal bar** (top 20), sorted descending
  - Filter: `Session ID` *is not null* (drops requests outside a conversation, such as login)
- **Widget 2 — "Average cost per conversation"**
  - View: **Traces**, Metric **Total Cost**, aggregation **Sum**, breakdown **Session ID**,
    chart **Big number** with aggregation **Average** over the sessions; if the Langfuse version
    does not average a breakdown, divide the total cost (Sum, no breakdown) by the session count
    (View **Traces**, Metric **Count unique Session ID**).
- Cross-check: open a session, then its `llm` observations, and add up `atlas.cost_usd`.

### p95 latency

- **Widget 3 — "p95 latency"**
  - View: **Traces**
  - Metric: **Latency**, aggregation **p95**
  - Breakdown: none; chart **Big number**
  - Filter: trace name equals the chat request root span (for example `POST /chat`), so health
    checks and static calls do not dilute the percentile
- **Widget 4 — "p95 latency over time"**
  - Same as widget 3, chart **Line**, time granularity **day** (or **hour** during a demo).
- Optional: View **Observations**, filter `Name = llm`, Metric **Latency** p95, breakdown
  **Model**, to see which model drives the tail.

### Fallback rate

Fallback rate = traces that contain a `fallback` span / all chat traces.

- **Widget 5 — "Fallbacks"**
  - View: **Observations**
  - Metric: **Count unique Trace ID** (or **Count** if unique trace count is not offered; one
    `fallback` span is created per provider switch)
  - Filter: `Name` equals `fallback`
  - Chart: **Big number** and a **Line** per day
- **Widget 6 — "Chat traces"**
  - View: **Traces**, Metric **Count**, same filter as widget 3; chart **Big number**.
- **Fallback rate** = widget 5 / widget 6 for the same date range. The widgets cannot divide two
  views, so read both numbers side by side (place them next to each other on the dashboard).
  In **Tracing → Traces**, filter by observation name `fallback` to list the affected traces.

## Public demo: Langfuse Cloud

The self-hosted stack is for local development only (LAC-36). The public demo on Render sends its
traces to **Langfuse Cloud**: create a Cloud project, set `LANGFUSE_HOST` (for example
`https://cloud.langfuse.com`), `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` as secret
environment variables of the service, and build the same dashboard above in that project.

Access control (`OBS-07`): only the Operator is a member of the Langfuse project, local or Cloud.
Do not invite customers, do not make traces or dashboards public, and do not expose the local
stack beyond `127.0.0.1`. The app never links to Langfuse; the customer only sees the `trace_id`.

## Retention

- Public demo: data is kept for 7 days (`ATLAS_RETENTION_DAYS`, DEMO-10). The maintenance job
  deletes the app rows and also deletes the Langfuse traces older than the cutoff through the
  public API (`LangfuseApi.delete_traces_older_than`, LAC-36).
- Locally, that purge only runs with `ATLAS_MAINTENANCE_ENABLED=true`; otherwise the local
  Langfuse keeps everything until you run `docker compose --profile observability down -v`.
- The local structured log keeps `trace_id`, tokens and cost for every request, even when export
  is off or Langfuse is down (`OBS-93`).
