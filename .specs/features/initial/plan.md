# Plano Técnico — Atlas: Agente de Suporte com IA (projeto completo, fases 0–14 + demo pública)

> Entradas: `spec.md` (revisão 3, 173 requisitos ativos), `decisions.md` (LAC-01..37), documento-fonte
> `Projeto de Portfólio — Agente de Suporte com IA.md`. Repositório greenfield (`ai-support`, branch `develop`,
> só com `.specs/` e o documento-fonte): todo caminho citado aqui é **novo** e todo comando é **declarado por este
> plano** e materializado pela task indicada na coluna Origem (ver R-1).

## 1. Resumo Executivo

O Atlas é um assistente de suporte da loja fictícia Assinaflow que evolui em 14 fases, e cada fase resolve o problema
criado pela anterior com uma métrica de antes e depois. Este plano constrói tudo num monorepo na raiz atual
(`ai-support/`), mantendo a estrutura proposta pelo documento-fonte sem a pasta `atlas-support-agent/`: `api/`
(FastAPI), `ingest/`, `evals/`, `demos/`, `prompts/`, `corpus/`, `web/` (Angular), `docs/` e `docker-compose.yml`.
A stack é a do documento-fonte: Python 3.12 + FastAPI async + Pydantic v2, PostgreSQL 16 + pgvector, Angular, Claude
como primário e OpenAI como secundário, OpenTelemetry + Langfuse, pytest + JSONL, Docker Compose e GitHub Actions.
Nenhum framework de agente entra: loop, retriever, fallback e guardrails são escritos à mão.

O trabalho está quebrado em 150 tasks agrupadas por fase. Cada fase termina numa task de fechamento (ADR + casos de
eval). As tasks da fase seguinte dependem dela, como uma barreira (DA-28), para que o Operador possa criar a tag
`phase-NN` num commit sem código da fase seguinte. Todo comportamento "antes" de cada fase continua selecionável por
configuração (DA-16), então qualquer `demo_NN` reproduz o problema tanto na sua tag quanto em HEAD. Os demos rodam com
`--provider fake` no CI e nos testes. A métrica real em `docs/results.md` vem do Operador, que roda cada demo com as
chaves reais (seção 16).

As áreas sensíveis reais são autenticação/autorização por sessão (as tools nunca recebem a identidade do modelo),
controle do gasto real com a API de LLM (teto diário), dados pessoais digitados por visitantes (mascarados, com
expurgo de 7 dias), segredos e quatro integrações de terceiros (Anthropic, OpenAI, Langfuse, Render). Elas concentram
as 29 tasks `alto`/`crítico` (21 críticas, 8 altas). O risco de 9 delas foi decidido pelo humano
(LAC-38..41).

## 2. Premissas e Lacunas

### 2.1 Decidido pelo humano (de `decisions.md`)

| ID | Decisão | Consequência no plano |
|---|---|---|
| LAC-01 | Produto bilíngue PT/EN (C) | Corpus em `corpus/en/` e `corpus/pt/`; `chunks.language` e `ts_config` por idioma (DA-13); `api/chat/language.py`; `web/src/app/core/i18n/`; datasets de eval e red-team com pt e en |
| LAC-02 | Personas fixas com restauração diária (C) | `api/seed/personas.py` (≥ 2 personas), `api/maintenance/restore.py`, `api/maintenance/scheduler.py` |
| LAC-03 | Confirmação só para `cancel_order` e `issue_refund` (A) | `destructive=True` só nessas duas em `api/tools/catalog.py` (CT-43) |
| LAC-04 | Recusa volta ao modelo; repetição recusada vira erro (A) | `AgentLoop.resume` e `refused_calls` no `AgentState` (CT-45); `ACTION_ALREADY_REFUSED` |
| LAC-05 | `issue_refund` valida a política no servidor (A) | `api/tools/refunds.py` reavalia `evaluate_refund` com o pedido travado |
| LAC-06 | Elegível se cancelado antes do envio ou entregue há ≤ 7 dias; valor integral (A) | Função pura `api/tools/refund_policy.py` (CT-41); política escrita no corpus nos dois idiomas |
| LAC-07 | Cancelável só em pendente/pago (A) | `UPDATE ... WHERE status IN ('pending','paid')` atômico (DA-21) |
| LAC-08 | Limite de passos/custo encerra e lista as ações (A) | `AGENT_LIMIT_REACHED` + `list_successful_actions(trace_id)` (CT-39) |
| LAC-09 | Configurável, padrão 20 msg/h por conta e US$ 2/dia, reset 00:00 UTC (C) | `ATLAS_RATE_LIMIT_PER_HOUR`, `ATLAS_DAILY_SPEND_CAP_USD`; `api/limits/*` |
| LAC-10 | Triagem recebe texto livre (A) | `TriageRequest{text}` com `extra=forbid` (CT-48) |
| LAC-11 | Enums fechados na triagem (A) | `Literal[...]` em `TriageResult` |
| LAC-12 | JSON inválido após 2 reparos → erro tipado (A) | `TRIAGE_UNAVAILABLE` 503 |
| LAC-13 | Injection na entrada bloqueia sem chamar o modelo (A) | `InputGuard` → `block` (CT-54) |
| LAC-14 | Dado pessoal mascarado antes do LLM e antes de persistir (A) | `mask_pii` (CT-53) aplicado antes de `append_message`, logs e spans |
| LAC-15 | "Fora da política" = dado alheio + promessa não executada + fora do suporte (A) | `OutputGuard` com 3 checagens (CT-55) |
| LAC-16 | Cancelamento salva a parcial como cancelada; tools permanecem (A) | `messages.status='cancelled'` desde a migration 0003 (DA-8) |
| LAC-17 | `trace_id` na UI; dashboard só na ferramenta (A) | `message-meta.component.ts`; `docs/observability.md`; nenhuma tela de dashboard |
| LAC-20 | Orçamento de contexto configurável, padrão 8.000 (A) | `ATLAS_CONTEXT_BUDGET_TOKENS`; `api/chat/context.py` |
| LAC-21 | 2ª repetição seguida → erro; 3ª encerra (A) | `RepeatDetector` (CT-47) |
| LAC-22 | Score 0–100, base = branch principal, bloqueia se o geral cair > 2 (A) | `evals/gate.py` (exit 0/1/2), `.github/workflows/evals.yml` |
| LAC-23 | Retenção de 7 dias (A) | `ATLAS_RETENTION_DAYS=7`; `api/maintenance/purge.py` |
| LAC-24 | Alucinação por LLM-as-judge + revisão manual por amostra (A) | `evals/judge.py`; `demos/demo_03_rag.py` gera `docs/review/phase03_sample.csv` |
| LAC-25 | Observabilidade fora do ar → atendimento segue, telemetria descartada (A) | `BatchSpanProcessor` com exportador que nunca propaga erro + log `telemetry_dropped` |
| LAC-26 | Mensagem máx. configurável, padrão 4.000 (C) | `ATLAS_MAX_MESSAGE_CHARS` |
| LAC-27 | `send_email` sempre para o e-mail da sessão (A) | `SendEmailArgs` sem destinatário; registro rejeita campos de destinatário (CT-40) |
| LAC-28 | Guardrail que falha bloqueia (fail-closed) (A) | Exceção no guard → `block` com `guard_failure` |
| LAC-29 | Fallback só antes do 1º token (C) | `FallbackLLMClient.stream` (CT-59) |
| LAC-30 | Moeda única USD, formatada pelo idioma (A) | `CHECK (currency='USD')`; `web/src/app/shared/money.pipe.ts` |
| LAC-31 | Outro idioma → aviso em inglês, sem conteúdo e sem tools (A) | `detect_language` → `other` → `UNSUPPORTED_LANGUAGE` sem LLM |
| LAC-32 | Idioma inicial da UI pelo navegador, senão EN (A) | `I18nService` (CT-24) |
| LAC-33 | Sem base anterior → primeiro run vira a base (A) | `evals/gate.py` sem `--base` → exit 0 e `baseline_created` |
| LAC-34 | Restauração apaga seed e conversas; traces/auditoria só pelo expurgo (A) | `restore_demo` não toca em `audit_log`, `email_outbox`, `tickets` |
| LAC-35 | Triagem por sessão de persona OU `X-Operator-Key` (C) | `require_customer_or_operator` (CT-16); limites por tipo de principal em `api/limits/deps.py` |
| LAC-36 | Langfuse auto-hospedado só local; demo usa Langfuse Cloud; expurgo apaga traces > 7 dias via API (A) | Perfil `observability` no compose; `LANGFUSE_HOST` aponta para o Cloud no Render; `LangfuseApi.delete_traces_older_than` (CT-63) |
| LAC-37 | ~20 documentos × 2 idiomas = ~40 arquivos (A) | `corpus/manifest.yaml` com ~20 `doc_key`, cada um em `corpus/en/` e `corpus/pt/` |
| LAC-38 | TASK-031 alto (AS-3); TASK-019 e TASK-094 médio (C) | Risco/âncora no `tasks.md` |
| LAC-39 | TASK-068 e TASK-072 crítico (AS-1) (A) | Risco/âncora no `tasks.md` |
| LAC-40 | TASK-107 alto (AS-3); TASK-111 crítico (AS-2) (B) | Risco/âncora no `tasks.md` |
| LAC-41 | TASK-001 médio; TASK-149 alto (AS-8) (A) | Risco/âncora no `tasks.md` |

### 2.2 Premissas assumidas (lacunas não bloqueantes)

| ID | Premissa | Reversibilidade | Onde impacta |
|---|---|---|---|
| LAC-18 | Só a busca híbrida com RRF entra (sem reranker) | alta | `api/rag/hybrid.py`, `demos/demo_05_hnsw.py` |
| LAC-19 | Um único provedor de embeddings, sem comparação obrigatória: OpenAI `text-embedding-3-small`, 1536 dimensões (Jev choice p=0.63: openai, local 0.27, voyage 0.10) | alta (trocar `ATLAS_EMBEDDING_*` + reingestão) | `api/rag/embeddings.py`, migration 0005 |
| P-1 | Provedor secundário de LLM = OpenAI (Jev choice p=0.80) | média | `api/llm/openai_adapter.py`, `api/llm/factory.py` |
| P-2 | Modelos configurados por variável (`ATLAS_PRIMARY_MODEL`, `ATLAS_SECONDARY_MODEL`, `ATLAS_GUARDRAIL_MODEL`, `ATLAS_JUDGE_MODEL`), sem valor padrão fixado no código; preços em `config/pricing.json` | alta | `api/config.py`, `config/pricing.json` |
| P-3 | Limite de custo por requisição do agente (AGT-03), padrão US$ 0,25 (`ATLAS_AGENT_MAX_COST_USD`) | alta | `api/agent/guards.py` |
| P-4 | Confirmação pendente expira em 15 min (`ATLAS_CONFIRMATION_TTL_MINUTES`) | alta | `api/agent/confirmations.py` |
| P-5 | Sessão expira em 24 h (`ATLAS_SESSION_TTL_HOURS`) | alta | `api/auth/service.py` |
| P-6 | Chunk de ~500 tokens com sobreposição de 80; top-k = 5 (`ATLAS_RETRIEVAL_TOP_K`) | alta | `ingest/chunking.py`, `api/rag/retriever.py` |
| P-7 | Detecção de idioma: `other` só com ≥ 20 letras e p ≥ 0,90; caso contrário, idioma da UI | alta | `api/chat/language.py` |
| P-8 | PaaS da demo = Render (Jev choice p=0.85) | média | `render.yaml` |
| P-9 | Personas `alex` (en) e `bia` (pt), senha pública via `ATLAS_PERSONA_PASSWORD` publicada no README | alta | `api/seed/personas.py`, `README.md` |
| P-10 | PDFs do corpus gerados a partir de fontes Markdown por `scripts/build_corpus_pdfs.py` e versionados | alta | `corpus/pdf/` |
| P-11 | Guardrail de saída em streaming libera texto por frase aprovada (DA-22); TTFT inclui esse buffer e isso é documentado na ADR 0010 | média | `api/guardrails/stream_filter.py` |

### 2.3 Lacunas ainda abertas

Nenhuma. As decisões de risco de 9 tasks (LAC-38..41) já foram tomadas pelo humano e aplicadas no `tasks.md`.

## 3. Ambiente e Comandos de Verificação

Repositório greenfield: nenhum arquivo de build existe ainda. Os comandos abaixo são **declarados por este plano**
seguindo a convenção do autor observada em `~/Projects/support-ticket-ai/pyproject.toml` e
`~/Projects/interview-reviewer/{backend/pyproject.toml,frontend/angular.json,frontend/vitest-base.config.ts}`. A task
da coluna Origem cria cada um, e o `Done when` dela executa o comando. Ferramentas confirmadas na máquina:
`uv 0.12.15`, `node v24.19.0`, Angular CLI `22.1.4` (global), `docker`. As flags `--include`, `--reporters` e
`--output-file` do `ng test` foram conferidas no schema do builder `@angular/build:unit-test` 22.1.4
(`~/Projects/complexity-patters/node_modules/@angular/build/src/builders/unit-test/schema.json`).

| Alvo | Comando | Diretório | Relatório | Origem |
|---|---|---|---|---|
| Instalar dependências | `uv sync` | `.` | — | `pyproject.toml` + `uv.lock` (TASK-001) |
| Instalar dependências | `npm ci` | `web` | — | `web/package.json` + `web/package-lock.json` (TASK-012) |
| Lint | `uv run ruff check . --output-format=concise` | `.` | — | `pyproject.toml` `[tool.ruff]` (TASK-001) |
| Lint | `npx ng lint` | `web` | — | `web/angular.json` target `lint` (angular-eslint, TASK-012) |
| Typecheck | `uv run mypy` | `.` | — | `pyproject.toml` `[tool.mypy] files = ["api","ingest","evals","demos","scripts"]` (TASK-001) |
| Typecheck | `npx tsc --noEmit -p tsconfig.app.json` | `web` | — | `web/tsconfig.app.json` (TASK-012) |
| Teste (suíte) | `uv run pytest -q` | `.` | `reports/junit.xml` | `pyproject.toml` `[tool.pytest.ini_options] addopts = "-q --junitxml=reports/junit.xml"` (TASK-001) |
| Teste (suíte) | `npx ng test --watch=false` | `web` | `reports/junit.xml` | `web/angular.json` `test.options.runnerConfig = vitest-base.config.ts` com reporter `junit` (TASK-012) |
| Teste (relacionado a arquivo) | `uv run pytest -q {files}` | `.` | `reports/junit.xml` | idem suíte |
| Teste (relacionado a arquivo) | `npx ng test --watch=false {include}` | `web` | `reports/junit.xml` | flag `include` do builder `@angular/build:unit-test` |
| Teste e2e (Playwright) | `npx playwright test {files}` | `web` | `reports/e2e-junit.xml` | `web/playwright.config.ts` (TASK-012) |
| Arquivo de regressão e2e | `web/e2e/regressao/{nome}.spec.ts` | — | — | convenção do pipeline |
| Build | `uv build` | `.` | — | `pyproject.toml` `[build-system] hatchling`, pacotes `api`, `ingest`, `evals` (TASK-001) |
| Build | `npx ng build` | `web` | — | `web/angular.json` (TASK-012) |
| Subir ambiente local | `docker compose up -d --build` | `.` | — | `docker-compose.yml` (TASK-004 cria só `db`; TASK-013 completa) |
| URL da aplicação | `http://localhost:8080` | — | — | serviço `web` (nginx) do `docker-compose.yml` (TASK-013) |
| Credenciais QA | `ATLAS_QA_USERNAME`, `ATLAS_QA_PASSWORD` | — | — | nomes de variáveis; valores = persona `alex` e `ATLAS_PERSONA_PASSWORD` |

Notas:
- **Pré-requisito dos testes `integration`**: `docker compose up -d db`, com Postgres em `localhost:5432`. O
  `tests/conftest.py` (TASK-004) cria um banco `atlas_test_<uuid>` por sessão de pytest e o destrói no fim (DA-12),
  seguindo o padrão de `~/Projects/interview-reviewer/backend/tests/conftest.py`.
- **Nenhum teste chama rede externa**: LLM, embeddings e Langfuse usam fakes ou `httpx.MockTransport`.
- O `run.py init` desta feature vai medir o baseline num repositório sem `pyproject.toml` nem `web/`: todos os alvos
  ficam "cegos" (G4) até a TASK-001 e a TASK-012 entrarem (R-1, C4).

## 4. Estratégia de Testes

Sem `.specs/codebase/TESTING.md`: estratégia derivada da convenção do autor (projetos irmãos citados na seção 3).

| Camada / pasta | Tipo exigido | Paralelo-seguro |
|---|---|---|
| `tests/unit` | unit | sim |
| `tests/integration` | integration | sim (banco próprio `atlas_test_<uuid>` por sessão de pytest; nenhuma porta fixa) |
| `web/src` | unit | sim |
| `web/e2e` | e2e | não (porta 4200 fixa do `webServer`) |

- `tests/unit/**`: funções puras, adapters com `httpx.MockTransport`, serviços com `FakeLLMClient`. Sem banco.
- `tests/integration/**`: tudo que toca Postgres (repositórios, tools, migrations, rotas via
  `httpx.AsyncClient(transport=ASGITransport(app))`, demos com `--provider fake`).
- `web/src/**/*.spec.ts`: Vitest via `@angular/build:unit-test` (jsdom), `HttpTestingController` para HTTP.
- `web/e2e/**`: Playwright com a API simulada por `page.route`. É criado pelo QA de tela em `web/e2e/regressao/`.
- Regra de co-location: o teste é escrito **na mesma task** que cria o código.
- Datasets JSONL de eval (`evals/datasets/*.jsonl`) são dados: entram em "Arquivos de teste" das tasks de
  fechamento, e cada uma tem um teste pytest que valida o arquivo.

## 5. Arquitetura Proposta

### 5.1 Visão de Componentes

```
ai-support/
  pyproject.toml, uv.lock, alembic.ini, Dockerfile, docker-compose.yml, render.yaml, .env.example
  api/                      FastAPI (pacote Python `api`)
    main.py, health.py, config.py, errors.py, messages.py, prompts.py
    observability/          logging JSON + trace_id (fase 0), tracing OTel/Langfuse (fase 14)
    db/                     engine async + migrations Alembic (0001..0011)
    llm/                    LLMClient, fake, anthropic, openai, pricing, tokens, instrumented, retry,
                            circuit_breaker, chaos, fallback, factory
    auth/                   customers, sessões, deps (persona/operador)
    chat/                   models, repository, language, context, prompt_builder, service, routes, stream_routes
    rag/                    models, fulltext, embeddings, vector_bruteforce, vector_hnsw, hybrid, retriever, citations
    tools/                  models, audit, registry, orders, refund_policy, refunds, subscriptions, email, catalog
    agent/                  models (confirmações), confirmations, events, guards, loop
    triage/                 schemas, service, routes
    streaming/              encoder SSE
    guardrails/             patterns, pii, input_guard, output_guard, stream_filter
    tickets/  feedback/  limits/  maintenance/  seed/
  ingest/                   parsers, chunking, pipeline, report, reindex, CLI
  evals/                    dataset, runner, judge, graders, targets, gate, report, CLI, datasets/*.jsonl
  demos/                    common, bench, demo_01..demo_14, data/
  prompts/                  system, summarize, judge_faithfulness, triage, topic_check (.md)
  corpus/                   manifest.yaml, en/, pt/, pdf/
  scripts/                  check_phase.py, build_corpus_pdfs.py
  docs/                     results.md, adr/00NN-*.md, observability.md, img/, review/
  web/                      Angular 22 standalone (core/i18n, core/auth, core/chat, features/login, features/chat, shared)
  .github/workflows/        ci.yml, evals.yml
  tests/                    unit/, integration/, conftest.py
```

Camadas do backend (dependência só para baixo): rotas → `ChatService` → `AgentLoop` → (`LLMClient` decorado,
`Retriever`, `ToolRegistry`, guardrails) → repositórios/ORM → Postgres. `api/config.py`,
`api/observability/logging.py` e `api/errors.py` são transversais.

### 5.2 Fluxo Principal

Caminho de uma mensagem com todas as fases prontas (`POST /chat/stream`):

```mermaid
sequenceDiagram
  autonumber
  participant UI as web (Angular)
  participant R as stream_routes
  participant L as limits/deps
  participant S as ChatService
  participant IG as InputGuard
  participant Repo as ConversationRepository
  participant Ret as Retriever
  participant A as AgentLoop
  participant LLM as LLMClient (Fallback → Breaker → Retry → Instrumented → [Chaos] → Adapter)
  participant T as ToolRegistry
  participant OG as StreamOutputFilter
  participant OT as tracing (Langfuse)
  UI->>R: POST /chat/stream {message, conversation_id} (cookie atlas_session)
  R->>R: require_customer (401 se inválida)
  R->>L: enforce_chat_limits (429 sem LLM)
  R->>S: handle_message
  S->>IG: check(text) → mascara PII / bloqueia injection
  alt bloqueado
    S-->>UI: done{status: blocked} (sem LLM)
  end
  S->>Repo: append_message(user, texto mascarado)
  S->>Ret: retrieve(query, idioma) (hybrid RRF)
  S->>A: run(turn: histórico resumido + trechos não confiáveis + tools)
  loop até resposta final ou limite (passos/custo/repetição)
    A->>LLM: stream(LLMRequest com prefixo estável em cache)
    LLM-->>A: text_delta / tool_call / usage
    alt tool_call destrutiva
      A-->>UI: tool_start + done{status: awaiting_confirmation, confirmation}
      UI->>R: POST /chat/confirmations/{id}/stream {decision}
      R->>S: resolve_confirmation → A.resume
    end
    A->>T: execute(call, ToolContext da sessão) → auditoria
    A-->>UI: tool_start / tool_end (rótulo do passo)
  end
  A->>OG: feed(deltas) → só frases aprovadas
  OG-->>UI: token... citation... done (ou blocked → mensagem padrão)
  S->>Repo: append_message(assistant, status)
  S-)OT: spans guardrail/retrieval/llm/tool/fallback (assíncrono, nunca bloqueia)
```

Narrativa: a identidade sai sempre do cookie, nunca do modelo. Limites e guardrail de entrada atuam antes de qualquer
chamada paga. O histórico persistido já vem mascarado. O loop suspende as tools destrutivas até a decisão do Cliente,
guardando o estado em `pending_confirmations`. A saída é liberada frase a frase depois do guardrail. A telemetria é
best-effort. Sem streaming, `POST /chat` percorre o mesmo caminho e só agrega os eventos (`ChatService.collect`).

### 5.3 Decisões Arquiteturais

| # | Decisão | Alternativas rejeitadas | Por quê |
|---|---|---|---|
| DA-1 | Monorepo na raiz atual (`ai-support/`), com as pastas do documento-fonte sem o diretório `atlas-support-agent/` | Criar `atlas-support-agent/` dentro do repo | O repo já é o projeto; um nível extra só alonga caminhos e quebra os filtros `prompts/**`/`api/**` do EVAL-03 |
| DA-2 | Um único projeto Python (raiz) com pacotes `api`, `ingest`, `evals`, `demos` e testes em `tests/` | Um `pyproject` por pasta | `ingest`, `evals` e `demos` importam `api` e um ambiente só simplifica CI, comandos e o `uv.lock` |
| DA-3 | SQLAlchemy 2 async + psycopg 3 + Alembic + `pgvector.sqlalchemy` | SQLModel; asyncpg puro | Padrão do autor (interview-reviewer); async casa com SSE |
| DA-4 | `Settings` completo (todas as variáveis da seção 7.3) criado na TASK-001 | Cada fase adicionar campos | Evita que 30 tasks disputem `api/config.py` e mantém `.env.example` estável |
| DA-5 | Interface própria `LLMClient` com adapters e decoradores (instrumented, retry, breaker, chaos, fallback) | SDK direto ou LiteLLM | Exigência do documento-fonte (primitivas à mão) e permite empilhar a resiliência sem tocar no chamador |
| DA-6 | Frontend Angular 22 standalone + signals em `web/`, testes Vitest pelo builder oficial | Karma; React | Stack do autor; versão do CLI instalado (22.1.4) |
| DA-7 | i18n próprio (serviço + dicionários TS) | `@angular/localize`; ngx-translate | A troca de idioma em tempo de execução é requisito (STR-08); evita dependência nova |
| DA-8 | `messages.status` com `complete/cancelled/blocked/error/degraded` desde a migration 0003 | Adicionar colunas por fase | Sem migrations de alteração em fases posteriores |
| DA-9 | `tiktoken` (contagem local) e `langdetect` (idioma) | API de contagem do provedor; LLM para idioma | Sem custo nem latência de rede por mensagem |
| DA-10 | Sessão opaca em cookie HttpOnly + SameSite=Strict, token guardado só como SHA-256; senha argon2 | JWT | Revogação simples (logout, restauração); a POST via fetch envia cookie same-origin |
| DA-11 | Log JSON com `logging` da stdlib + `contextvars` | structlog | Sem dependência nova; OTel entra na fase 14 |
| DA-12 | Banco de teste próprio por sessão de pytest | Banco compartilhado; testcontainers | Deixa `tests/integration` paralelo-seguro entre worktrees sem Docker por teste |
| DA-13 | `chunks.tsv` como coluna gerada `to_tsvector(ts_config, content)` com `ts_config` por idioma | Duas colunas tsvector | Um índice GIN, e a análise linguística segue o idioma do trecho (RAG-15) |
| DA-14 | SDKs `anthropic`/`openai` com `max_retries=0`; OpenAI como secundário | Retry do SDK | O retry é nosso (RES-02), e retry duplo distorceria a métrica do demo_12 |
| DA-15 | Preços por modelo em `config/pricing.json` editável; modelo sem preço = erro | Preços no código | Preços mudam e o teto diário (dinheiro real) não pode subestimar |
| DA-16 | Todo comportamento "antes" de cada fase fica selecionável por configuração: `ATLAS_HISTORY_MODE`, `ATLAS_RETRIEVAL_MODE`, `ATLAS_AGENT_TOOLS_ENABLED`, `ATLAS_AGENT_MAX_STEPS`, `ATLAS_TRIAGE_MODE`, `ATLAS_GUARDRAILS_ENABLED`, `ATLAS_INGEST_INJECTION_SCAN`, `ATLAS_PROMPT_LAYOUT`, `CHAOS_RATE` | Manter só o código da tag | DOD-02 continua verdadeiro mesmo se uma tag pegar código de outra fase, e os demos medem antes e depois na mesma execução |
| DA-17 | Runner de evals mínimo na fase 0, com o formato final do relatório (CT-10) | Evals só na fase 13 | Documento-fonte: embrião antes da fase 1 |
| DA-18 | Compose com `db`, `api` (migra no start) e `web` (nginx, proxy `/api`); Langfuse só no perfil `observability` | Langfuse sempre ligado | FND-01 pede um comando; a stack do Langfuse é pesada (LAC-36) |
| DA-19 | Embeddings OpenAI `text-embedding-3-small`, 1536 dimensões | Voyage; modelo local | Jev p=0.63; reaproveita a conta do secundário; imagem leve para o PaaS |
| DA-20 | Loop do agente como gerador assíncrono de `AgentEvent`, com suspensão serializada em `pending_confirmations.agent_state` | Manter o loop em memória esperando a confirmação | Sobrevive a reconexão e a múltiplas instâncias; a confirmação é outra requisição HTTP |
| DA-21 | Cancelamento por `UPDATE ... WHERE status IN (...) RETURNING`; reembolso com `SELECT ... FOR UPDATE` + `UNIQUE(order_id)` | Checar e depois atualizar | Garante TOOL-92/TOOL-93 sob concorrência (DEMO-91) |
| DA-22 | Guardrail de saída em streaming por frase: só frases aprovadas são enviadas; bloqueio troca a resposta inteira | Bufferizar a resposta inteira; checar só no fim | Nenhum dado alheio chega ao cliente sem perder todo o streaming ("a resposta sobe pelo guardrail de saída em stream", documento-fonte) |
| DA-23 | Idioma da resposta e data/hora fora do prefixo estável (mensagem após o ponto de cache) | Idioma no system | CACHE-02 exige prefixo idêntico entre conversas |
| DA-24 | Circuit breaker em memória por processo | Estado no banco/Redis | Uma instância na demo; sem infraestrutura nova |
| DA-25 | OTel SDK + exportador OTLP HTTP para o endpoint OTLP do Langfuse; score de feedback e exclusão por API pública | SDK proprietário do Langfuse | O documento-fonte pede OpenTelemetry; o backend fica trocável |
| DA-26 | Agendador de manutenção dentro da API (asyncio + `pg_try_advisory_lock` + `maintenance_runs`) | Cron do PaaS | Portável entre PaaS e local; uma execução por dia mesmo com várias instâncias |
| DA-27 | Deploy por blueprint `render.yaml` (API Docker, static site, Postgres gerenciado) | Fly.io; Railway | Jev p=0.85 |
| DA-28 | Barreira de fase: toda task da fase N depende do fechamento da fase N-1; a tag `phase-NN` é criada pelo Operador no merge da onda que contém o fechamento, após `scripts/check_phase.py NN` passar | Tags por task; sem barreira | A ordem de ondas do `/implement` vem do grafo, e sem barreira código da fase N+1 entraria antes da tag N |

## 6. Reuso Obrigatório

O repositório não tem código. O reuso é (a) de padrões do próprio autor em projetos irmãos, a copiar, e (b) de
componentes criados nesta feature, que as tasks seguintes **devem** usar em vez de recriar.

**(a) Padrões externos a copiar**

| Precisa de | Já existe em | Como usar |
|---|---|---|
| Configuração de ruff/mypy/pytest com JUnit | `~/Projects/support-ticket-ai/pyproject.toml` (`[tool.ruff]`, `[tool.mypy]`, `addopts --junitxml`) | Copiar as seções na TASK-001, trocando `packages`/`files` |
| Banco de teste por sessão | `~/Projects/interview-reviewer/backend/tests/conftest.py` (fixtures `admin_database_url`, `test_database_url`, prefixo `ir_test_`) | Mesmo desenho com prefixo `atlas_test_` e `alembic upgrade head` (TASK-004); as fixtures de banco de todas as tasks de integração dependem de `db_session` desse conftest, nunca de engine próprio |
| Vitest com JUnit no Angular | `~/Projects/interview-reviewer/frontend/vitest-base.config.ts` + `angular.json` `test.options.runnerConfig` | Igual na TASK-012 |
| Playwright com JUnit separado | `~/Projects/interview-reviewer/frontend/playwright.config.ts` | Igual na TASK-012 (`reports/e2e-junit.xml`, `workers: 1`) |
| Compose com Postgres e healthcheck | `~/Projects/interview-reviewer/docker-compose.yml` (serviço `db`) | Mesmo healthcheck `pg_isready`, imagem `pgvector/pgvector:pg16` |

**(b) Componentes internos (reuso obrigatório a partir da task produtora)**

| Precisa de | Já existe em (task) | Como usar |
|---|---|---|
| Configuração | `api/config.py` (TASK-001) | `get_settings()`; nunca `os.environ` |
| Log e trace_id | `api/observability/logging.py` (TASK-002) | `log_event(...)`, `get_trace_id()` |
| Erros e mensagens fixas | `api/errors.py`, `api/messages.py` (TASK-003) | `AtlasError(ErrorCode.X, status)`, `user_message(key, lang)` |
| Sessão de banco | `api/db/engine.py` (TASK-004) | `session_scope()`, `get_db_session()` |
| Chamada a LLM | `api/llm/client.py`, `api/llm/factory.py` | Sempre via `LLMClient` vindo de `build_llm_client`; testes com `FakeLLMClient` |
| Custo | `api/llm/pricing.py` (TASK-007) | `estimate_cost` (nunca recalcular) |
| Tokens | `api/llm/tokens.py` (TASK-008) | `count_tokens`/`count_messages` (chunker, contexto, log) |
| Prompts | `api/prompts.py` (TASK-018) | `load_prompt(name)`; textos em `prompts/*.md` |
| Usuário atual | `api/auth/deps.py` | `Depends(require_customer)` / `Depends(require_customer_or_operator)` |
| Conversas | `api/chat/repository.py` | Sempre via `ConversationRepository` (posse checada lá) |
| Recuperação | `api/rag/retriever.py` | `Retriever.retrieve`; novos modos via `register_mode` |
| Tools | `api/tools/registry.py`, `api/tools/catalog.py` | `build_default_registry()`; `ToolContext` montado só a partir da sessão |
| Auditoria | `api/tools/audit.py` | `record_audit` e `list_successful_actions` (AGT-08 e GRD-08) |
| PII | `api/guardrails/pii.py` | `mask_pii` também em atributos de span (OBS-91) |
| Resultados de demo | `demos/common.py` | `record_result(...)`, `--provider fake` |
| Checagem de fase | `scripts/check_phase.py` | Rodar antes de marcar a fase pronta |

Padrões internos a seguir: rotas novas registram-se em `api/main.py` só com `include_router`; migrations novas
importam o módulo de modelos em `api/db/migrations/env.py`; textos fixos ao Cliente só em `api/messages.py` (backend)
e `web/src/app/core/i18n/messages.ts` (frontend), nos dois idiomas.

## 7. Modelos de Dados

### 7.1 Tabelas (PostgreSQL 16 + pgvector)

Marcação: **[PII]** = pode conter dado pessoal real digitado por visitante (sempre mascarado antes de gravar, GRD-07).
**[fictício]** = dado inventado pelo seed.

| Tabela | Campos (tipo, obrigatoriedade) | Relacionamentos / índices | Migration |
|---|---|---|---|
| `customers` | `id uuid PK`, `username text UNIQUE NOT NULL`, `display_name text NOT NULL`, `email text NOT NULL` [fictício], `password_hash text NOT NULL`, `language text CHECK (pt,en) NOT NULL` | — | 0002 |
| `customer_sessions` | `id uuid PK`, `customer_id uuid FK NOT NULL`, `token_hash char(64) UNIQUE NOT NULL`, `created_at timestamptz`, `expires_at timestamptz NOT NULL` | idx `customer_id`; CASCADE | 0002 |
| `conversations` | `id uuid PK`, `customer_id uuid FK NOT NULL`, `created_at`, `summary text NULL` [PII mascarada], `summary_upto_message_id bigint NULL` | idx `(customer_id, created_at)`; CASCADE de customer | 0003 |
| `messages` | `id bigserial PK`, `conversation_id uuid FK NOT NULL`, `role text CHECK (user,assistant,tool)`, `content text NOT NULL` [PII mascarada], `status text CHECK (complete,cancelled,blocked,error,degraded)`, `language text NULL`, `trace_id text NOT NULL`, `created_at` | idx `(conversation_id, id)`; CASCADE | 0003 |
| `documents` | `id`, `doc_key text`, `title`, `language`, `doc_type CHECK (manual,policy,faq)`, `format CHECK (md,pdf)`, `source_path`, `content_hash`, `status CHECK (indexed,quarantined,failed)`, `error text NULL`, `ingested_at` | `UNIQUE(doc_key, language)` | 0004 |
| `chunks` | `id bigserial`, `document_id FK`, `ordinal int`, `content text`, `language`, `section text NULL`, `page int NULL`, `token_count int`, `ts_config regconfig`, `tsv tsvector GENERATED`, `embedding vector(1536) NULL` (0005) | GIN(`tsv`); HNSW(`embedding vector_cosine_ops`) (0006); idx `language`; CASCADE | 0004/0005/0006 |
| `bench_vectors` | `id bigserial`, `embedding vector(1536)` | HNSW (criado pelo demo_05) | 0006 |
| `orders` | `id int PK` (número exibido), `customer_id FK`, `status CHECK (pending,paid,shipped,delivered,cancelled)`, `total_cents int CHECK >0`, `currency char(3) CHECK ='USD'`, `items jsonb`, `created_at`, `shipped_at NULL`, `delivered_at NULL`, `cancelled_at NULL` [fictício] | idx `customer_id` | 0007 |
| `subscriptions` | `id`, `customer_id UNIQUE`, `plan_code`, `status`, `price_cents`, `currency CHECK ='USD'`, `renews_at` [fictício] | — | 0007 |
| `refunds` | `id`, `order_id UNIQUE FK`, `customer_id FK`, `amount_cents`, `currency CHECK ='USD'`, `created_at`, `trace_id` | `UNIQUE(order_id)` (TOOL-93) | 0007 |
| `audit_log` | `id bigserial`, `customer_id`, `tool`, `arguments jsonb`, `result jsonb`, `outcome CHECK (success,error,refused)`, `trace_id`, `created_at` | idx `trace_id`, `created_at` | 0007 |
| `email_outbox` | `id`, `customer_id`, `recipient` [fictício], `subject`, `body` [PII mascarada possível], `trace_id`, `created_at` | idx `created_at` | 0007 |
| `pending_confirmations` | `id uuid`, `conversation_id FK`, `customer_id FK`, `tool`, `arguments jsonb`, `agent_state jsonb` [PII mascarada], `status CHECK (pending,confirmed,refused,expired)`, `trace_id`, `created_at`, `resolved_at NULL` | idx `(conversation_id, status)`; CASCADE de conversa | 0008 |
| `tickets` | `id`, `conversation_id FK NULL`, `customer_id FK`, `reason`, `trace_id`, `created_at` | idx `created_at` | 0009 |
| `feedback` | `id`, `message_id UNIQUE FK`, `customer_id FK`, `rating CHECK (up,down)`, `trace_id`, `created_at` | CASCADE de message | 0010 |
| `llm_calls` | `id bigserial`, `trace_id`, `provider`, `model`, `cost_usd numeric(12,6)`, `created_at` | idx `created_at` | 0011 |
| `rate_limit_events` | `id bigserial`, `customer_id FK`, `created_at` | idx `(customer_id, created_at)` | 0011 |
| `maintenance_runs` | `id`, `job`, `run_date date`, `finished_at` | `UNIQUE(job, run_date)` | 0011 |

### 7.2 Migrations

`api/db/migrations/versions/0001_enable_pgvector.py` … `0011_usage_and_maintenance.py` (lista completa na seção 9).
Todas criam estrutura nova (nenhuma tem backfill) e têm `downgrade` simétrico. A 0005 adiciona uma coluna anulável a
`chunks`, que não tem dado em produção no momento (a demo pública só sobe na fase final).

### 7.3 Variáveis de ambiente (`Settings`, CT-1) — `.env.example`

| Variável | Padrão | Uso |
|---|---|---|
| `ATLAS_APP_ENV` | `local` | `local`/`ci`/`demo` |
| `ATLAS_DATABASE_URL` | `postgresql+psycopg://atlas:atlas@localhost:5432/atlas` | banco |
| `ATLAS_LLM_PROVIDER` | `anthropic` | `anthropic`/`fake` |
| `ANTHROPIC_API_KEY` | — (segredo) | primário |
| `OPENAI_API_KEY` | — (segredo) | secundário e embeddings |
| `ATLAS_PRIMARY_MODEL` / `ATLAS_SECONDARY_MODEL` / `ATLAS_GUARDRAIL_MODEL` / `ATLAS_JUDGE_MODEL` | — (obrigatório o primário) | modelos (P-2) |
| `ATLAS_EMBEDDING_PROVIDER` / `ATLAS_EMBEDDING_MODEL` / `ATLAS_EMBEDDING_DIMENSIONS` | `openai` / `text-embedding-3-small` / `1536` | DA-19 |
| `ATLAS_PRICING_FILE` | `config/pricing.json` | DA-15 |
| `ATLAS_LLM_TIMEOUT_SECONDS` / `ATLAS_LLM_MAX_RETRIES` / `ATLAS_RETRY_BASE_DELAY_SECONDS` | `30` / `3` / `0.5` | RES-01/02 |
| `ATLAS_BREAKER_FAILURE_THRESHOLD` / `ATLAS_BREAKER_RESET_SECONDS` | `5` / `30` | RES-04 |
| `CHAOS_RATE` | `0` | RES-07, DEMO-05 |
| `ATLAS_CONTEXT_BUDGET_TOKENS` | `8000` | CHAT-08 |
| `ATLAS_HISTORY_MODE` | `summarized` | DA-16 |
| `ATLAS_MAX_MESSAGE_CHARS` | `4000` | CHAT-09 |
| `ATLAS_RETRIEVAL_MODE` / `ATLAS_RETRIEVAL_TOP_K` | `hybrid` / `5` | DA-16, P-6 |
| `ATLAS_HNSW_M` / `ATLAS_HNSW_EF_CONSTRUCTION` / `ATLAS_HNSW_EF_SEARCH` / `ATLAS_RRF_K` | `16` / `64` / `40` / `60` | RAG-10, RAG-12 |
| `ATLAS_AGENT_TOOLS_ENABLED` / `ATLAS_AGENT_MAX_STEPS` / `ATLAS_AGENT_MAX_COST_USD` | `true` / `8` / `0.25` | AGT-02/03, P-3 |
| `ATLAS_CONFIRMATION_TTL_MINUTES` | `15` | P-4 |
| `ATLAS_TRIAGE_MODE` / `ATLAS_TRIAGE_MAX_REPAIRS` | `schema` / `2` | TRI-02/03 |
| `ATLAS_GUARDRAILS_ENABLED` / `ATLAS_INGEST_INJECTION_SCAN` | `true` / `true` | GRD |
| `ATLAS_PROMPT_LAYOUT` | `cache_ordered` | CACHE-01 |
| `ATLAS_SESSION_TTL_HOURS` / `ATLAS_COOKIE_SECURE` | `24` / `false` | P-5 |
| `ATLAS_PERSONA_PASSWORD` | `atlas-demo` (pública, README) | P-9 |
| `ATLAS_OPERATOR_API_KEY` | — (segredo; vazio = desligado) | LAC-35 |
| `ATLAS_RATE_LIMIT_PER_HOUR` / `ATLAS_DAILY_SPEND_CAP_USD` | `20` / `2.00` | DEMO-08 |
| `ATLAS_RETENTION_DAYS` / `ATLAS_MAINTENANCE_ENABLED` / `ATLAS_MAINTENANCE_INTERVAL_SECONDS` | `7` / `false` / `300` | DEMO-07/10 |
| `LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | — (chaves = segredo) | OBS, LAC-36 |
| `ATLAS_LOG_LEVEL` | `INFO` | log |
| `ATLAS_QA_USERNAME` / `ATLAS_QA_PASSWORD` | — | QA de tela |

## 8. Contratos

### 8.1 Contratos externos (API)

Base: API em `:8000` (local) e atrás de `/api` no nginx/Render (o frontend chama `/api/...`). Erros sempre no envelope
`{"error": {"code", "message", "trace_id"}}` (CT-3). Todo response traz o cabeçalho `X-Trace-Id`.

**`GET /health`** — público. 200 `{"status":"ok","db":"ok"}` | 503 `{"status":"degraded","db":"unavailable"}`.

**`POST /auth/login`** — público. Req `{"username": str, "password": str}`. 200 `{"customer": {"display_name", "language"}}`
+ `Set-Cookie: atlas_session=...; HttpOnly; SameSite=Strict; Path=/`. 401 `INVALID_CREDENTIALS`. 422 `VALIDATION_ERROR`.

**`POST /auth/logout`** — sessão. 204. **`GET /auth/me`** — 200 `{"display_name","language"}` | 401 `AUTH_REQUIRED`.

**`POST /chat`** — sessão de persona. Req/resp: CT-14. 200 em todos os desfechos de negócio (`completed`,
`awaiting_confirmation`, `blocked`, `degraded`, `limit_reached`). 401 `AUTH_REQUIRED`; 404 `CONVERSATION_NOT_FOUND`;
422 `VALIDATION_ERROR` / `MESSAGE_TOO_LONG`; 429 `RATE_LIMITED` / `DAILY_CAP_REACHED` (corpo com `retry_at`);
502 `LLM_UNAVAILABLE` (antes da fase 12).

**`POST /chat/confirmations/{confirmation_id}`** — sessão. Req `{"decision": "confirm"|"refuse"}`. 200 `ChatResponse`.
404 `CONFIRMATION_NOT_FOUND` (inexistente ou de outro cliente); 409 `CONFIRMATION_NOT_PENDING` (já resolvida ou
expirada); 429 `DAILY_CAP_REACHED`.

**`POST /chat/stream`** e **`POST /chat/confirmations/{id}/stream`** — sessão. Mesmo corpo. 200 `text/event-stream`:
```
event: tool_start   data: {"call_id","tool","label_key"}
event: tool_end     data: {"call_id","tool","ok"}
event: token        data: {"text"}
event: citation     data: {"index","document","section","page"}
event: done         data: {"status","message_id","conversation_id","trace_id","confirmation"?}
event: error        data: {"code","message","trace_id"}
```
`done` exatamente uma vez e por último. Depois de `error`, nada é emitido. Os erros de pré-condição (401/404/422/429)
saem como HTTP antes de abrir o stream.

**`POST /triage`** — sessão de persona OU `X-Operator-Key`. Req `{"text": str}` (`extra=forbid`). 200 `TriageResult`
(CT-48). 401 `AUTH_REQUIRED`; 422 `VALIDATION_ERROR`; 429; 503 `TRIAGE_UNAVAILABLE` (sem objeto de triagem).

**`POST /messages/{message_id}/feedback`** — sessão. Req `{"rating": "up"|"down"}`. 204. 404 `MESSAGE_NOT_FOUND`.

**CLIs**: `python -m ingest run|reindex-hnsw` (CT-28), `python -m evals run|gate` (CT-10, seção 8.3), `python -m api.seed`
(CT-23), `python scripts/check_phase.py NN` (CT-11), `python -m demos.demo_NN_* --provider real|fake`.

### 8.2 Contratos internos entre tasks

| ID | Contrato (assinatura / rota / tipo) | Produzido por | Consumido por |
|---|---|---|---|
| CT-1 | `api.config.Settings` (pydantic-settings, campos da seção 7.3) + `get_settings() -> Settings` (cacheado) + `MissingConfigError(var_name: str)` | TASK-001 | TASK-002, TASK-004, TASK-006, TASK-007, TASK-009, TASK-010, TASK-011, TASK-017, TASK-019, TASK-025, TASK-047, TASK-053, TASK-114, TASK-117, TASK-132, TASK-134, TASK-143, TASK-144 |
| CT-2 | `api.observability.logging`: `configure_logging(settings) -> None`, `get_trace_id() -> str`, `bind_trace_id(trace_id: str) -> contextvars.Token`, `log_event(event: str, **fields: object) -> None`; `api.observability.middleware.TraceIdMiddleware` (gera UUID4 hex, devolve no cabeçalho `X-Trace-Id`) | TASK-002 | TASK-003, TASK-009, TASK-010, TASK-132 |
| CT-3 | `api.errors`: `ErrorCode` (StrEnum, catálogo 8.3), `AtlasError(code: ErrorCode, http_status: int, params: dict[str, object] \| None = None)`, `install_error_handlers(app) -> None`; corpo de erro `{"error": {"code": str, "message": str, "trace_id": str}}` | TASK-003 | TASK-010, TASK-019, TASK-020, TASK-025, TASK-028, TASK-075, TASK-088, TASK-145 |
| CT-4 | `api.messages.user_message(key: MessageKey, lang: Lang, **params: object) -> str`; `Lang = Literal["pt", "en"]`; `MessageKey` (StrEnum com as chaves da seção 9 do spec) | TASK-003 | TASK-019, TASK-031, TASK-049, TASK-084, TASK-145 |
| CT-5 | `api.db.engine`: `Base` (DeclarativeBase), `get_engine() -> AsyncEngine`, `session_scope() -> AsyncIterator[AsyncSession]` (async context manager com commit/rollback), `get_db_session()` (dependência FastAPI) | TASK-004 | TASK-010, TASK-023, TASK-024, TASK-043, TASK-045, TASK-065, TASK-074, TASK-119, TASK-120, TASK-135, TASK-141, TASK-142, TASK-147 |
| CT-6 | `api.llm.client`: `class LLMClient(Protocol)` com `async def complete(self, request: LLMRequest) -> LLMResponse` e `def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]`; dataclasses `SystemBlock(text, cache_breakpoint: bool = False)`, `ChatMessage(role: Literal["user","assistant","tool"], content: str, tool_calls: list[ToolCall] = [], tool_call_id: str \| None = None)`, `ToolSpec(name, description, input_schema: dict)`, `ToolCall(id, name, arguments: dict)`, `LLMRequest(system: list[SystemBlock], messages: list[ChatMessage], tools: list[ToolSpec] = [], response_schema: dict \| None = None, max_tokens: int = 1024, purpose: str = "chat")`, `Usage(input_tokens, output_tokens, cache_read_tokens=0, cache_write_tokens=0)`, `LLMResponse(text, tool_calls, usage, model, provider, stop_reason)`, `LLMStreamEvent(kind: Literal["text_delta","tool_call","usage","stop"], text="", tool_call=None, usage=None, stop_reason=None)`; exceções `LLMError(message, recoverable: bool, status: int \| None)`, `LLMTimeoutError(LLMError)`; `api.llm.fake.FakeLLMClient(script: list[LLMResponse])` | TASK-005 | TASK-006, TASK-007, TASK-008, TASK-009, TASK-019, TASK-030, TASK-048, TASK-050, TASK-068, TASK-076, TASK-083, TASK-088, TASK-092, TASK-094, TASK-103, TASK-111, TASK-114, TASK-115, TASK-116, TASK-117, TASK-118 |
| CT-7 | `api.llm.pricing.estimate_cost(model: str, usage: Usage) -> Decimal` (USD; preços por milhão de tokens em `config/pricing.json`: `input`, `output`, `cache_read`, `cache_write`); `UnknownModelPricing` | TASK-007 | TASK-009, TASK-083, TASK-111, TASK-142 |
| CT-8 | `api.llm.tokens.count_tokens(text: str) -> int` e `count_messages(system: list[SystemBlock], messages: list[ChatMessage]) -> int` (tiktoken `o200k_base`) | TASK-008 | TASK-009, TASK-030, TASK-044 |
| CT-9 | `api.llm.instrumented.InstrumentedLLMClient(inner: LLMClient, model: str, provider: str)` (implementa CT-6 e emite `log_event("llm_call", trace_id, provider, model, purpose, context_tokens, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, latency_ms, outcome)`); `api.llm.factory.build_llm_client(settings: Settings) -> LLMClient` | TASK-009 | TASK-017, TASK-019, TASK-088, TASK-121 |
| CT-10 | Caso de eval JSONL `{"id": str, "phase": int, "category": "rag"\|"tool_trajectory"\|"json_valid"\|"red_team"\|"tone"\|"smoke", "language": "pt"\|"en", "input": dict, "expected": dict, "grader": str}`; `evals.dataset.load_cases(paths: list[Path]) -> list[EvalCase]`; `evals.runner.run_cases(cases, target: EvalTarget, graders: Mapping[str, Grader]) -> EvalReport`; relatório JSON `{"run_id", "status": "ok"\|"error", "cases": [{"id","category","language","score","passed","details"}], "scores": {"overall": float, "by_category": {str: float}}}`; CLI `python -m evals run --dataset <glob> --target <nome> --out <arquivo>` | TASK-011 | TASK-016, TASK-022, TASK-039, TASK-050, TASK-052, TASK-059, TASK-064, TASK-082, TASK-086, TASK-091, TASK-099, TASK-108, TASK-109, TASK-113, TASK-123, TASK-124, TASK-125, TASK-126, TASK-127, TASK-128, TASK-130, TASK-131, TASK-139, TASK-140 |
| CT-11 | CLI `python scripts/check_phase.py <NN> [--require-tag]`: exit 0 se a fase está pronta (DOD-01), exit 1 listando o que falta | TASK-016 | TASK-022 |
| CT-12 | `demos.common`: `parse_demo_args(description: str) -> argparse.Namespace` (flags `--provider {real,fake}`, `--results PATH` padrão `docs/results.md`, `--out-dir PATH` padrão `docs/img`), `build_demo_llm(provider: str) -> LLMClient`, `record_result(phase: int, metric: str, before: str, after: str, notes: str = "", results_path: Path = ...) -> None` (atualiza a linha da fase na tabela de `docs/results.md`, idempotente) | TASK-017 | TASK-021, TASK-038, TASK-051, TASK-057, TASK-058, TASK-063, TASK-081, TASK-085, TASK-090, TASK-098, TASK-108, TASK-112, TASK-122, TASK-130, TASK-139 |
| CT-13 | `api.prompts.load_prompt(name: str) -> str` (lê `prompts/<name>.md`, cacheado; `PromptNotFound`) | TASK-018 | TASK-019, TASK-030, TASK-048, TASK-050, TASK-087, TASK-103 |
| CT-14 | HTTP `POST /chat` — request `ChatRequest{message: str, conversation_id: UUID \| None, ui_language: "pt"\|"en" \| None}`; response 200 `ChatResponse{conversation_id: UUID \| None, message_id: int \| None, status: "completed"\|"awaiting_confirmation"\|"blocked"\|"degraded"\|"limit_reached", content: str, language: "pt"\|"en", citations: list[Citation{index, document, section, page}], confirmation: ConfirmationPrompt{id: UUID, tool: str, target: str, amount_cents: int \| None, currency: "USD" \| None} \| None, trace_id: str}` (schemas em `api/chat/schemas.py`) | TASK-019 | TASK-020, TASK-032, TASK-036 |
| CT-15 | `api.chat.service.ChatService.handle_message(request: ChatRequest, principal: CurrentCustomer \| None) -> AsyncIterator[AgentEvent]` e `ChatService.collect(events) -> ChatResponse`; fábrica `get_chat_service()` (dependência FastAPI) | TASK-019 | TASK-020, TASK-021, TASK-031, TASK-038, TASK-049, TASK-077, TASK-078, TASK-081, TASK-085, TASK-094, TASK-095, TASK-107, TASK-108, TASK-112, TASK-121, TASK-122, TASK-126, TASK-139 |
| CT-16 | `api.auth.deps`: `CurrentCustomer(id: UUID, email: str, display_name: str, language: Lang)`, `require_customer() -> CurrentCustomer` (cookie `atlas_session`; 401 `AUTH_REQUIRED`), `Principal = CurrentCustomer \| OperatorPrincipal`, `require_customer_or_operator() -> Principal` (cabeçalho `X-Operator-Key` comparado com `secrets.compare_digest`); `api.auth.service`: `authenticate(session, username, password) -> CurrentCustomer`, `create_session(session, customer_id) -> str`, `resolve_session(session, token) -> CurrentCustomer \| None`, `revoke_session(session, token) -> None`, `hash_password(raw) -> str` | TASK-025 | TASK-026, TASK-027, TASK-031, TASK-032, TASK-078, TASK-089, TASK-095, TASK-136 |
| CT-17 | HTTP `POST /auth/login {username, password}` → 200 `{customer: {display_name, language}}` + `Set-Cookie: atlas_session=<token>; HttpOnly; SameSite=Strict; Path=/; Secure (se ATLAS_COOKIE_SECURE)`; 401 `INVALID_CREDENTIALS`; `POST /auth/logout` → 204; `GET /auth/me` → 200 `{display_name, language}` ou 401 `AUTH_REQUIRED` | TASK-026 | TASK-034 |
| CT-18 | ORM `api.auth.models.Customer(id UUID, username, display_name, email, password_hash, language)` e `CustomerSession(id, customer_id, token_hash, created_at, expires_at)` | TASK-023 | TASK-024, TASK-025, TASK-027, TASK-065 |
| CT-19 | ORM `api.chat.models.Conversation(id UUID, customer_id, created_at, summary, summary_upto_message_id)` e `Message(id bigint, conversation_id, role, content, status: complete\|cancelled\|blocked\|error\|degraded, language, trace_id, created_at)` | TASK-024 | TASK-028, TASK-030, TASK-135 |
| CT-20 | `api.chat.repository.ConversationRepository(session)`: `get_owned(conversation_id, customer_id) -> Conversation` (levanta `AtlasError(CONVERSATION_NOT_FOUND, 404)` igual para inexistente e alheia), `create(customer_id) -> Conversation`, `append_message(conversation_id, role, content, status, language, trace_id) -> Message`, `list_messages(conversation_id) -> list[Message]`, `save_summary(conversation_id, summary, upto_message_id) -> None`, `delete_for_customers(customer_ids) -> int` | TASK-028 | TASK-031, TASK-077, TASK-094, TASK-146 |
| CT-21 | `api.chat.language.detect_language(text: str, fallback: Lang) -> Literal["pt", "en", "other"]` | TASK-029 | TASK-031 |
| CT-22 | `api.chat.context.build_history(messages: list[Message], summary: str \| None, budget_tokens: int, summarizer: Summarizer) -> HistoryWindow(summary: str \| None, messages: list[ChatMessage], tokens: int, summarized_upto: int \| None)`; `HISTORY_MODE=full` desliga o resumo | TASK-030 | TASK-031, TASK-038, TASK-048 |
| CT-23 | `api.seed.personas.PERSONAS` (tupla de `PersonaSpec(username, display_name, email, language)`, ≥ 2 itens: `alex` (en) e `bia` (pt)) e `seed_personas(session, password: str) -> list[UUID]`; CLI `python -m api.seed [--commerce] [--reset]` | TASK-027 | TASK-066, TASK-146 |
| CT-24 | Web `I18nService` (`lang: Signal<'pt'\|'en'>`, `setLang(l)`, `t(key: MessageKey, params?) : string`, idioma inicial pelo `navigator.language`, persistido em `localStorage['atlas.lang']`) | TASK-033 | TASK-035, TASK-037, TASK-079, TASK-097, TASK-137 |
| CT-25 | Web `AuthService` (`login(username, password): Observable<Me>`, `logout()`, `me(): Observable<Me \| null>`, `isAuthenticated: Signal<boolean>`) e `authGuard: CanActivateFn` (redireciona para `/login`) | TASK-034 | TASK-035 |
| CT-26 | Web `ChatApiService` (`send(req: ChatRequest): Observable<ChatResponse>`, `confirm(id: string, decision: 'confirm'\|'refuse'): Observable<ChatResponse>`) e tipos TS espelhando CT-14/CT-46 em `chat.models.ts` | TASK-036 | TASK-037, TASK-079, TASK-080 |
| CT-27 | ORM `api.rag.models.Document(id, doc_key, title, language, doc_type: manual\|policy\|faq, format: md\|pdf, source_path, content_hash, status: indexed\|quarantined\|failed, error, ingested_at)` e `Chunk(id, document_id, ordinal, content, language, section, page, token_count, ts_config: regconfig, tsv (gerada), embedding vector(ATLAS_EMBEDDING_DIMENSIONS) NULL)` | TASK-043 | TASK-045, TASK-046, TASK-054 |
| CT-28 | CLI `python -m ingest run [--corpus corpus] [--report reports/ingest-report.json] [--skip-embeddings]` e `python -m ingest reindex-hnsw`; `IngestReport{documents: [{doc_key, language, doc_type, format, status, chunks, error}], totals: {indexed, quarantined, failed}}` | TASK-045 | TASK-051, TASK-055, TASK-060, TASK-106 |
| CT-29 | `api.rag.fulltext.search_fulltext(session, query: str, language: Lang, k: int) -> list[ScoredChunk(chunk_id: int, score: float)]` (config `english`/`portuguese` pelo idioma) | TASK-046 | TASK-047, TASK-051, TASK-058, TASK-062 |
| CT-30 | `api.rag.retriever.Retriever(session_factory, embedder, settings)`: `async retrieve(query: str, language: Lang, k: int \| None = None, mode: RetrievalMode \| None = None) -> list[RetrievedChunk(chunk_id, document_title, doc_key, section, page, language, content, score)]`; `RetrievalUnavailable` quando busca/embedding falha; `RetrievalMode = Literal["none","fulltext","vector_bruteforce","hnsw","hybrid"]`; `register_mode(mode, searcher)` | TASK-047 | TASK-048, TASK-049, TASK-056, TASK-061, TASK-062 |
| CT-31 | `api.chat.prompt_builder.build_request(*, language: Lang, history: HistoryWindow, chunks: list[RetrievedChunk], user_message: str, tools: list[ToolSpec], layout: Literal["naive","cache_ordered"]) -> LLMRequest` | TASK-048 | TASK-049, TASK-076, TASK-105, TASK-110 |
| CT-32 | `api.rag.citations.extract_citations(answer: str, chunks: list[RetrievedChunk]) -> list[Citation]` (marcadores `[n]`, 1-based, só índices existentes) | TASK-048 | TASK-049 |
| CT-33 | `evals.judge.judge_faithfulness(llm: LLMClient, question: str, answer: str, chunks: list[str]) -> JudgeVerdict(faithful: bool, score: float, rationale: str)` | TASK-050 | TASK-051, TASK-125 |
| CT-34 | `api.rag.embeddings.EmbeddingClient(Protocol).embed(texts: list[str]) -> list[list[float]]`; `OpenAIEmbeddingClient(settings)`, `FakeEmbeddingClient(dim)` (determinístico por hash); `EmbeddingError`; `build_embedder(settings) -> EmbeddingClient` | TASK-053 | TASK-055, TASK-056 |
| CT-35 | Buscadores vetoriais com a assinatura `async search(session, query_vector: list[float], language: Lang, k: int) -> list[ScoredChunk]`: `api.rag.vector_bruteforce.search_bruteforce` e `api.rag.vector_hnsw.search_hnsw` | TASK-056 | TASK-058, TASK-061, TASK-062, TASK-063 |
| CT-36 | `api.rag.hybrid.rrf_fuse(rankings: list[list[ScoredChunk]], k: int, rrf_k: int = 60) -> list[ScoredChunk]` | TASK-062 | TASK-063 |
| CT-37 | `demos.bench`: `generate_vectors(n: int, dim: int, seed: int) -> np.ndarray`, `load_bench_table(session, vectors) -> None`, `measure(search_fn, queries, k) -> BenchResult(p50_ms, p95_ms, recall_at_5)` | TASK-057 | TASK-058, TASK-063 |
| CT-38 | ORM `api.tools.models`: `OrderStatus` (pending, paid, shipped, delivered, cancelled), `Order(id int, customer_id, status, total_cents, currency='USD', items JSONB, created_at, shipped_at, delivered_at, cancelled_at)`, `Subscription(id, customer_id UNIQUE, plan_code, status, price_cents, currency, renews_at)`, `Refund(id, order_id UNIQUE, customer_id, amount_cents, currency, created_at, trace_id)`, `AuditLog(id, customer_id, tool, arguments JSONB, result JSONB, outcome: success\|error\|refused, trace_id, created_at)`, `EmailOutbox(id, customer_id, recipient, subject, body, trace_id, created_at)` | TASK-065 | TASK-066, TASK-067, TASK-069, TASK-070, TASK-071, TASK-072 |
| CT-39 | `api.tools.audit`: `record_audit(session, *, customer_id, tool, arguments, result, outcome, trace_id) -> AuditLog` e `list_successful_actions(session, trace_id) -> list[AuditLog]` | TASK-067 | TASK-069, TASK-071, TASK-072, TASK-076, TASK-084, TASK-107 |
| CT-40 | `api.tools.registry`: `ToolDefinition(name, description, args_model: type[BaseModel], destructive: bool, handler)`, `ToolContext(customer_id: UUID, customer_email: str, trace_id: str, session: AsyncSession, language: Lang, now: datetime)`, `ToolResult(ok: bool, content: dict, error_code: str \| None)`, `ToolRegistry.register(defn)`, `.specs() -> list[ToolSpec]`, `.is_destructive(name) -> bool`, `async .execute(call: ToolCall, ctx: ToolContext) -> ToolResult` (valida args; tool inexistente → `UNKNOWN_TOOL`; args inválidos → `INVALID_ARGUMENTS`) | TASK-068 | TASK-069, TASK-071, TASK-072, TASK-073, TASK-076 |
| CT-41 | `api.tools.refund_policy.evaluate_refund(order: Order, now: datetime) -> RefundEligibility(eligible: bool, reason_code: Literal["cancelled_before_shipment","delivered_within_7_days","delivered_over_7_days","not_delivered_yet","already_refunded"], amount_cents: int)` | TASK-070 | TASK-071 |
| CT-42 | Handlers de tool `async (args, ctx: ToolContext) -> ToolResult`: `list_orders(ListOrdersArgs{limit: int = 10})`, `get_order(GetOrderArgs{order_id: int})`, `cancel_order(CancelOrderArgs{order_id: int})`, `check_refund_policy(CheckRefundArgs{order_id: int})`, `issue_refund(IssueRefundArgs{order_id: int})`, `get_subscription(EmptyArgs)`, `send_email(SendEmailArgs{subject: str, body: str})` — nenhum args model tem campo de cliente nem de destinatário | TASK-069 | TASK-071, TASK-073 |
| CT-43 | `api.tools.catalog.build_default_registry() -> ToolRegistry` (exatamente as 7 tools; `destructive` só em `cancel_order` e `issue_refund`) | TASK-073 | TASK-077 |
| CT-44 | ORM `api.agent.models.PendingConfirmation(id UUID, conversation_id, customer_id, tool, arguments JSONB, agent_state JSONB, status: pending\|confirmed\|refused\|expired, trace_id, created_at, resolved_at)`; `api.agent.confirmations.ConfirmationStore(session)`: `create(...) -> PendingConfirmation`, `resolve(confirmation_id, customer_id, decision: Literal["confirm","refuse"]) -> PendingConfirmation` (404 igual para inexistente/alheia; 409 se não está pending/expirada), `refuse_pending(conversation_id) -> list[PendingConfirmation]`, `expire_stale(now) -> int` | TASK-075 | TASK-076, TASK-077, TASK-078 |
| CT-45 | `api.agent.events.AgentEvent` (união discriminada por `type`: `token{text}`, `tool_start{call_id, tool, label_key}`, `tool_end{call_id, tool, ok}`, `citation{index, document, section, page}`, `done{status, message_id, conversation_id, trace_id, confirmation?}`, `error{code, message, trace_id}`); `api.agent.loop.AgentLoop(llm, registry, settings, audit_reader)`: `run(turn: AgentTurn) -> AsyncIterator[AgentEvent]` e `resume(state: AgentState, decision: Literal["confirm","refuse"], ctx: ToolContext) -> AsyncIterator[AgentEvent]`; `AgentState` serializável em JSON (mensagens, passos, custo, chamadas recusadas, histórico de repetição) | TASK-076 | TASK-077, TASK-084, TASK-093, TASK-094, TASK-095, TASK-107 |
| CT-46 | HTTP `POST /chat/confirmations/{confirmation_id}` body `{decision: "confirm"\|"refuse"}` → 200 `ChatResponse` (CT-14); 404 `CONFIRMATION_NOT_FOUND`; 409 `CONFIRMATION_NOT_PENDING` | TASK-078 | TASK-080 |
| CT-47 | `api.agent.guards`: `StepBudget(max_steps).consume() -> bool`, `CostBudget(max_usd).add(cost) -> bool`, `RepeatDetector.check(call: ToolCall) -> Literal["ok","repeat_error","terminate"]`, `limit_reason` em `step_limit\|cost_limit\|repeat_limit` | TASK-083 | TASK-084 |
| CT-48 | `api.triage.schemas`: `TriageRequest{text: str (1..ATLAS_MAX_MESSAGE_CHARS)}` (extra=forbid), `TriageResult{categoria: Literal[pedido,assinatura,reembolso,entrega,conta,outro], prioridade: Literal[baixa,media,alta,urgente], sentimento: Literal[positivo,neutro,negativo], precisa_humano: bool, resumo: str}` (extra=forbid) | TASK-087 | TASK-088, TASK-089 |
| CT-49 | HTTP `POST /triage` body `TriageRequest` → 200 `TriageResult`; 422 `VALIDATION_ERROR`; 401 `AUTH_REQUIRED`; 503 `TRIAGE_UNAVAILABLE`; acesso: sessão de persona OU `X-Operator-Key` (LAC-35) | TASK-089 | — |
| CT-50 | `api.streaming.events.encode_sse(event: AgentEvent) -> bytes` (`event: <type>\ndata: <json>\n\n`) e `guard_terminal(events: AsyncIterator[AgentEvent]) -> AsyncIterator[AgentEvent]` (`done` único e último; nada após `error`) | TASK-093 | TASK-095 |
| CT-51 | Web `ChatStreamService.stream(req): Observable<StreamEvent>` (fetch + `AbortController`; unsubscribe aborta) e `parseSse(chunk: string, buffer: string): {events: StreamEvent[], rest: string}` em `sse-parser.ts` | TASK-096 | TASK-097 |
| CT-52 | `api.guardrails.patterns`: `INJECTION_PATTERNS: dict[Lang, tuple[re.Pattern, ...]]`, `ACTION_PROMISE_PATTERNS: dict[str, dict[Lang, tuple[re.Pattern, ...]]]` (chave = nome da tool), `find_injection(text: str) -> list[str]` | TASK-100 | TASK-102, TASK-103, TASK-106 |
| CT-53 | `api.guardrails.pii.mask_pii(text: str) -> MaskResult(text: str, findings: list[PiiFinding(kind: Literal["card","national_id","email","phone"], start: int, end: int)])` (substitui por `[CARD]`, `[ID]`, `[EMAIL]`, `[PHONE]`) | TASK-101 | TASK-102 |
| CT-54 | `api.guardrails.input_guard.InputGuard(settings).check(text: str) -> InputVerdict(action: Literal["allow","block"], masked_text: str, reasons: list[str])` (exceção interna → `block` com reason `guard_failure`) | TASK-102 | TASK-107 |
| CT-55 | `api.guardrails.output_guard.OutputGuard(llm: LLMClient, settings)`: `async check_sentence(sentence: str, ctx: OutputContext) -> OutputVerdict(action: Literal["allow","block"], reason: str \| None)`, `async check_topic(question: str, first_sentence: str, lang: Lang) -> OutputVerdict`; `OutputContext(customer_id: UUID, own_order_ids: set[int], own_email: str, successful_tools: set[str], question: str, language: Lang)` | TASK-103 | TASK-104, TASK-107 |
| CT-56 | `api.llm.retry.RetryingLLMClient(inner: LLMClient, *, max_retries: int, base_delay_s: float, timeout_s: float, sleep=asyncio.sleep, rng=random.random)` (implementa CT-6) | TASK-115 | TASK-121 |
| CT-57 | `api.llm.circuit_breaker.CircuitBreaker(name: str, failure_threshold: int, reset_seconds: float, clock=time.monotonic)` com `state: Literal["closed","open","half_open"]`, `allow() -> bool`, `record_success()`, `record_failure()`; `BreakerLLMClient(inner, breaker)` (implementa CT-6; aberto → `CircuitOpen(LLMError, recoverable=True)`) | TASK-116 | TASK-118, TASK-121 |
| CT-58 | `api.llm.chaos.ChaosLLMClient(inner: LLMClient, rate: float, rng: random.Random \| None = None)` (implementa CT-6) | TASK-117 | TASK-121 |
| CT-59 | `api.llm.fallback.FallbackLLMClient(chain: list[tuple[str, LLMClient, CircuitBreaker]])` (implementa CT-6) e `AllProvidersFailed(Exception)`; no `stream`, troca de provedor só antes do 1º `text_delta` | TASK-118 | TASK-121 |
| CT-60 | ORM `api.tickets.models.Ticket(id, conversation_id, customer_id, reason, trace_id, created_at)` | TASK-119 | TASK-120 |
| CT-61 | `api.tickets.service.open_ticket(session, *, conversation_id: UUID, customer_id: UUID, reason: str, trace_id: str) -> Ticket` | TASK-120 | TASK-121 |
| CT-62 | `api.observability.tracing`: `init_tracing(settings) -> None`, `start_span(name: str, **attributes: object) -> ContextManager[Span]`, constantes `ATTR_SESSION_ID = "langfuse.session.id"`, `ATTR_MODEL = "gen_ai.request.model"`, `ATTR_INPUT_TOKENS = "gen_ai.usage.input_tokens"`, `ATTR_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"`, `ATTR_COST = "atlas.cost_usd"`, `ATTR_FALLBACK = "atlas.fallback"`; exportação nunca bloqueia nem propaga erro | TASK-132 | TASK-133 |
| CT-63 | `api.observability.langfuse_api.LangfuseApi(settings, http: httpx.AsyncClient \| None = None)`: `async push_score(trace_id: str, value: int, comment: str \| None = None) -> bool`, `async delete_traces_older_than(cutoff: datetime) -> int` | TASK-134 | TASK-136, TASK-147 |
| CT-64 | ORM `api.feedback.models.Feedback(id, message_id UNIQUE, customer_id, rating: Literal["up","down"], trace_id, created_at)` | TASK-135 | TASK-136 |
| CT-65 | HTTP `POST /messages/{message_id}/feedback` body `{rating: "up"\|"down"}` → 204; 404 `MESSAGE_NOT_FOUND` (inexistente ou de outro cliente); 401 `AUTH_REQUIRED` | TASK-136 | TASK-137 |
| CT-66 | ORM `api.limits.models`: `LlmCall(id, trace_id, provider, model, cost_usd: Numeric(12,6), created_at)`, `RateLimitEvent(id, customer_id, created_at)`, `MaintenanceRun(id, job, run_date, finished_at)` com `UNIQUE(job, run_date)` | TASK-141 | TASK-142, TASK-143, TASK-144 |
| CT-67 | `api.limits.ledger.record_llm_call(*, trace_id: str, provider: str, model: str, cost_usd: Decimal, created_at: datetime) -> None` (sessão própria; erro só gera log) | TASK-142 | — |
| CT-68 | `api.limits.spend_cap.SpendCap(settings).check(session, now: datetime) -> CapDecision(allowed: bool, spent_usd: Decimal, resets_at: datetime)` | TASK-143 | TASK-145 |
| CT-69 | `api.limits.rate_limit.RateLimiter(settings).check_and_record(session, customer_id: UUID, now: datetime) -> RateDecision(allowed: bool, retry_at: datetime \| None)` | TASK-144 | TASK-145 |
| CT-70 | `api.limits.deps`: dependências FastAPI `enforce_chat_limits(principal, session)` (rate limit + teto) e `enforce_llm_budget(session)` (só teto) → `AtlasError(RATE_LIMITED\|DAILY_CAP_REACHED, 429, {retry_at})` | TASK-145 | — |
| CT-71 | `api.maintenance.restore.restore_demo(session, now: datetime) -> RestoreReport(conversations_deleted: int, orders_reset: int)` | TASK-146 | TASK-148 |
| CT-72 | `api.maintenance.purge.purge_expired(session, now: datetime, retention_days: int, langfuse: LangfuseApi \| None) -> PurgeReport(conversations: int, audit_rows: int, emails: int, tickets: int, llm_calls: int, langfuse_traces: int)` | TASK-147 | TASK-148 |
| CT-73 | `api.maintenance.scheduler.start_scheduler(app) -> asyncio.Task` e `async tick(now: datetime) -> list[str]` (jobs executados) | TASK-148 | — |
| CT-74 | `api.triage.service.TriageService(llm: LLMClient, settings)`: `async classify(text: str, mode: Literal["prompt_only","schema"] \| None = None) -> TriageResult` (levanta `AtlasError(TRIAGE_UNAVAILABLE, 503)`) | TASK-088 | TASK-089, TASK-090, TASK-126 |
| CT-75 | `api.guardrails.stream_filter.StreamOutputFilter(guard: OutputGuard, ctx: OutputContext)`: `async feed(delta: str) -> list[str]` (só frases aprovadas), `async finish() -> FilterOutcome(blocked: bool, released: str, reason: str \| None)` | TASK-104 | TASK-107 |
| CT-76 | HTTP `POST /chat/stream` (body CT-14) e `POST /chat/confirmations/{id}/stream` (body CT-46) → 200 `text/event-stream` com os eventos de CT-45 (`event: token\|tool_start\|tool_end\|citation\|done\|error`); 401 antes de abrir o stream | TASK-095 | TASK-096, TASK-098 |

### 8.3 Catálogo de erros

Mensagens ao usuário em pt e en vêm de `api/messages.py` (conteúdo da seção 9 do spec). A coluna abaixo é o conteúdo,
não a redação final.

| Código | Quando ocorre | Mensagem ao usuário | HTTP |
|---|---|---|---|
| `VALIDATION_ERROR` | corpo inválido; mensagem vazia (CHAT-90); triagem vazia (TRI-90) | "A mensagem está vazia." / campo inválido | 422 |
| `MESSAGE_TOO_LONG` | mensagem > `ATLAS_MAX_MESSAGE_CHARS` (CHAT-91) | "A mensagem passou de N caracteres." | 422 |
| `AUTH_REQUIRED` | sem sessão válida (TOOL-95, DEMO-01) ou sem chave de operador na triagem | "Entre com uma conta de teste." | 401 |
| `INVALID_CREDENTIALS` | login inválido | "Usuário ou senha inválidos." | 401 |
| `CONVERSATION_NOT_FOUND` | conversa inexistente ou de outro cliente (CHAT-94) | "Conversa não encontrada." | 404 |
| `CONFIRMATION_NOT_FOUND` | confirmação inexistente ou alheia | "Ação não encontrada." | 404 |
| `CONFIRMATION_NOT_PENDING` | confirmação já resolvida ou expirada (TOOL-96) | "Essa ação não está mais aguardando confirmação." | 409 |
| `MESSAGE_NOT_FOUND` | feedback em mensagem inexistente/alheia (OBS-92) | "Mensagem não encontrada." | 404 |
| `RATE_LIMITED` | > 20 msg/h por conta (DEMO-02) | "Você atingiu o limite; tente de novo às HH:MM." | 429 |
| `DAILY_CAP_REACHED` | teto diário atingido (DEMO-03) | "A demo atingiu o limite diário; volta às 00:00 UTC." | 429 |
| `LLM_UNAVAILABLE` | provedor falhou antes da fase 12 (CHAT-92) | "Não consegui responder agora; tente de novo." | 502 |
| `TRIAGE_UNAVAILABLE` | JSON inválido após 2 reparos (TRI-07) | "Triagem indisponível." | 503 |
| `INTERNAL_ERROR` | exceção não tratada | "Erro inesperado." (sem stack trace) | 500 |
| `STREAM_ERROR` | erro durante o stream (STR-90, RES-93) | "Houve um erro; tente de novo." | evento `error` |

Desfechos de negócio que **não** são erro HTTP (200 com `status`): `blocked` (GRD-04/06/08/93, mensagem padrão de
bloqueio), `degraded` (RES-06, "tente em instantes" + ticket), `limit_reached` (AGT-08, lista de ações),
`UNSUPPORTED_LANGUAGE` (CHAT-95, texto fixo em inglês), `KNOWLEDGE_UNAVAILABLE` (RAG-94).

Erros devolvidos **ao modelo** como resultado de tool (nunca ao Cliente diretamente): `ORDER_NOT_FOUND` (TOOL-90),
`ORDER_NOT_CANCELLABLE` (TOOL-91/99), `REFUND_NOT_ELIGIBLE` (TOOL-15), `ALREADY_REFUNDED` (TOOL-93),
`INVALID_ARGUMENTS` (TOOL-94), `UNKNOWN_TOOL` (AGT-91), `ACTION_ALREADY_REFUSED` (TOOL-14), `REPEATED_CALL` (AGT-09),
`customer_refused` (TOOL-13).

Códigos de saída do gate de evals: `0` aprovado/base criada, `1` regressão de score, `2` erro de execução (EVAL-90).

## 9. Componentes Afetados

Todos os arquivos são **novos** (repositório greenfield). "altera" indica que uma task posterior da própria feature
modifica um arquivo criado antes.

### 9.1 Arquivos de produção

| Arquivo / módulo | Tipo de impacto | Requisitos atendidos |
|---|---|---|
| `pyproject.toml` | cria | `FND-90`, `CHAT-08`, `CHAT-09`, `DEMO-08`, `DEMO-05` |
| `api/config.py` | cria | `FND-90`, `CHAT-08`, `CHAT-09`, `DEMO-08`, `DEMO-05` |
| `api/observability/logging.py` | cria | `FND-03`, `FND-90` |
| `api/observability/middleware.py` | cria | `FND-03`, `FND-90` |
| `api/errors.py` | cria | `CHAT-92`, `CHAT-95` |
| `api/messages.py` | cria | `CHAT-92`, `CHAT-95` |
| `api/db/engine.py` | cria | `FND-01` |
| `api/db/migrations/versions/0001_enable_pgvector.py` | cria | `FND-01` |
| `api/llm/client.py` | cria | `CHAT-01` |
| `api/llm/fake.py` | cria | `CHAT-01` |
| `api/llm/anthropic_adapter.py` | cria (TASK-006) e altera (TASK-092, TASK-111) | `CHAT-01`, `FND-90`, `TRI-02`, `STR-01`, `CACHE-01`, `CACHE-03`, `CACHE-90` |
| `api/llm/pricing.py` | cria (TASK-007) e altera (TASK-111) | `FND-04`, `CACHE-03`, `CACHE-01`, `CACHE-90` |
| `config/pricing.json` | cria | `FND-04`, `CACHE-03` |
| `api/llm/tokens.py` | cria | `CHAT-05` |
| `api/llm/instrumented.py` | cria (TASK-009) e altera (TASK-133) | `FND-04`, `CHAT-05`, `FND-90`, `OBS-01` |
| `api/llm/factory.py` | cria (TASK-009) e altera (TASK-121) | `FND-04`, `CHAT-05`, `FND-90`, `RES-05`, `RES-06`, `RES-91`, `RES-92`, `RES-93` |
| `api/main.py` | cria | `FND-01`, `FND-03` |
| `api/health.py` | cria | `FND-01`, `FND-03` |
| `evals/runner.py` | cria | `FND-05` |
| `evals/dataset.py` | cria | `FND-05` |
| `web/package.json` | cria | `FND-01` |
| `web/angular.json` | cria | `FND-01` |
| `docker-compose.yml` | cria (TASK-013) e altera (TASK-138) | `FND-01`, `OBS-03`, `OBS-07` |
| `Dockerfile` | cria | `FND-01` |
| `.github/workflows/ci.yml` | cria | `FND-02` |
| `README.md` | cria (TASK-015) e altera (TASK-150) | `FND-01`, `DOD-04`, `DOD-03`, `DEMO-04`, `DEMO-06`, `STR-06` |
| `docs/results.md` | cria | `FND-01`, `DOD-04` |
| `scripts/check_phase.py` | cria | `DOD-01`, `DOD-04` |
| `demos/common.py` | cria | `DOD-02` |
| `api/prompts.py` | cria | `CHAT-10`, `RAG-04` |
| `prompts/system.md` | cria | `CHAT-10`, `RAG-04` |
| `api/chat/service.py` | cria (TASK-019) e altera (TASK-031, TASK-049, TASK-077, TASK-094, TASK-107, TASK-121) | `CHAT-01`, `CHAT-09`, `CHAT-90`, `CHAT-91`, `CHAT-92`, `CHAT-03`, `CHAT-04`, `CHAT-06`, `CHAT-10`, `CHAT-95`, `CHAT-93`, `RAG-03`, `RAG-04`, `RAG-90`, `RAG-94`, `TOOL-05`, `TOOL-11`, `TOOL-13`, `TOOL-96`, `TOOL-97`, `STR-01`, `STR-03`, `STR-04`, `STR-05`, `STR-07`, `STR-91`, `GRD-01`, `GRD-04`, `GRD-06`, `GRD-07`, `GRD-08`, `GRD-91`, `GRD-93`, `OBS-91`, `RES-05`, `RES-06`, `RES-91`, `RES-92`, `RES-93` |
| `api/chat/schemas.py` | cria | `CHAT-01`, `CHAT-09`, `CHAT-90`, `CHAT-91`, `CHAT-92` |
| `api/chat/routes.py` | cria (TASK-020) e altera (TASK-032, TASK-078) | `CHAT-01`, `CHAT-90`, `CHAT-91`, `CHAT-92`, `CHAT-94`, `TOOL-95`, `DEMO-01`, `TOOL-05`, `TOOL-13` |
| `demos/demo_01_stateless.py` | cria | `CHAT-02`, `DOD-02` |
| `docs/adr/0001-llm-api-call.md` | cria | `DOD-01`, `DOD-04` |
| `evals/datasets/phase_01.jsonl` | cria | `DOD-01`, `DOD-04` |
| `api/auth/models.py` | cria | `TOOL-95`, `DEMO-06` |
| `api/db/migrations/versions/0002_customers_sessions.py` | cria | `TOOL-95`, `DEMO-06` |
| `api/chat/models.py` | cria | `CHAT-03`, `CHAT-93` |
| `api/db/migrations/versions/0003_conversations_messages.py` | cria | `CHAT-03`, `CHAT-93` |
| `api/auth/service.py` | cria | `TOOL-95`, `DEMO-01`, `TOOL-03` |
| `api/auth/deps.py` | cria | `TOOL-95`, `DEMO-01`, `TOOL-03` |
| `api/auth/routes.py` | cria | `DEMO-01`, `TOOL-95` |
| `api/seed/personas.py` | cria | `DEMO-06` |
| `api/seed/__main__.py` | cria | `DEMO-06` |
| `api/chat/repository.py` | cria | `CHAT-03`, `CHAT-93`, `CHAT-94` |
| `api/chat/language.py` | cria | `CHAT-10`, `CHAT-95` |
| `api/chat/context.py` | cria | `CHAT-06`, `CHAT-08` |
| `prompts/summarize.md` | cria | `CHAT-06`, `CHAT-08` |
| `web/src/app/core/i18n/i18n.service.ts` | cria | `STR-08`, `STR-09`, `DEMO-11` |
| `web/src/app/core/i18n/messages.ts` | cria | `STR-08`, `STR-09`, `DEMO-11` |
| `web/src/app/core/auth/auth.service.ts` | cria | `DEMO-01` |
| `web/src/app/core/auth/auth.guard.ts` | cria | `DEMO-01` |
| `web/src/app/features/login/login.page.ts` | cria | `DEMO-01`, `DEMO-06`, `DEMO-11`, `STR-09` |
| `web/src/app/features/login/login.page.html` | cria | `DEMO-01`, `DEMO-06`, `DEMO-11`, `STR-09` |
| `web/src/app/core/chat/chat-api.service.ts` | cria | `CHAT-01`, `TOOL-05` |
| `web/src/app/core/chat/chat.models.ts` | cria | `CHAT-01`, `TOOL-05` |
| `web/src/app/features/chat/chat.page.ts` | cria (TASK-037) e altera (TASK-080, TASK-097) | `CHAT-01`, `CHAT-90`, `CHAT-91`, `STR-08`, `TOOL-05`, `TOOL-13`, `STR-02`, `STR-03`, `STR-04`, `STR-90`, `STR-92` |
| `web/src/app/features/chat/chat.page.html` | cria (TASK-037) e altera (TASK-080, TASK-097) | `CHAT-01`, `CHAT-90`, `CHAT-91`, `STR-08`, `TOOL-05`, `TOOL-13`, `STR-02`, `STR-03`, `STR-04`, `STR-90`, `STR-92` |
| `demos/demo_02_context_window.py` | cria | `CHAT-02`, `CHAT-07`, `DOD-02` |
| `docs/adr/0002-context-window.md` | cria | `DOD-01`, `DOD-04` |
| `evals/datasets/phase_02.jsonl` | cria | `DOD-01`, `DOD-04` |
| `corpus/en/` | cria | `RAG-02`, `RAG-14`, `TOOL-16`, `TOOL-19` |
| `corpus/manifest.yaml` | cria | `RAG-02`, `RAG-14`, `TOOL-16`, `TOOL-19` |
| `corpus/pt/` | cria | `RAG-02`, `RAG-14`, `TOOL-16`, `TOOL-19` |
| `scripts/build_corpus_pdfs.py` | cria | `RAG-02` |
| `corpus/pdf/` | cria | `RAG-02` |
| `api/rag/models.py` | cria (TASK-043) e altera (TASK-054) | `RAG-14`, `RAG-15`, `RAG-07` |
| `api/db/migrations/versions/0004_documents_chunks.py` | cria | `RAG-14`, `RAG-15` |
| `ingest/parsers.py` | cria | `RAG-01`, `RAG-91` |
| `ingest/chunking.py` | cria | `RAG-01`, `RAG-91` |
| `ingest/pipeline.py` | cria (TASK-045) e altera (TASK-055, TASK-106) | `RAG-01`, `RAG-02`, `RAG-14`, `RAG-91`, `RAG-92`, `RAG-07`, `RAG-93`, `GRD-03`, `GRD-91` |
| `ingest/report.py` | cria | `RAG-01`, `RAG-02`, `RAG-14`, `RAG-91`, `RAG-92` |
| `api/rag/fulltext.py` | cria | `RAG-15`, `RAG-05`, `RAG-16` |
| `api/rag/retriever.py` | cria (TASK-047) e altera (TASK-133) | `RAG-03`, `RAG-90`, `RAG-94`, `OBS-01` |
| `api/chat/prompt_builder.py` | cria (TASK-048) e altera (TASK-105, TASK-110) | `RAG-03`, `RAG-04`, `GRD-02`, `CACHE-01`, `CACHE-02` |
| `api/rag/citations.py` | cria | `RAG-03`, `RAG-04` |
| `evals/judge.py` | cria | `RAG-18`, `EVAL-02` |
| `prompts/judge_faithfulness.md` | cria | `RAG-18`, `EVAL-02` |
| `demos/demo_03_rag.py` | cria | `RAG-05`, `RAG-06`, `RAG-16`, `RAG-18`, `RAG-19`, `DOD-02` |
| `demos/data/phase03_questions.jsonl` | cria | `RAG-05`, `RAG-06`, `RAG-16`, `RAG-18`, `RAG-19`, `DOD-02` |
| `docs/adr/0003-rag-full-text.md` | cria | `DOD-01`, `DOD-04` |
| `evals/datasets/phase_03.jsonl` | cria | `DOD-01`, `DOD-04` |
| `api/rag/embeddings.py` | cria | `RAG-07`, `RAG-93`, `RAG-94` |
| `api/db/migrations/versions/0005_chunk_embeddings.py` | cria | `RAG-07` |
| `api/rag/vector_bruteforce.py` | cria | `RAG-08`, `RAG-17` |
| `demos/bench.py` | cria | `RAG-09`, `RAG-11` |
| `demos/demo_04_embeddings.py` | cria | `RAG-08`, `RAG-09`, `RAG-17`, `DOD-02` |
| `demos/data/synonym_questions.jsonl` | cria | `RAG-08`, `RAG-09`, `RAG-17`, `DOD-02` |
| `docs/adr/0004-embeddings.md` | cria | `DOD-01`, `DOD-04` |
| `evals/datasets/phase_04.jsonl` | cria | `DOD-01`, `DOD-04` |
| `api/db/migrations/versions/0006_hnsw_and_bench.py` | cria | `RAG-10` |
| `ingest/reindex.py` | cria | `RAG-10` |
| `api/rag/vector_hnsw.py` | cria | `RAG-10` |
| `api/rag/hybrid.py` | cria | `RAG-12` |
| `demos/demo_05_hnsw.py` | cria | `RAG-11`, `RAG-13`, `DOD-02` |
| `docs/adr/0005-vector-database.md` | cria | `DOD-01`, `DOD-04` |
| `evals/datasets/phase_05.jsonl` | cria | `DOD-01`, `DOD-04` |
| `api/tools/models.py` | cria | `TOOL-17`, `TOOL-19`, `TOOL-93`, `TOOL-06` |
| `api/db/migrations/versions/0007_commerce.py` | cria | `TOOL-17`, `TOOL-19`, `TOOL-93`, `TOOL-06` |
| `api/seed/commerce.py` | cria | `TOOL-01`, `DEMO-06`, `TOOL-19`, `TOOL-98` |
| `api/tools/audit.py` | cria | `TOOL-06`, `TOOL-07`, `TOOL-08` |
| `api/tools/registry.py` | cria | `TOOL-02`, `TOOL-03`, `TOOL-12`, `TOOL-94`, `AGT-91` |
| `api/tools/orders.py` | cria | `TOOL-04`, `TOOL-06`, `TOOL-10`, `TOOL-17`, `TOOL-19`, `TOOL-90`, `TOOL-91`, `TOOL-92`, `TOOL-99`, `DEMO-91` |
| `api/tools/refund_policy.py` | cria | `TOOL-16`, `TOOL-98` |
| `api/tools/refunds.py` | cria | `TOOL-04`, `TOOL-07`, `TOOL-09`, `TOOL-15`, `TOOL-90`, `TOOL-93`, `DEMO-91` |
| `api/tools/subscriptions.py` | cria | `TOOL-04`, `TOOL-08`, `TOOL-10`, `TOOL-18` |
| `api/tools/email.py` | cria | `TOOL-04`, `TOOL-08`, `TOOL-10`, `TOOL-18` |
| `api/tools/catalog.py` | cria | `TOOL-02`, `TOOL-12` |
| `api/agent/models.py` | cria | `TOOL-05`, `TOOL-96` |
| `api/db/migrations/versions/0008_pending_confirmations.py` | cria | `TOOL-05`, `TOOL-96` |
| `api/agent/confirmations.py` | cria | `TOOL-05`, `TOOL-13`, `TOOL-96`, `TOOL-97` |
| `api/agent/loop.py` | cria (TASK-076) e altera (TASK-084, TASK-094, TASK-107) | `AGT-01`, `AGT-04`, `AGT-90`, `AGT-91`, `TOOL-05`, `TOOL-12`, `TOOL-13`, `TOOL-14`, `AGT-02`, `AGT-03`, `AGT-05`, `AGT-06`, `AGT-08`, `AGT-09`, `AGT-10`, `STR-01`, `STR-03`, `STR-04`, `STR-05`, `STR-07`, `STR-91`, `GRD-01`, `GRD-04`, `GRD-06`, `GRD-07`, `GRD-08`, `GRD-91`, `GRD-93`, `OBS-91` |
| `api/agent/events.py` | cria | `AGT-01`, `AGT-04`, `AGT-90`, `AGT-91`, `TOOL-05`, `TOOL-12`, `TOOL-13`, `TOOL-14` |
| `web/src/app/features/chat/confirmation-card.component.ts` | cria | `TOOL-05`, `TOOL-19` |
| `web/src/app/shared/money.pipe.ts` | cria | `TOOL-05`, `TOOL-19` |
| `demos/demo_06_tools.py` | cria | `TOOL-11`, `DOD-02` |
| `docs/adr/0006-tool-calling.md` | cria | `DOD-01`, `DOD-04` |
| `evals/datasets/phase_06.jsonl` | cria | `DOD-01`, `DOD-04` |
| `api/agent/guards.py` | cria | `AGT-02`, `AGT-03`, `AGT-05`, `AGT-09`, `AGT-10` |
| `demos/demo_07_agent.py` | cria | `AGT-06`, `AGT-07`, `DOD-02` |
| `demos/data/phase07_tasks.jsonl` | cria | `AGT-06`, `AGT-07`, `DOD-02` |
| `docs/adr/0007-agent-loop.md` | cria | `DOD-01`, `DOD-04` |
| `evals/datasets/phase_07.jsonl` | cria | `DOD-01`, `DOD-04` |
| `api/triage/schemas.py` | cria | `TRI-01`, `TRI-05`, `TRI-06`, `TRI-08`, `TRI-90` |
| `prompts/triage.md` | cria | `TRI-01`, `TRI-05`, `TRI-06`, `TRI-08`, `TRI-90` |
| `api/triage/service.py` | cria | `TRI-02`, `TRI-03`, `TRI-07`, `TRI-08` |
| `api/triage/routes.py` | cria | `TRI-01`, `TRI-05`, `TRI-07`, `TRI-90` |
| `demos/demo_08_triage.py` | cria | `TRI-04`, `DOD-02` |
| `demos/data/phase08_inputs.jsonl` | cria | `TRI-04`, `DOD-02` |
| `docs/adr/0008-structured-output.md` | cria | `DOD-01`, `DOD-04` |
| `evals/datasets/phase_08.jsonl` | cria | `DOD-01`, `DOD-04` |
| `api/streaming/events.py` | cria | `STR-01`, `STR-05`, `STR-90` |
| `api/chat/stream_routes.py` | cria | `STR-01`, `STR-04`, `STR-05`, `STR-90`, `STR-91` |
| `web/src/app/core/chat/chat-stream.service.ts` | cria | `STR-01`, `STR-04`, `STR-90` |
| `web/src/app/core/chat/sse-parser.ts` | cria | `STR-01`, `STR-04`, `STR-90` |
| `demos/demo_09_streaming.py` | cria | `STR-06`, `DOD-02` |
| `docs/adr/0009-streaming.md` | cria | `DOD-01`, `DOD-04` |
| `evals/datasets/phase_09.jsonl` | cria | `DOD-01`, `DOD-04` |
| `api/guardrails/patterns.py` | cria | `GRD-09`, `GRD-90` |
| `api/guardrails/pii.py` | cria | `GRD-07`, `OBS-91` |
| `api/guardrails/input_guard.py` | cria | `GRD-01`, `GRD-06`, `GRD-93`, `GRD-90` |
| `api/guardrails/output_guard.py` | cria | `GRD-04`, `GRD-08`, `GRD-93` |
| `prompts/topic_check.md` | cria | `GRD-04`, `GRD-08`, `GRD-93` |
| `api/guardrails/stream_filter.py` | cria | `GRD-04`, `GRD-08` |
| `demos/demo_10_guardrails.py` | cria | `GRD-05`, `GRD-09`, `GRD-90`, `GRD-91`, `DOD-02` |
| `evals/datasets/redteam.jsonl` | cria | `GRD-05`, `GRD-09`, `GRD-90`, `GRD-91`, `DOD-02` |
| `docs/adr/0010-guardrails.md` | cria | `DOD-01`, `DOD-04` |
| `evals/datasets/phase_10.jsonl` | cria | `DOD-01`, `DOD-04` |
| `demos/demo_11_prompt_cache.py` | cria | `CACHE-04`, `DOD-02` |
| `docs/adr/0011-prompt-cache.md` | cria | `DOD-01`, `DOD-04` |
| `evals/datasets/phase_11.jsonl` | cria | `DOD-01`, `DOD-04` |
| `api/llm/openai_adapter.py` | cria | `RES-05`, `CACHE-90`, `TRI-02` |
| `api/llm/retry.py` | cria | `RES-01`, `RES-02`, `RES-03` |
| `api/llm/circuit_breaker.py` | cria | `RES-04` |
| `api/llm/chaos.py` | cria | `RES-07`, `DEMO-05` |
| `api/llm/fallback.py` | cria | `RES-05`, `RES-91`, `RES-92`, `RES-93` |
| `api/tickets/models.py` | cria | `RES-06` |
| `api/db/migrations/versions/0009_tickets.py` | cria | `RES-06` |
| `api/tickets/service.py` | cria | `RES-06` |
| `demos/demo_12_resilience.py` | cria | `RES-08`, `DOD-02` |
| `docs/adr/0012-retry-fallback.md` | cria | `DOD-01`, `DOD-04` |
| `evals/datasets/phase_12.jsonl` | cria | `DOD-01`, `DOD-04` |
| `evals/datasets/core_en.jsonl` | cria | `EVAL-01`, `EVAL-08` |
| `evals/datasets/core_pt.jsonl` | cria | `EVAL-01`, `EVAL-08` |
| `evals/graders.py` | cria | `EVAL-02` |
| `evals/targets.py` | cria | `EVAL-01`, `EVAL-02` |
| `evals/gate.py` | cria | `EVAL-05`, `EVAL-07`, `EVAL-90`, `EVAL-91`, `EVAL-92` |
| `evals/report.py` | cria | `EVAL-03`, `DOD-04` |
| `.github/workflows/evals.yml` | cria | `EVAL-03`, `EVAL-04`, `EVAL-05`, `EVAL-90` |
| `demos/demo_13_eval_gate.py` | cria | `EVAL-06`, `DOD-02` |
| `docs/adr/0013-evals.md` | cria | `DOD-01`, `DOD-04` |
| `evals/datasets/phase_13.jsonl` | cria | `DOD-01`, `DOD-04` |
| `api/observability/tracing.py` | cria | `OBS-01`, `OBS-91`, `OBS-93` |
| `api/observability/langfuse_api.py` | cria | `OBS-02`, `DEMO-10` |
| `api/feedback/models.py` | cria | `OBS-02` |
| `api/db/migrations/versions/0010_feedback.py` | cria | `OBS-02` |
| `api/feedback/routes.py` | cria | `OBS-02`, `OBS-92` |
| `web/src/app/features/chat/message-meta.component.ts` | cria | `OBS-02`, `OBS-06` |
| `web/src/app/core/chat/feedback-api.service.ts` | cria | `OBS-02`, `OBS-06` |
| `docs/observability.md` | cria | `OBS-03`, `OBS-07` |
| `demos/demo_14_trace_to_eval.py` | cria | `OBS-05`, `DOD-02` |
| `docs/adr/0014-observability.md` | cria | `DOD-01`, `DOD-04` |
| `evals/datasets/phase_14.jsonl` | cria | `DOD-01`, `DOD-04` |
| `api/limits/models.py` | cria | `DEMO-02`, `DEMO-03` |
| `api/db/migrations/versions/0011_usage_and_maintenance.py` | cria | `DEMO-02`, `DEMO-03` |
| `api/limits/ledger.py` | cria | `DEMO-03` |
| `api/limits/spend_cap.py` | cria | `DEMO-03`, `DEMO-08`, `DEMO-09` |
| `api/limits/rate_limit.py` | cria | `DEMO-02`, `DEMO-08`, `DEMO-09` |
| `api/limits/deps.py` | cria | `DEMO-02`, `DEMO-03`, `DEMO-09` |
| `api/maintenance/restore.py` | cria | `DEMO-07`, `DEMO-12`, `DEMO-91` |
| `api/maintenance/purge.py` | cria | `DEMO-10`, `DEMO-12` |
| `api/maintenance/scheduler.py` | cria | `DEMO-07`, `DEMO-10` |
| `render.yaml` | cria | `DEMO-05`, `DEMO-04` |

### 9.2 Arquivos de suporte e wiring novos

Criados ou tocados só via "Wiring permitido" (registro, boilerplate, dados de teste), também novos:
`.gitignore`, `.env.example`, `.env` (cópia local, não versionada), `.dockerignore`, `uv.lock`, `alembic.ini`,
`api/__init__.py` e demais `__init__.py` de pacote (`api/*/__init__.py`, `ingest/__init__.py`, `evals/__init__.py`,
`demos/__init__.py`), `api/db/migrations/env.py` (usa `Base.metadata`; importa `api.auth.models`, `api.chat.models`,
`api.rag.models`, `api.tools.models`, `api.agent.models`, `api.tickets.models`, `api.feedback.models`,
`api.limits.models`), `api/db/migrations/script.py.mako`, `api/seed/__main__.py`, `ingest/__main__.py`,
`evals/__main__.py`, `docker/api-entrypoint.sh`, `tests/conftest.py`, `demos/data/`, `docs/adr/0000-template.md`,
`docs/img/architecture.svg`, `docs/img/demo.gif`, `docs/img/phase02_tokens.png`, `docs/review/phase03_sample.csv`,
`web/` (scaffold do `ng new`: `tsconfig*.json`, `eslint.config.js`, `src/**`, `public/**`, `package-lock.json`),
`web/vitest-base.config.ts`, `web/playwright.config.ts`, `web/proxy.conf.json` (`/api` → `http://localhost:8000`),
`web/Dockerfile`, `web/nginx.conf`, `web/src/app/app.routes.ts`, `web/src/app/app.config.ts`, `web/src/styles.scss`,
`web/e2e/regressao/`, `evals/datasets/smoke.jsonl`, `reports/` (saída, ignorada), imagem `pgvector/pgvector:pg16`.

## 10. Rastreabilidade Requisito → Componente

| ID do spec | Componentes / camadas | Contrato |
|---|---|---|
| `FND-01` | `api/db/engine.py`, `api/db/migrations/versions/0001_enable_pgvector.py`, `api/main.py`, `api/health.py`, `web/package.json`, `web/angular.json`, `docker-compose.yml`, `Dockerfile`, `README.md`, `docs/results.md` | CT-1, CT-2, CT-3, CT-5 |
| `FND-02` | `.github/workflows/ci.yml` | — |
| `FND-03` | `api/observability/logging.py`, `api/observability/middleware.py`, `api/main.py`, `api/health.py` | CT-1, CT-2, CT-3, CT-5 |
| `FND-04` | `api/llm/pricing.py`, `config/pricing.json`, `api/llm/instrumented.py`, `api/llm/factory.py` | CT-1, CT-2, CT-6, CT-7, CT-8, CT-9 |
| `FND-05` | `evals/runner.py`, `evals/dataset.py` | CT-1, CT-10 |
| `CHAT-01` | `api/llm/client.py`, `api/llm/fake.py`, `api/llm/anthropic_adapter.py`, `api/chat/service.py`, `api/chat/schemas.py`, `api/chat/routes.py`, `web/src/app/core/chat/chat-api.service.ts`, `web/src/app/core/chat/chat.models.ts`, `web/src/app/features/chat/chat.page.ts`, `web/src/app/features/chat/chat.page.html` | CT-1, CT-3, CT-4, CT-6, CT-9, CT-13, CT-14, CT-15, CT-24, CT-26 |
| `CHAT-02` | `demos/demo_01_stateless.py`, `demos/demo_02_context_window.py` | CT-12, CT-15, CT-22 |
| `CHAT-03` | `api/chat/models.py`, `api/db/migrations/versions/0003_conversations_messages.py`, `api/chat/repository.py`, `api/chat/service.py` | CT-3, CT-4, CT-5, CT-15, CT-16, CT-18, CT-19, CT-20, CT-21, CT-22 |
| `CHAT-04` | `api/chat/service.py` | CT-4, CT-15, CT-16, CT-20, CT-21, CT-22 |
| `CHAT-05` | `api/llm/tokens.py`, `api/llm/instrumented.py`, `api/llm/factory.py` | CT-1, CT-2, CT-6, CT-7, CT-8, CT-9 |
| `CHAT-06` | `api/chat/context.py`, `prompts/summarize.md`, `api/chat/service.py` | CT-4, CT-6, CT-8, CT-13, CT-15, CT-16, CT-19, CT-20, CT-21, CT-22 |
| `CHAT-07` | `demos/demo_02_context_window.py` | CT-12, CT-15, CT-22 |
| `CHAT-08` | `pyproject.toml`, `api/config.py`, `api/chat/context.py`, `prompts/summarize.md` | CT-1, CT-6, CT-8, CT-13, CT-19, CT-22 |
| `CHAT-09` | `pyproject.toml`, `api/config.py`, `api/chat/service.py`, `api/chat/schemas.py` | CT-1, CT-3, CT-4, CT-6, CT-9, CT-13, CT-14, CT-15 |
| `CHAT-10` | `api/prompts.py`, `prompts/system.md`, `api/chat/language.py`, `api/chat/service.py` | CT-4, CT-13, CT-15, CT-16, CT-20, CT-21, CT-22 |
| `RAG-01` | `ingest/parsers.py`, `ingest/chunking.py`, `ingest/pipeline.py`, `ingest/report.py` | CT-5, CT-8, CT-27, CT-28 |
| `RAG-02` | `corpus/en/`, `corpus/manifest.yaml`, `corpus/pt/`, `scripts/build_corpus_pdfs.py`, `corpus/pdf/`, `ingest/pipeline.py`, `ingest/report.py` | CT-5, CT-27, CT-28 |
| `RAG-03` | `api/rag/retriever.py`, `api/chat/prompt_builder.py`, `api/rag/citations.py`, `api/chat/service.py` | CT-1, CT-4, CT-6, CT-13, CT-15, CT-22, CT-29, CT-30, CT-31, CT-32 |
| `RAG-04` | `api/prompts.py`, `prompts/system.md`, `api/chat/prompt_builder.py`, `api/rag/citations.py`, `api/chat/service.py` | CT-4, CT-6, CT-13, CT-15, CT-22, CT-30, CT-31, CT-32 |
| `RAG-05` | `api/rag/fulltext.py`, `demos/demo_03_rag.py`, `demos/data/phase03_questions.jsonl` | CT-12, CT-27, CT-28, CT-29, CT-33 |
| `RAG-06` | `demos/demo_03_rag.py`, `demos/data/phase03_questions.jsonl` | CT-12, CT-28, CT-29, CT-33 |
| `RAG-07` | `api/rag/embeddings.py`, `api/db/migrations/versions/0005_chunk_embeddings.py`, `api/rag/models.py`, `ingest/pipeline.py` | CT-1, CT-27, CT-28, CT-34 |
| `RAG-08` | `api/rag/vector_bruteforce.py`, `demos/demo_04_embeddings.py`, `demos/data/synonym_questions.jsonl` | CT-12, CT-29, CT-30, CT-34, CT-35, CT-37 |
| `RAG-09` | `demos/bench.py`, `demos/demo_04_embeddings.py`, `demos/data/synonym_questions.jsonl` | CT-12, CT-29, CT-35, CT-37 |
| `RAG-10` | `api/db/migrations/versions/0006_hnsw_and_bench.py`, `ingest/reindex.py`, `api/rag/vector_hnsw.py` | CT-28, CT-30, CT-35 |
| `RAG-11` | `demos/bench.py`, `demos/demo_05_hnsw.py` | CT-12, CT-35, CT-36, CT-37 |
| `RAG-14` | `corpus/en/`, `corpus/manifest.yaml`, `corpus/pt/`, `api/rag/models.py`, `api/db/migrations/versions/0004_documents_chunks.py`, `ingest/pipeline.py`, `ingest/report.py` | CT-5, CT-27, CT-28 |
| `RAG-15` | `api/rag/models.py`, `api/db/migrations/versions/0004_documents_chunks.py`, `api/rag/fulltext.py` | CT-5, CT-27, CT-29 |
| `RAG-16` | `api/rag/fulltext.py`, `demos/demo_03_rag.py`, `demos/data/phase03_questions.jsonl` | CT-12, CT-27, CT-28, CT-29, CT-33 |
| `RAG-17` | `api/rag/vector_bruteforce.py`, `demos/demo_04_embeddings.py`, `demos/data/synonym_questions.jsonl` | CT-12, CT-29, CT-30, CT-34, CT-35, CT-37 |
| `RAG-18` | `evals/judge.py`, `prompts/judge_faithfulness.md`, `demos/demo_03_rag.py`, `demos/data/phase03_questions.jsonl` | CT-6, CT-10, CT-12, CT-13, CT-28, CT-29, CT-33 |
| `RAG-19` | `demos/demo_03_rag.py`, `demos/data/phase03_questions.jsonl` | CT-12, CT-28, CT-29, CT-33 |
| `DOD-01` | `scripts/check_phase.py`, `docs/adr/0001-llm-api-call.md`, `evals/datasets/phase_01.jsonl`, `docs/adr/0002-context-window.md`, `evals/datasets/phase_02.jsonl`, `docs/adr/0003-rag-full-text.md`, `evals/datasets/phase_03.jsonl`, `docs/adr/0004-embeddings.md`, `evals/datasets/phase_04.jsonl`, `docs/adr/0005-vector-database.md`, `evals/datasets/phase_05.jsonl`, `docs/adr/0006-tool-calling.md`, `evals/datasets/phase_06.jsonl`, `docs/adr/0007-agent-loop.md`, `evals/datasets/phase_07.jsonl`, `docs/adr/0008-structured-output.md`, `evals/datasets/phase_08.jsonl`, `docs/adr/0009-streaming.md`, `evals/datasets/phase_09.jsonl`, `docs/adr/0010-guardrails.md`, `evals/datasets/phase_10.jsonl`, `docs/adr/0011-prompt-cache.md`, `evals/datasets/phase_11.jsonl`, `docs/adr/0012-retry-fallback.md`, `evals/datasets/phase_12.jsonl`, `docs/adr/0013-evals.md`, `evals/datasets/phase_13.jsonl`, `docs/adr/0014-observability.md`, `evals/datasets/phase_14.jsonl` | CT-10, CT-11 |
| `DOD-02` | `demos/common.py`, `demos/demo_01_stateless.py`, `demos/demo_02_context_window.py`, `demos/demo_03_rag.py`, `demos/data/phase03_questions.jsonl`, `demos/demo_04_embeddings.py`, `demos/data/synonym_questions.jsonl`, `demos/demo_05_hnsw.py`, `demos/demo_06_tools.py`, `demos/demo_07_agent.py`, `demos/data/phase07_tasks.jsonl`, `demos/demo_08_triage.py`, `demos/data/phase08_inputs.jsonl`, `demos/demo_09_streaming.py`, `demos/demo_10_guardrails.py`, `evals/datasets/redteam.jsonl`, `demos/demo_11_prompt_cache.py`, `demos/demo_12_resilience.py`, `demos/demo_13_eval_gate.py`, `demos/demo_14_trace_to_eval.py` | CT-1, CT-9, CT-10, CT-12, CT-15, CT-22, CT-28, CT-29, CT-33, CT-35, CT-36, CT-37, CT-74, CT-76 |
| `DOD-03` | `README.md` | — |
| `DOD-04` | `README.md`, `docs/results.md`, `scripts/check_phase.py`, `docs/adr/0001-llm-api-call.md`, `evals/datasets/phase_01.jsonl`, `docs/adr/0002-context-window.md`, `evals/datasets/phase_02.jsonl`, `docs/adr/0003-rag-full-text.md`, `evals/datasets/phase_03.jsonl`, `docs/adr/0004-embeddings.md`, `evals/datasets/phase_04.jsonl`, `docs/adr/0005-vector-database.md`, `evals/datasets/phase_05.jsonl`, `docs/adr/0006-tool-calling.md`, `evals/datasets/phase_06.jsonl`, `docs/adr/0007-agent-loop.md`, `evals/datasets/phase_07.jsonl`, `docs/adr/0008-structured-output.md`, `evals/datasets/phase_08.jsonl`, `docs/adr/0009-streaming.md`, `evals/datasets/phase_09.jsonl`, `docs/adr/0010-guardrails.md`, `evals/datasets/phase_10.jsonl`, `docs/adr/0011-prompt-cache.md`, `evals/datasets/phase_11.jsonl`, `docs/adr/0012-retry-fallback.md`, `evals/datasets/phase_12.jsonl`, `evals/report.py`, `docs/adr/0013-evals.md`, `evals/datasets/phase_13.jsonl`, `docs/adr/0014-observability.md`, `evals/datasets/phase_14.jsonl` | CT-10, CT-11 |
| `TOOL-01` | `api/seed/commerce.py` | CT-23, CT-38 |
| `TOOL-02` | `api/tools/registry.py`, `api/tools/catalog.py` | CT-6, CT-40, CT-42, CT-43 |
| `TOOL-03` | `api/auth/service.py`, `api/auth/deps.py`, `api/tools/registry.py` | CT-1, CT-3, CT-6, CT-16, CT-18, CT-40 |
| `TOOL-04` | `api/tools/orders.py`, `api/tools/refunds.py`, `api/tools/subscriptions.py`, `api/tools/email.py` | CT-38, CT-39, CT-40, CT-41, CT-42 |
| `TOOL-05` | `web/src/app/core/chat/chat-api.service.ts`, `web/src/app/core/chat/chat.models.ts`, `api/agent/models.py`, `api/db/migrations/versions/0008_pending_confirmations.py`, `api/agent/confirmations.py`, `api/agent/loop.py`, `api/agent/events.py`, `api/chat/service.py`, `api/chat/routes.py`, `web/src/app/features/chat/confirmation-card.component.ts`, `web/src/app/shared/money.pipe.ts`, `web/src/app/features/chat/chat.page.ts`, `web/src/app/features/chat/chat.page.html` | CT-3, CT-5, CT-6, CT-14, CT-15, CT-16, CT-20, CT-24, CT-26, CT-31, CT-39, CT-40, CT-43, CT-44, CT-45, CT-46 |
| `TOOL-06` | `api/tools/models.py`, `api/db/migrations/versions/0007_commerce.py`, `api/tools/audit.py`, `api/tools/orders.py` | CT-5, CT-18, CT-38, CT-39, CT-40, CT-42 |
| `TOOL-07` | `api/tools/audit.py`, `api/tools/refunds.py` | CT-38, CT-39, CT-40, CT-41, CT-42 |
| `TOOL-08` | `api/tools/audit.py`, `api/tools/subscriptions.py`, `api/tools/email.py` | CT-38, CT-39, CT-40 |
| `TOOL-09` | `api/tools/refunds.py` | CT-38, CT-39, CT-40, CT-41, CT-42 |
| `TOOL-10` | `api/tools/orders.py`, `api/tools/subscriptions.py`, `api/tools/email.py` | CT-38, CT-39, CT-40, CT-42 |
| `TOOL-11` | `api/chat/service.py`, `demos/demo_06_tools.py` | CT-12, CT-15, CT-20, CT-43, CT-44, CT-45 |
| `TOOL-12` | `api/tools/registry.py`, `api/tools/catalog.py`, `api/agent/loop.py`, `api/agent/events.py` | CT-6, CT-31, CT-39, CT-40, CT-42, CT-43, CT-44, CT-45 |
| `TOOL-13` | `api/agent/confirmations.py`, `api/agent/loop.py`, `api/agent/events.py`, `api/chat/service.py`, `api/chat/routes.py`, `web/src/app/features/chat/chat.page.ts`, `web/src/app/features/chat/chat.page.html` | CT-3, CT-6, CT-15, CT-16, CT-20, CT-26, CT-31, CT-39, CT-40, CT-43, CT-44, CT-45, CT-46 |
| `TOOL-14` | `api/agent/loop.py`, `api/agent/events.py` | CT-6, CT-31, CT-39, CT-40, CT-44, CT-45 |
| `TOOL-15` | `api/tools/refunds.py` | CT-38, CT-39, CT-40, CT-41, CT-42 |
| `TOOL-16` | `corpus/en/`, `corpus/manifest.yaml`, `corpus/pt/`, `api/tools/refund_policy.py` | CT-38, CT-41 |
| `TOOL-17` | `api/tools/models.py`, `api/db/migrations/versions/0007_commerce.py`, `api/tools/orders.py` | CT-5, CT-18, CT-38, CT-39, CT-40, CT-42 |
| `TOOL-18` | `api/tools/subscriptions.py`, `api/tools/email.py` | CT-38, CT-39, CT-40 |
| `TOOL-19` | `corpus/en/`, `corpus/manifest.yaml`, `corpus/pt/`, `api/tools/models.py`, `api/db/migrations/versions/0007_commerce.py`, `api/seed/commerce.py`, `api/tools/orders.py`, `web/src/app/features/chat/confirmation-card.component.ts`, `web/src/app/shared/money.pipe.ts` | CT-5, CT-18, CT-23, CT-24, CT-26, CT-38, CT-39, CT-40, CT-42 |
| `AGT-01` | `api/agent/loop.py`, `api/agent/events.py` | CT-6, CT-31, CT-39, CT-40, CT-44, CT-45 |
| `AGT-02` | `api/agent/guards.py`, `api/agent/loop.py` | CT-4, CT-6, CT-7, CT-39, CT-45, CT-47 |
| `AGT-03` | `api/agent/guards.py`, `api/agent/loop.py` | CT-4, CT-6, CT-7, CT-39, CT-45, CT-47 |
| `AGT-04` | `api/agent/loop.py`, `api/agent/events.py` | CT-6, CT-31, CT-39, CT-40, CT-44, CT-45 |
| `AGT-05` | `api/agent/guards.py`, `api/agent/loop.py` | CT-4, CT-6, CT-7, CT-39, CT-45, CT-47 |
| `AGT-06` | `api/agent/loop.py`, `demos/demo_07_agent.py`, `demos/data/phase07_tasks.jsonl` | CT-4, CT-12, CT-15, CT-39, CT-45, CT-47 |
| `AGT-07` | `demos/demo_07_agent.py`, `demos/data/phase07_tasks.jsonl` | CT-12, CT-15 |
| `AGT-08` | `api/agent/loop.py` | CT-4, CT-39, CT-45, CT-47 |
| `AGT-09` | `api/agent/guards.py`, `api/agent/loop.py` | CT-4, CT-6, CT-7, CT-39, CT-45, CT-47 |
| `AGT-10` | `api/agent/guards.py`, `api/agent/loop.py` | CT-4, CT-6, CT-7, CT-39, CT-45, CT-47 |
| `TRI-01` | `api/triage/schemas.py`, `prompts/triage.md`, `api/triage/routes.py` | CT-13, CT-16, CT-48, CT-49, CT-74 |
| `TRI-02` | `api/llm/anthropic_adapter.py`, `api/triage/service.py`, `api/llm/openai_adapter.py` | CT-1, CT-3, CT-6, CT-9, CT-48, CT-74 |
| `TRI-03` | `api/triage/service.py` | CT-3, CT-6, CT-9, CT-48, CT-74 |
| `TRI-04` | `demos/demo_08_triage.py`, `demos/data/phase08_inputs.jsonl` | CT-12, CT-74 |
| `TRI-05` | `api/triage/schemas.py`, `prompts/triage.md`, `api/triage/routes.py` | CT-13, CT-16, CT-48, CT-49, CT-74 |
| `TRI-06` | `api/triage/schemas.py`, `prompts/triage.md` | CT-13, CT-48 |
| `TRI-07` | `api/triage/service.py`, `api/triage/routes.py` | CT-3, CT-6, CT-9, CT-16, CT-48, CT-49, CT-74 |
| `TRI-08` | `api/triage/schemas.py`, `prompts/triage.md`, `api/triage/service.py` | CT-3, CT-6, CT-9, CT-13, CT-48, CT-74 |
| `STR-01` | `api/llm/anthropic_adapter.py`, `api/streaming/events.py`, `api/agent/loop.py`, `api/chat/service.py`, `api/chat/stream_routes.py`, `web/src/app/core/chat/chat-stream.service.ts`, `web/src/app/core/chat/sse-parser.ts` | CT-6, CT-15, CT-16, CT-20, CT-45, CT-50, CT-51, CT-76 |
| `STR-02` | `web/src/app/features/chat/chat.page.ts`, `web/src/app/features/chat/chat.page.html` | CT-24, CT-51 |
| `STR-03` | `api/agent/loop.py`, `api/chat/service.py`, `web/src/app/features/chat/chat.page.ts`, `web/src/app/features/chat/chat.page.html` | CT-6, CT-15, CT-20, CT-24, CT-45, CT-51 |
| `STR-04` | `api/agent/loop.py`, `api/chat/service.py`, `api/chat/stream_routes.py`, `web/src/app/core/chat/chat-stream.service.ts`, `web/src/app/core/chat/sse-parser.ts`, `web/src/app/features/chat/chat.page.ts`, `web/src/app/features/chat/chat.page.html` | CT-6, CT-15, CT-16, CT-20, CT-24, CT-45, CT-50, CT-51, CT-76 |
| `STR-05` | `api/streaming/events.py`, `api/agent/loop.py`, `api/chat/service.py`, `api/chat/stream_routes.py` | CT-6, CT-15, CT-16, CT-20, CT-45, CT-50, CT-76 |
| `STR-06` | `demos/demo_09_streaming.py`, `README.md` | CT-12, CT-76 |
| `STR-07` | `api/agent/loop.py`, `api/chat/service.py` | CT-6, CT-15, CT-20, CT-45 |
| `STR-08` | `web/src/app/core/i18n/i18n.service.ts`, `web/src/app/core/i18n/messages.ts`, `web/src/app/features/chat/chat.page.ts`, `web/src/app/features/chat/chat.page.html` | CT-24, CT-26, CT-51 |
| `STR-09` | `web/src/app/core/i18n/i18n.service.ts`, `web/src/app/core/i18n/messages.ts`, `web/src/app/features/login/login.page.ts`, `web/src/app/features/login/login.page.html` | CT-24, CT-25 |
| `GRD-01` | `api/guardrails/input_guard.py`, `api/chat/service.py`, `api/agent/loop.py` | CT-15, CT-39, CT-45, CT-52, CT-53, CT-54, CT-55, CT-75 |
| `GRD-02` | `api/chat/prompt_builder.py` | CT-31 |
| `GRD-03` | `ingest/pipeline.py` | CT-28, CT-52 |
| `GRD-04` | `api/guardrails/output_guard.py`, `prompts/topic_check.md`, `api/guardrails/stream_filter.py`, `api/chat/service.py`, `api/agent/loop.py` | CT-6, CT-13, CT-15, CT-39, CT-45, CT-52, CT-54, CT-55, CT-75 |
| `GRD-05` | `demos/demo_10_guardrails.py`, `evals/datasets/redteam.jsonl` | CT-10, CT-12, CT-15 |
| `GRD-06` | `api/guardrails/input_guard.py`, `api/chat/service.py`, `api/agent/loop.py` | CT-15, CT-39, CT-45, CT-52, CT-53, CT-54, CT-55, CT-75 |
| `GRD-07` | `api/guardrails/pii.py`, `api/chat/service.py`, `api/agent/loop.py` | CT-15, CT-39, CT-45, CT-53, CT-54, CT-55, CT-75 |
| `GRD-08` | `api/guardrails/output_guard.py`, `prompts/topic_check.md`, `api/guardrails/stream_filter.py`, `api/chat/service.py`, `api/agent/loop.py` | CT-6, CT-13, CT-15, CT-39, CT-45, CT-52, CT-54, CT-55, CT-75 |
| `GRD-09` | `api/guardrails/patterns.py`, `demos/demo_10_guardrails.py`, `evals/datasets/redteam.jsonl` | CT-10, CT-12, CT-15, CT-52 |
| `CACHE-01` | `api/chat/prompt_builder.py`, `api/llm/anthropic_adapter.py`, `api/llm/pricing.py` | CT-6, CT-7, CT-31 |
| `CACHE-02` | `api/chat/prompt_builder.py` | CT-31 |
| `CACHE-03` | `api/llm/pricing.py`, `config/pricing.json`, `api/llm/anthropic_adapter.py` | CT-1, CT-6, CT-7 |
| `CACHE-04` | `demos/demo_11_prompt_cache.py` | CT-12, CT-15 |
| `RES-01` | `api/llm/retry.py` | CT-6, CT-56 |
| `RES-02` | `api/llm/retry.py` | CT-6, CT-56 |
| `RES-03` | `api/llm/retry.py` | CT-6, CT-56 |
| `RES-04` | `api/llm/circuit_breaker.py` | CT-6, CT-57 |
| `RES-05` | `api/llm/openai_adapter.py`, `api/llm/fallback.py`, `api/llm/factory.py`, `api/chat/service.py` | CT-1, CT-6, CT-9, CT-15, CT-56, CT-57, CT-58, CT-59, CT-61 |
| `RES-06` | `api/tickets/models.py`, `api/db/migrations/versions/0009_tickets.py`, `api/tickets/service.py`, `api/llm/factory.py`, `api/chat/service.py` | CT-5, CT-9, CT-15, CT-56, CT-57, CT-58, CT-59, CT-60, CT-61 |
| `RES-07` | `api/llm/chaos.py` | CT-1, CT-6, CT-58 |
| `RES-08` | `demos/demo_12_resilience.py` | CT-12, CT-15 |
| `EVAL-01` | `evals/datasets/core_en.jsonl`, `evals/datasets/core_pt.jsonl`, `evals/targets.py` | CT-10, CT-15, CT-74 |
| `EVAL-02` | `evals/judge.py`, `prompts/judge_faithfulness.md`, `evals/graders.py`, `evals/targets.py` | CT-6, CT-10, CT-13, CT-15, CT-33, CT-74 |
| `EVAL-03` | `evals/report.py`, `.github/workflows/evals.yml` | CT-10 |
| `EVAL-04` | `.github/workflows/evals.yml` | — |
| `EVAL-05` | `evals/gate.py`, `.github/workflows/evals.yml` | CT-10 |
| `EVAL-06` | `demos/demo_13_eval_gate.py` | CT-10, CT-12 |
| `EVAL-07` | `evals/gate.py` | CT-10 |
| `EVAL-08` | `evals/datasets/core_en.jsonl`, `evals/datasets/core_pt.jsonl` | CT-10 |
| `OBS-01` | `api/observability/tracing.py`, `api/llm/instrumented.py`, `api/rag/retriever.py` | CT-1, CT-2, CT-62 |
| `OBS-02` | `api/observability/langfuse_api.py`, `api/feedback/models.py`, `api/db/migrations/versions/0010_feedback.py`, `api/feedback/routes.py`, `web/src/app/features/chat/message-meta.component.ts`, `web/src/app/core/chat/feedback-api.service.ts` | CT-1, CT-5, CT-16, CT-19, CT-24, CT-63, CT-64, CT-65 |
| `OBS-03` | `docker-compose.yml`, `docs/observability.md` | — |
| `OBS-05` | `demos/demo_14_trace_to_eval.py` | CT-10, CT-12, CT-15 |
| `OBS-06` | `web/src/app/features/chat/message-meta.component.ts`, `web/src/app/core/chat/feedback-api.service.ts` | CT-24, CT-65 |
| `OBS-07` | `docker-compose.yml`, `docs/observability.md` | — |
| `DEMO-01` | `api/auth/service.py`, `api/auth/deps.py`, `api/auth/routes.py`, `api/chat/routes.py`, `web/src/app/core/auth/auth.service.ts`, `web/src/app/core/auth/auth.guard.ts`, `web/src/app/features/login/login.page.ts`, `web/src/app/features/login/login.page.html` | CT-1, CT-3, CT-14, CT-16, CT-17, CT-18, CT-24, CT-25 |
| `DEMO-02` | `api/limits/models.py`, `api/db/migrations/versions/0011_usage_and_maintenance.py`, `api/limits/rate_limit.py`, `api/limits/deps.py` | CT-1, CT-3, CT-4, CT-5, CT-66, CT-68, CT-69, CT-70 |
| `DEMO-03` | `api/limits/models.py`, `api/db/migrations/versions/0011_usage_and_maintenance.py`, `api/limits/ledger.py`, `api/limits/spend_cap.py`, `api/limits/deps.py` | CT-1, CT-3, CT-4, CT-5, CT-7, CT-66, CT-67, CT-68, CT-69, CT-70 |
| `DEMO-04` | `render.yaml`, `README.md` | — |
| `DEMO-05` | `pyproject.toml`, `api/config.py`, `api/llm/chaos.py`, `render.yaml` | CT-1, CT-6, CT-58 |
| `DEMO-06` | `api/auth/models.py`, `api/db/migrations/versions/0002_customers_sessions.py`, `api/seed/personas.py`, `api/seed/__main__.py`, `web/src/app/features/login/login.page.ts`, `web/src/app/features/login/login.page.html`, `api/seed/commerce.py`, `README.md` | CT-5, CT-16, CT-18, CT-23, CT-24, CT-25, CT-38 |
| `DEMO-07` | `api/maintenance/restore.py`, `api/maintenance/scheduler.py` | CT-20, CT-23, CT-71, CT-72, CT-73 |
| `DEMO-08` | `pyproject.toml`, `api/config.py`, `api/limits/spend_cap.py`, `api/limits/rate_limit.py` | CT-1, CT-66, CT-68, CT-69 |
| `DEMO-09` | `api/limits/spend_cap.py`, `api/limits/rate_limit.py`, `api/limits/deps.py` | CT-1, CT-3, CT-4, CT-66, CT-68, CT-69, CT-70 |
| `DEMO-10` | `api/observability/langfuse_api.py`, `api/maintenance/purge.py`, `api/maintenance/scheduler.py` | CT-1, CT-5, CT-63, CT-71, CT-72, CT-73 |
| `DEMO-11` | `web/src/app/core/i18n/i18n.service.ts`, `web/src/app/core/i18n/messages.ts`, `web/src/app/features/login/login.page.ts`, `web/src/app/features/login/login.page.html` | CT-24, CT-25 |
| `DEMO-12` | `api/maintenance/restore.py`, `api/maintenance/purge.py` | CT-5, CT-20, CT-23, CT-63, CT-71, CT-72 |
| `RAG-12` | `api/rag/hybrid.py` | CT-29, CT-30, CT-35, CT-36 |
| `RAG-13` | `demos/demo_05_hnsw.py` | CT-12, CT-35, CT-36, CT-37 |
| `FND-90` | `pyproject.toml`, `api/config.py`, `api/observability/logging.py`, `api/observability/middleware.py`, `api/llm/anthropic_adapter.py`, `api/llm/instrumented.py`, `api/llm/factory.py` | CT-1, CT-2, CT-6, CT-7, CT-8, CT-9 |
| `CHAT-90` | `api/chat/service.py`, `api/chat/schemas.py`, `api/chat/routes.py`, `web/src/app/features/chat/chat.page.ts`, `web/src/app/features/chat/chat.page.html` | CT-1, CT-3, CT-4, CT-6, CT-9, CT-13, CT-14, CT-15, CT-24, CT-26 |
| `CHAT-91` | `api/chat/service.py`, `api/chat/schemas.py`, `api/chat/routes.py`, `web/src/app/features/chat/chat.page.ts`, `web/src/app/features/chat/chat.page.html` | CT-1, CT-3, CT-4, CT-6, CT-9, CT-13, CT-14, CT-15, CT-24, CT-26 |
| `CHAT-92` | `api/errors.py`, `api/messages.py`, `api/chat/service.py`, `api/chat/schemas.py`, `api/chat/routes.py` | CT-1, CT-2, CT-3, CT-4, CT-6, CT-9, CT-13, CT-14, CT-15 |
| `CHAT-93` | `api/chat/models.py`, `api/db/migrations/versions/0003_conversations_messages.py`, `api/chat/repository.py`, `api/chat/service.py` | CT-3, CT-4, CT-5, CT-15, CT-16, CT-18, CT-19, CT-20, CT-21, CT-22 |
| `CHAT-94` | `api/chat/repository.py`, `api/chat/routes.py` | CT-3, CT-14, CT-16, CT-19, CT-20 |
| `CHAT-95` | `api/errors.py`, `api/messages.py`, `api/chat/language.py`, `api/chat/service.py` | CT-2, CT-3, CT-4, CT-15, CT-16, CT-20, CT-21, CT-22 |
| `RAG-90` | `api/rag/retriever.py`, `api/chat/service.py` | CT-1, CT-4, CT-15, CT-29, CT-30, CT-31, CT-32 |
| `RAG-91` | `ingest/parsers.py`, `ingest/chunking.py`, `ingest/pipeline.py`, `ingest/report.py` | CT-5, CT-8, CT-27, CT-28 |
| `RAG-92` | `ingest/pipeline.py`, `ingest/report.py` | CT-5, CT-27, CT-28 |
| `RAG-93` | `api/rag/embeddings.py`, `ingest/pipeline.py` | CT-1, CT-28, CT-34 |
| `RAG-94` | `api/rag/retriever.py`, `api/chat/service.py`, `api/rag/embeddings.py` | CT-1, CT-4, CT-15, CT-29, CT-30, CT-31, CT-32, CT-34 |
| `TOOL-90` | `api/tools/orders.py`, `api/tools/refunds.py` | CT-38, CT-39, CT-40, CT-41, CT-42 |
| `TOOL-91` | `api/tools/orders.py` | CT-38, CT-39, CT-40, CT-42 |
| `TOOL-92` | `api/tools/orders.py` | CT-38, CT-39, CT-40, CT-42 |
| `TOOL-93` | `api/tools/models.py`, `api/db/migrations/versions/0007_commerce.py`, `api/tools/refunds.py` | CT-5, CT-18, CT-38, CT-39, CT-40, CT-41, CT-42 |
| `TOOL-94` | `api/tools/registry.py` | CT-6, CT-40 |
| `TOOL-95` | `api/auth/models.py`, `api/db/migrations/versions/0002_customers_sessions.py`, `api/auth/service.py`, `api/auth/deps.py`, `api/auth/routes.py`, `api/chat/routes.py` | CT-1, CT-3, CT-5, CT-14, CT-16, CT-17, CT-18 |
| `TOOL-96` | `api/agent/models.py`, `api/db/migrations/versions/0008_pending_confirmations.py`, `api/agent/confirmations.py`, `api/chat/service.py` | CT-3, CT-5, CT-15, CT-20, CT-43, CT-44, CT-45 |
| `TOOL-97` | `api/agent/confirmations.py`, `api/chat/service.py` | CT-3, CT-15, CT-20, CT-43, CT-44, CT-45 |
| `TOOL-98` | `api/seed/commerce.py`, `api/tools/refund_policy.py` | CT-23, CT-38, CT-41 |
| `TOOL-99` | `api/tools/orders.py` | CT-38, CT-39, CT-40, CT-42 |
| `AGT-90` | `api/agent/loop.py`, `api/agent/events.py` | CT-6, CT-31, CT-39, CT-40, CT-44, CT-45 |
| `AGT-91` | `api/tools/registry.py`, `api/agent/loop.py`, `api/agent/events.py` | CT-6, CT-31, CT-39, CT-40, CT-44, CT-45 |
| `TRI-90` | `api/triage/schemas.py`, `prompts/triage.md`, `api/triage/routes.py` | CT-13, CT-16, CT-48, CT-49, CT-74 |
| `STR-90` | `api/streaming/events.py`, `api/chat/stream_routes.py`, `web/src/app/core/chat/chat-stream.service.ts`, `web/src/app/core/chat/sse-parser.ts`, `web/src/app/features/chat/chat.page.ts`, `web/src/app/features/chat/chat.page.html` | CT-15, CT-16, CT-24, CT-45, CT-50, CT-51, CT-76 |
| `STR-91` | `api/agent/loop.py`, `api/chat/service.py`, `api/chat/stream_routes.py` | CT-6, CT-15, CT-16, CT-20, CT-45, CT-50, CT-76 |
| `STR-92` | `web/src/app/features/chat/chat.page.ts`, `web/src/app/features/chat/chat.page.html` | CT-24, CT-51 |
| `GRD-90` | `api/guardrails/patterns.py`, `api/guardrails/input_guard.py`, `demos/demo_10_guardrails.py`, `evals/datasets/redteam.jsonl` | CT-10, CT-12, CT-15, CT-52, CT-53, CT-54 |
| `GRD-91` | `ingest/pipeline.py`, `api/chat/service.py`, `api/agent/loop.py`, `demos/demo_10_guardrails.py`, `evals/datasets/redteam.jsonl` | CT-10, CT-12, CT-15, CT-28, CT-39, CT-45, CT-52, CT-54, CT-55, CT-75 |
| `GRD-93` | `api/guardrails/input_guard.py`, `api/guardrails/output_guard.py`, `prompts/topic_check.md`, `api/chat/service.py`, `api/agent/loop.py` | CT-6, CT-13, CT-15, CT-39, CT-45, CT-52, CT-53, CT-54, CT-55, CT-75 |
| `CACHE-90` | `api/llm/anthropic_adapter.py`, `api/llm/pricing.py`, `api/llm/openai_adapter.py` | CT-1, CT-6, CT-7 |
| `RES-91` | `api/llm/fallback.py`, `api/llm/factory.py`, `api/chat/service.py` | CT-6, CT-9, CT-15, CT-56, CT-57, CT-58, CT-59, CT-61 |
| `RES-92` | `api/llm/fallback.py`, `api/llm/factory.py`, `api/chat/service.py` | CT-6, CT-9, CT-15, CT-56, CT-57, CT-58, CT-59, CT-61 |
| `RES-93` | `api/llm/fallback.py`, `api/llm/factory.py`, `api/chat/service.py` | CT-6, CT-9, CT-15, CT-56, CT-57, CT-58, CT-59, CT-61 |
| `EVAL-90` | `evals/gate.py`, `.github/workflows/evals.yml` | CT-10 |
| `EVAL-91` | `evals/gate.py` | CT-10 |
| `EVAL-92` | `evals/gate.py` | CT-10 |
| `OBS-91` | `api/guardrails/pii.py`, `api/chat/service.py`, `api/agent/loop.py`, `api/observability/tracing.py` | CT-1, CT-2, CT-15, CT-39, CT-45, CT-53, CT-54, CT-55, CT-62, CT-75 |
| `OBS-92` | `api/feedback/routes.py` | CT-16, CT-63, CT-64, CT-65 |
| `OBS-93` | `api/observability/tracing.py` | CT-1, CT-2, CT-62 |
| `DEMO-91` | `api/tools/orders.py`, `api/tools/refunds.py`, `api/maintenance/restore.py` | CT-20, CT-23, CT-38, CT-39, CT-40, CT-41, CT-42, CT-71 |

Cobertura: 173/173 requisitos ativos. Os aposentados (`GRD-92`, `RES-90`, `OBS-04`, `OBS-90`, `DEMO-90`) não entram.

## 11. Dependências Externas

**Python (pyproject, TASK-001; acréscimos por wiring indicados):** `fastapi`, `uvicorn[standard]`, `pydantic>=2.8`,
`pydantic-settings`, `sqlalchemy[asyncio]>=2.0`, `psycopg[binary]>=3.2`, `alembic`, `pgvector` (tipo SQLAlchemy),
`anthropic`, `openai` (LLM secundário e embeddings), `httpx`, `tiktoken` (DA-9), `langdetect` (DA-9), `numpy`
(força bruta, fase 4), `pypdf` (parser de PDF; é o "framework como utilitário" que o documento-fonte permite),
`argon2-cffi` (DA-10), `pyyaml` (manifesto do corpus), `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`
(DA-25). Grupo `dev`: `pytest`, `pytest-asyncio`, `ruff`, `mypy`, `types-PyYAML`, `matplotlib` (gráfico do demo_02),
`reportlab` (PDFs do corpus). Todas já usadas pelo autor em projetos irmãos ou exigidas pelo documento-fonte. Nenhuma
é framework de agente.

**Node (web/package.json, TASK-012):** `@angular/*` 22, `@angular/build`, `vitest`, `angular-eslint`, `eslint`,
`typescript-eslint`, `@playwright/test`. Sem biblioteca de UI nem de i18n (DA-7).

**Serviços de terceiros:** Anthropic API (primário), OpenAI API (secundário + embeddings), Langfuse (auto-hospedado
local; Langfuse Cloud na demo, LAC-36), Render (PaaS, P-8), GitHub Actions (CI, `gh` CLI do runner).

**Segredos (nunca em código nem em log, FND-90):** `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY`, `ATLAS_OPERATOR_API_KEY`. No CI: `secrets.*` do repositório. No Render: `sync: false`.

**Infra:** Docker Compose (local), Postgres 16 com pgvector (`pgvector/pgvector:pg16`; Render Postgres com extensão
`vector`), perfil `observability` (Langfuse web/worker + ClickHouse + Redis + MinIO + Postgres próprio).

## 12. Áreas Sensíveis

Classificação por eixo via Jev (um `noul` por eixo, com os arquivos como evidência).

| ID | Eixo | Situação | Componentes envolvidos |
|---|---|---|---|
| AS-1 | Autenticação / autorização / sessão | SIM: sessão por cookie, acesso persona/operador, identidade das tools só da sessão, escopo por cliente em conversas, tools, confirmações, triagem e feedback (Jev p=0.99) | `api/auth/service.py`, `api/auth/deps.py`, `api/auth/routes.py`, `api/chat/repository.py`, `api/tools/registry.py`, `api/tools/orders.py`, `api/tools/refunds.py`, `api/tools/subscriptions.py`, `api/tools/email.py`, `api/agent/confirmations.py`, `api/agent/loop.py`, `api/triage/routes.py`, `api/feedback/routes.py` |
| AS-2 | Pagamento / faturamento / cálculo financeiro | SIM: custo real da API de LLM (estimativa, registro, teto diário, limite por requisição). Os reembolsos são fictícios e NÃO entram aqui (Jev p=0.98) | `api/llm/pricing.py`, `api/limits/ledger.py`, `api/limits/spend_cap.py`, `api/limits/deps.py`, `api/agent/loop.py` |
| AS-3 | Dados pessoais ou sensíveis (PII, saúde, financeiro) | SIM: visitantes podem digitar PII real; mascaramento antes do LLM e da persistência, logs e traces mascarados, conversas apagadas (Jev p=0.90) | `api/guardrails/pii.py`, `api/guardrails/input_guard.py`, `api/guardrails/output_guard.py`, `api/guardrails/stream_filter.py`, `api/chat/service.py`, `api/observability/logging.py`, `api/observability/tracing.py`, `api/maintenance/restore.py`, `api/maintenance/purge.py` |
| AS-4 | Migration com dados existentes em produção | NÃO: greenfield; todas as migrations criam estrutura nova antes de a demo existir (Jev p=0.03) | — |
| AS-5 | Lógica regulatória / fiscal / compliance | SIM: retenção e expurgo de 7 dias (LGPD) e exclusão de traces no Langfuse (Jev p=0.98) | `api/maintenance/purge.py`, `api/maintenance/restore.py`, `api/maintenance/scheduler.py`, `api/observability/langfuse_api.py` |
| AS-6 | Endpoint público sem autenticação | NÃO: só `GET /health` e `POST /auth/login` são públicos, e nenhum dos dois acessa dados de cliente nem gera custo de LLM (Jev p=0.21) | — |
| AS-7 | Criptografia / manuseio de chaves e segredos | SIM: chaves de API via ambiente sem log, hash argon2, token de sessão com hash SHA-256, chave de operador com comparação em tempo constante (Jev p=0.99) | `api/config.py`, `api/auth/service.py`, `api/auth/deps.py`, `api/observability/logging.py` |
| AS-8 | Integração externa nova com terceiro | SIM: Anthropic, OpenAI (LLM + embeddings), Langfuse (OTLP + API), Render (Jev p=0.97) | `api/llm/anthropic_adapter.py`, `api/llm/openai_adapter.py`, `api/rag/embeddings.py`, `api/observability/tracing.py`, `api/observability/langfuse_api.py`, `render.yaml` |

## 13. Migração e Rollback

Há mudança de schema (11 migrations novas) e publicação de contratos HTTP novos, então esta seção se aplica, mas sem
dado legado.

- **Script de ida**: `uv run alembic upgrade head`, executado pelo `docker/api-entrypoint.sh` no start do container
  `api` (local e Render). Cada migration cria só tabelas, colunas anuláveis ou índices novos (0001..0011, seção 7.2).
- **Script de volta**: `uv run alembic downgrade -1` por migration (todas com `downgrade` simétrico, testado na task que
  a cria: `upgrade` → `downgrade` sem erro). Rollback de aplicação no Render = redeploy do commit/tag anterior seguido
  de `alembic downgrade <revisão>`, se a migration nova não for compatível com o código antigo.
- **Compatibilidade**: o código antigo continua funcionando com o schema novo, porque as migrations só acrescentam. A
  coluna `chunks.embedding` é anulável, e o índice HNSW (0006) e as tabelas novas são ignorados pelo código anterior.
  Os contratos HTTP são publicados pela primeira vez, sem cliente legado. O frontend e a API sobem juntos no mesmo deploy.
- **Backfill**: não há. Embeddings e índice HNSW são gerados pela reingestão (`python -m ingest run` e
  `python -m ingest reindex-hnsw`), que é idempotente (RAG-92) e leva segundos para ~40 arquivos. Seed de personas
  e comércio idempotente (`python -m api.seed --commerce`).
- **Janela**: não exige janela de manutenção. Na demo, migrações rodam no start do deploy, e a indisponibilidade é a do
  restart do serviço.
- **Dados da demo**: restauração diária e expurgo de 7 dias são operações de dado em produção (AS-5), cobertas pelas
  TASK-146/147 (`crítico`) e auditáveis por `maintenance_runs`.

## 14. Observabilidade

- **Logar** (JSON, sempre com `trace_id`): `http_request` (método, rota, status, latência); `llm_call` (provider, model,
  purpose, context_tokens, input/output/cache tokens, cost_usd, latency_ms, outcome); `llm_fallback` (de, para,
  motivo); `retrieval` (modo, k, ids e scores); `tool_executed` (tool, outcome, ids de pedido); `agent_limit`
  (motivo); `agent_repeat_detected`; `guardrail_block` (camada, motivo sem o padrão casado); `triage_failed`;
  `confirmation_resolved`; `rate_limited`/`daily_cap_reached`; `maintenance_run` (job, contagens);
  `telemetry_dropped`; `ingest_document` (doc_key, status).
- **NÃO logar**: valor de nenhuma chave/segredo (filtro de redação, TASK-002); senha, token ou cookie de sessão;
  `X-Operator-Key`; texto de mensagem antes do mascaramento (só o texto mascarado pode aparecer em span e só a partir
  da fase 10); e-mail completo de visitante digitado no chat (já mascarado); corpo de requisições de auth.
- **Métricas / alertas** (no Langfuse, OBS-03, `docs/observability.md`): custo por conversa (`langfuse.session.id`),
  latência p95 por trace, taxa de fallback (traces com span `fallback` / total), taxa de bloqueio por guardrail,
  gasto diário vs teto. Alerta operacional: log `daily_cap_reached` e `telemetry_dropped` repetido.
- **Auditoria**: `audit_log` (toda tool com efeito: cliente, tool, argumentos, resultado, outcome, trace_id, data),
  `email_outbox`, `tickets`, `feedback` e `maintenance_runs`. Retenção de 7 dias na demo (DEMO-10), com expurgo
  também no Langfuse (LAC-36). A restauração diária não apaga auditoria (DEMO-12). Localmente, a retenção só se aplica
  se `ATLAS_MAINTENANCE_ENABLED=true`.

## 15. Riscos e Mitigações

| Risco | Probabilidade | Impacto | Mitigação |
|---|---|---|---|
| R-1: os comandos da seção 3 só passam a existir após as TASK-001/012; o baseline do `run.py init` sai todo "cego" (G4) e as primeiras ondas exigem QA semântico | alta | médio | TASK-001 e TASK-012 entram nas primeiras ondas; o `Done when` delas executa cada comando. Ver C4 na autoverificação |
| R-2: testes de integração exigem `docker compose up -d db`; sem ele, a suíte falha por ambiente | média | médio | Pré-requisito explícito na seção 3 e no CI (serviço Postgres); conftest com mensagem clara se o banco não responde |
| R-3: escopo grande (150 tasks) para 8–10 h/semana até 31/01/2027 | alta | alto | Barreira por fase permite publicar fase a fase; P1 (fases 0–5) forma o MVP; métricas reais ficam para o Operador rodar uma vez por fase |
| R-4: o mecanismo exato de saída estruturada nativa da Anthropic depende da versão do SDK fixada (incerto) | média | médio | Adapter usa o recurso nativo se o SDK fixado o expõe; senão, tool forçada com `input_schema` (mecanismo estável). Teste captura o payload. A ADR 0008 registra a escolha |
| R-5: Langfuse auto-hospedado pesado para máquina local | média | baixo | Fica em perfil opcional `observability`; `docker compose up` padrão não o sobe |
| R-6: endpoints da API pública do Langfuse para score e exclusão de traces podem mudar entre versões (não confirmados neste repo) | média | médio | Isolados em `api/observability/langfuse_api.py` (CT-63) com testes de contrato via MockTransport; a TASK-134 confirma na documentação da versão em uso |
| R-7: custo real das demos (1M vetores não usa LLM; 200 triagens, 100 conversas, red-team e evals usam) | média | médio | `--n` configurável, `--provider fake` no CI, teto diário também aplicado aos demos via ledger |
| R-8: guardrails por regex geram falso positivo/negativo | alta | médio | Métrica de falso positivo publicada (GRD-05) com conjunto de controle; padrões ancorados; fail-closed só em erro do verificador |
| R-9: buffer por frase aumenta o TTFT medido no demo_09 | média | baixo | Demo_09 mede com e sem guardrails; a ADR 0010 documenta o trade-off |
| R-10: tag `phase-NN` exige ação manual do Operador após o merge da onda de fechamento | alta | médio | Seção 16 descreve o procedimento; `scripts/check_phase.py NN --require-tag` valida |
| R-11: o bloqueio de merge do gate de evals depende de configurar o job como required check no GitHub (fora do código) | alta | alto | Passo do Operador na seção 16; o workflow já falha com exit 1 |
| R-12: Angular 22 + `@angular/build:unit-test` com Vitest é recente; flags conferidas só no schema local de outro projeto | baixa | médio | TASK-012 executa os comandos no `Done when`; o padrão já roda em `~/Projects/complexity-patters` |
| R-13: falhas conhecidas pré-existentes | — | — | Nenhuma: repositório sem código nem CI |

Nenhuma stack nova fora do documento-fonte ou do histórico do autor. Não há item "requer aprovação".

## 16. Critérios de Aceite Técnicos

- [ ] `uv run pytest -q` (com `docker compose up -d db`) e `npx ng test --watch=false` (em `web`) verdes, com
      `reports/junit.xml` gerado nos dois diretórios.
- [ ] `uv run ruff check . --output-format=concise`, `uv run mypy`, `npx ng lint` e `npx tsc --noEmit -p tsconfig.app.json` sem erros.
- [ ] `uv build` e `npx ng build` terminam com exit 0.
- [ ] `docker compose up -d --build` num clone limpo com `.env` copiado de `.env.example` → `GET http://localhost:8000/health` 200 e `http://localhost:8080` serve a tela de login.
- [ ] `uv run python scripts/check_phase.py NN` retorna 0 para NN = 01..14.
- [ ] Workflow `ci.yml` verde num PR; `evals.yml` roda só quando o PR toca `prompts/**` ou `api/**`.
- [ ] Os 173 requisitos ativos cobertos por ≥ 1 task (`check_plan.py`).

**Passos do Operador (fora das tasks, não automatizáveis pelo pipeline):**
1. Após o merge da onda que contém a task "Phase NN ADR and eval cases": rodar `python -m demos.demo_NN_* --provider real`,
   commitar a linha de `docs/results.md`, rodar `scripts/check_phase.py NN` e criar a tag
   `git tag phase-NN <sha> && git push origin phase-NN`.
2. Configurar `secrets` do GitHub (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, modelos) e marcar o job do `evals.yml` como
   required check da branch padrão (R-11). Documentar no `docs/results.md` o PR real de regressão (EVAL-06).
3. Criar o blueprint no Render a partir de `render.yaml`, preencher os segredos (`sync: false`) e o projeto do
   Langfuse Cloud (LAC-36), e gravar o GIF de ~30 s para o README.

## Autoverificação do plano

Itens semânticos sob responsabilidade do plan-architect (os mecânicos ficam com o `check_plan.py`):

- [x] **B3** — Toda decisão de `decisions.md` (LAC-01..17, 20..41) aparece na seção 2.1 com a consequência concreta; LAC-35..37 aplicadas em CT-16/CT-70 (triagem), seção 7.3/11/14 e DA-18 (Langfuse), `corpus/manifest.yaml` (corpus).
- [x] **B4** — LAC-18 e LAC-19 e as premissas próprias P-1..P-11 estão na seção 2.2 com reversibilidade e impacto.
- [ ] **C1** — RESSALVA: nenhum comando da seção 3 pode ser confirmado num arquivo do repo, porque ele é greenfield. Cada comando está declarado com a origem (arquivo + task que o cria) e segue configuração lida em projetos irmãos do autor. As flags do `ng test` foram conferidas no schema do builder 22.1.4. O `Done when` das TASK-001/004/012/013 executa os comandos.
- [x] **C4** — Typecheck, suíte e build têm comando (nenhum `N/A`). Destaque mesmo assim: até a TASK-001 (Python) e a TASK-012 (web) entrarem, esses alvos não existem no baseline, então as primeiras ondas rodam com gate cego (G4) e QA semântico obrigatório (R-1).
- [x] **D2** — Todo CT consumido tem uma task produtora (validado pelo gerador: 76 contratos, 1 produtor cada, e todo consumidor depende transitivamente do produtor). Nenhum contrato vem do codebase (greenfield).
- [x] **I5** — Seção 6 não vazia: padrões externos do autor + 16 componentes internos de reuso obrigatório.
- [x] **I6** — Seção 13 preenchida (ida, volta, compatibilidade, backfill, janela).
- [x] **I7** — Seção 14 preenchida (logar, não logar, métricas, auditoria) para AS-1/2/3/5/7/8.
- [x] **I8** — Diagrama de sequência do fluxo principal na seção 5.2.
- [x] Toda task paralela previsível tem CT-n (76 contratos, seção 8.2).
- [x] Toda área sensível SIM lista arquivos concretos que constam da seção 9.
- [x] Todo arquivo novo das tasks aparece na seção 9 (produção em 9.1; wiring/suporte em 9.2).
- [x] Nenhuma stack/framework novo fora do documento-fonte e do histórico do autor.
- [x] Nenhum código de implementação: só assinaturas, contratos, schemas e comandos.
