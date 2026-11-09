# Atlas — AI customer support agent

Atlas is a customer support agent for a fictional e-commerce store, built from primitives: direct
LLM API calls, context management, retrieval (full-text, embeddings, pgvector), tool calling, an
agent loop, structured output, streaming, guardrails, prompt caching, resilience, evals and
observability. No agent framework hides the moving parts.

The stack is a FastAPI backend (Python 3.12, managed with `uv`), PostgreSQL 16 with pgvector, and
an Angular web app served by nginx.

## Quickstart

Requirements: Docker with Compose v2.

```bash
cp .env.example .env
# Edit .env and fill the variables listed in "Required configuration" below.
docker compose up
```

Then open <http://localhost:8080>. The API is also published on <http://localhost:8000>
(`GET /health`). On start the `api` container applies the database migrations
(`alembic upgrade head`) before serving.

| Service | Image | Port | Role |
|---|---|---|---|
| `db` | `pgvector/pgvector:pg16` | 5432 | PostgreSQL with pgvector |
| `api` | `Dockerfile` | 8000 | FastAPI app; migrates the database on start |
| `web` | `web/Dockerfile` | 8080 | Angular build on nginx; proxies `/api/` to `api` |

## Required configuration

Every setting lives in `.env` (template: `.env.example`) and is read by `api/config.py`. The
variables below have no safe default, so `.env.example` leaves them empty. Secrets are never
logged and never committed: `.env` is git-ignored and excluded from the Docker images.

| Variable | Needed for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | chat with `ATLAS_LLM_PROVIDER=anthropic` | Secret. Primary LLM provider. |
| `OPENAI_API_KEY` | embeddings and the secondary (fallback) model | Secret. |
| `ATLAS_PRIMARY_MODEL` | chat with `ATLAS_LLM_PROVIDER=anthropic` | Model id of the primary LLM. Required. |
| `ATLAS_SECONDARY_MODEL` | fallback when the primary provider fails | Model id. |
| `ATLAS_GUARDRAIL_MODEL` | input and output guardrails | Model id. |
| `ATLAS_JUDGE_MODEL` | LLM-as-judge evals | Model id. |
| `ATLAS_OPERATOR_API_KEY` | operator access to triage (`X-Operator-Key`) | Secret. Empty disables operator access. |
| `LANGFUSE_HOST` | tracing (optional `observability` profile) | URL of the Langfuse server. |
| `LANGFUSE_PUBLIC_KEY` | tracing | Secret. |
| `LANGFUSE_SECRET_KEY` | tracing | Secret. |
| `ATLAS_QA_USERNAME` | automated screen QA | A demo persona username. |
| `ATLAS_QA_PASSWORD` | automated screen QA | Secret. The persona password. |

To run everything offline (tests, smoke evals, demos with `--provider fake`), set
`ATLAS_LLM_PROVIDER=fake` and `ATLAS_EMBEDDING_PROVIDER=fake`; no key is needed then. The
default retrieval mode is `hybrid` (full-text + HNSW), which embeds every question: with
`ATLAS_EMBEDDING_PROVIDER=openai` and no `OPENAI_API_KEY`, the chat answers that the knowledge
base is unavailable. Use the fake embedder or `ATLAS_RETRIEVAL_MODE=fulltext` without a key. The
remaining variables in `.env.example` already have working defaults (database URL, timeouts,
retrieval and agent limits, demo limits).

## Knowledge base

- `python -m ingest run` ingests `corpus/` (chunks, full-text and embeddings). It stops before
  touching the database when the OpenAI embedder has no key; `--skip-embeddings` ingests without
  vectors and `--reembed` recomputes every vector after switching embedders
  ([ADR 0004](docs/adr/0004-embeddings.md)).
- `python -m ingest reindex-hnsw` rebuilds the HNSW index with the current `ATLAS_HNSW_M` and
  `ATLAS_HNSW_EF_CONSTRUCTION`; it locks the `chunks` table while it runs
  ([ADR 0005](docs/adr/0005-vector-database.md)).

## LLM providers

`ATLAS_LLM_PROVIDER` picks the adapter behind the `LLMClient` interface
([ADR 0001](docs/adr/0001-llm-api-call.md)).

| Provider | Requires | When to use |
|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` and `ATLAS_PRIMARY_MODEL` | Default. The app, the public demo and every measured demo run. |
| `fake` | nothing | Tests, CI and demos with `--provider fake`: scripted answers, no network. |
| `claude_cli` | Claude Code installed and logged in | Local manual testing with your subscription only. |
| `codex_cli` | Codex CLI installed and logged in | Local manual testing with your subscription only. |

The subscription CLI providers need no API key, but their cost is not measured
(`cost_usd` is logged empty), prompt caching cannot be controlled, tool calling is not supported
yet and latency is higher. Never use them in CI, for `docs/results.md` numbers or in the public
demo. Optional settings for them (defaults in parentheses):

- `ATLAS_CLAUDE_CLI_PATH` (`claude`): path of the Claude Code binary.
- `ATLAS_CODEX_CLI_PATH` (`codex`): path of the Codex CLI binary.
- `ATLAS_CLI_TIMEOUT_SECONDS` (`120`): the CLI process is killed after this many seconds.

### Phase 01 demo

`demo_01` sends "My name is Rubens" and then "What is my name?" (and the same in Portuguese) as
two independent calls, prints the answers and `name_remembered=<bool>`, and records the result in
the phase 01 row of `docs/results.md`:

```bash
uv run python -m demos.demo_01_stateless --provider fake --results /tmp/results.md  # offline
uv run python -m demos.demo_01_stateless --provider real   # configured provider, docs/results.md
```

## Development

Requirements: `uv`, Node.js 24, Docker.

```bash
uv sync                      # Python dependencies (runtime + dev)
docker compose up -d db      # PostgreSQL for the integration tests
(cd web && npm ci)           # web dependencies
```

Integration tests create their own database (`atlas_test_<uuid>`) on the compose Postgres for
each pytest session and drop it at the end.

### Quality gates

Run from the repository root:

```bash
uv run ruff check . --output-format=concise
uv run ruff format --check .
uv run mypy
uv run pytest -q             # JUnit report in reports/junit.xml
```

Run from `web/`:

```bash
npx ng lint
npx tsc --noEmit -p tsconfig.app.json
npx ng test --watch=false    # JUnit report in web/reports/junit.xml
npx ng build
```

CI (`.github/workflows/ci.yml`) runs the same gates on every push and pull request, plus the smoke
evals.

### Evals

Eval cases are JSONL files in `evals/datasets/`. Run a dataset against a target and write the JSON
report:

```bash
uv run python -m evals run --dataset evals/datasets/smoke.jsonl --target echo --out reports/eval-smoke.json
```

`--dataset` accepts a glob (quote it: `"evals/datasets/*.jsonl"`). Exit code 0 means the run
completed; 2 means an execution error (invalid dataset, unknown target, or a case that raised).

### Phase checklist

Each learning phase is done when it has eval cases, a demo script, a filled row in
`docs/results.md` and an English ADR in `docs/adr/`:

```bash
uv run python scripts/check_phase.py 01
```

## Phases

_Filled in as each phase ships._

## Results

Before/after measurements per phase are logged in [docs/results.md](docs/results.md).

## Architecture decisions

ADRs live in [docs/adr/](docs/adr/); new ones start from
[docs/adr/0000-template.md](docs/adr/0000-template.md).
