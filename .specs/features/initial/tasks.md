# Tarefas — Atlas: Agente de Suporte com IA (projeto completo, fases 0–14 + demo pública)

> Gerado pelo plan-architect a partir de `plan.md`. Ondas, caminho crítico e tabelas de validação: `check_plan.py` (`validacao.md`).
> Cada fase termina numa task "Phase NN ADR and eval cases"; toda task da fase seguinte depende dela (barreira de fase, DA-28), para que a tag `phase-NN` aponte para um commit sem código da fase seguinte.
> Pré-requisito dos testes `integration`: `docker compose up -d db` (seção 3 do plan).

## Fase 0 — Fundação

### TASK-001 — Bootstrap Python project and typed settings

- **Requisito**: `FND-90`, `CHAT-08`, `CHAT-09`, `DEMO-08`, `DEMO-05`
- **Tipo**: config
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: —
- **Arquivos de produção**:
  - `pyproject.toml`
  - `api/config.py`
- **Arquivos de teste**:
  - `tests/unit/api/test_config.py`
- **Wiring permitido**:
  - `.gitignore` (criar: `.venv/`, `reports/`, `__pycache__/`, `.env`, `web/node_modules/`, `web/dist/`, `.angular/`)
  - `.env.example` (criar com todas as variáveis da seção 7.3 do plan, sem valores secretos)
  - `api/__init__.py` (criar vazio)
  - `uv.lock` (gerado por `uv lock`)
- **Reusa**: —
- **Contrato**:
  - CT-1 — `api.config.Settings` (pydantic-settings, campos da seção 7.3) + `get_settings() -> Settings` (cacheado) + `MissingConfigError(var_name: str)` (produz)
- **Testes**: unit
- **Descrição**: Cria o projeto Python único da raiz (DA-1, DA-2) com as dependências da seção 11 e as configurações de ruff, mypy e pytest (`addopts = "-q --junitxml=reports/junit.xml"`, `asyncio_mode = "auto"`). Define `Settings` com TODOS os campos da seção 7.3 já nesta task (DA-4): as tasks seguintes só leem configuração. `MissingConfigError` cita só o nome da variável.
- **Done when**:
  - [ ] `uv sync` (executar em `.`) termina com exit 0 e cria `uv.lock`
  - [ ] teste prova os padrões `context_budget_tokens == 8000`, `max_message_chars == 4000`, `rate_limit_per_hour == 20`, `daily_spend_cap_usd == Decimal('2.00')` e `chaos_rate == 0.0`, e que cada um muda via variável de ambiente
  - [ ] teste prova que, com `ATLAS_LLM_PROVIDER=anthropic` e sem `ANTHROPIC_API_KEY`, `get_settings().require_llm_keys()` levanta `MissingConfigError` cuja mensagem contém `ANTHROPIC_API_KEY`
  - [ ] teste prova que `repr(settings)` e `str(settings)` não contêm o valor de nenhuma chave secreta (campos `SecretStr`)
  - [ ] `uv run pytest -q tests/unit/api/test_config.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 6 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não criar o motor de banco nem clientes de LLM (TASK de `api/db/engine.py` e `api/llm/*`)
  - Não espalhar `os.environ` fora de `api/config.py`

---

### TASK-002 — Structured JSON logging with trace id middleware

- **Requisito**: `FND-03`, `FND-90`
- **Tipo**: infra
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-001
- **Arquivos de produção**:
  - `api/observability/logging.py`
  - `api/observability/middleware.py`
- **Arquivos de teste**:
  - `tests/unit/api/observability/test_logging.py`
- **Wiring permitido**:
  - `api/observability/__init__.py` (criar vazio)
- **Reusa**:
  - `api/config.py` → `get_settings()` (nível de log e lista de segredos a redigir)
- **Contrato**:
  - CT-2 — `api.observability.logging`: `configure_logging(settings) -> None`, `get_trace_id() -> str`, `bind_trace_id(trace_id: str) -> contextvars.Token`, `log_event(event: str, **fields: object) -> None`; `api.observability.middleware.TraceIdMiddleware` (gera UUID4 hex, devolve no cabeçalho `X-Trace-Id`) (produz)
  - CT-1 — `api.config.Settings` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Log estruturado em JSON (stdlib `logging` com formatter próprio, DA-11) com `trace_id` vindo de `contextvars`. O middleware ASGI gera um `trace_id` por requisição, devolve `X-Trace-Id` e o injeta em todo log da requisição. Um filtro troca por `***` qualquer valor de segredo configurado.
- **Done when**:
  - [ ] teste prova que dois logs emitidos dentro da mesma requisição (app Starlette de teste) carregam o mesmo `trace_id` e que esse valor é igual ao cabeçalho `X-Trace-Id` da resposta
  - [ ] teste prova que duas requisições recebem `trace_id` diferentes
  - [ ] teste prova que `log_event('x', api_key=<valor de ANTHROPIC_API_KEY>)` grava `***` e não o valor
  - [ ] `uv run pytest -q tests/unit/api/observability/test_logging.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 4 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não integrar OpenTelemetry/Langfuse (fase 14, `api/observability/tracing.py`)

---

### TASK-003 — Error catalog and bilingual user messages

- **Requisito**: `CHAT-92`, `CHAT-95`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-002
- **Arquivos de produção**:
  - `api/errors.py`
  - `api/messages.py`
- **Arquivos de teste**:
  - `tests/unit/api/test_errors.py`
- **Wiring permitido**: —
- **Reusa**:
  - `api/observability/logging.py` → `get_trace_id()` para o campo `trace_id` do corpo de erro
- **Contrato**:
  - CT-3 — `api.errors`: `ErrorCode` (StrEnum, catálogo 8.3), `AtlasError(code: ErrorCode, http_status: int, params: dict[str, object] | None = None)`, `install_error_handlers(app) -> None`; corpo de erro `{"error": {"code": str, "message": str, "trace_id": str}}` (produz)
  - CT-4 — `api.messages.user_message(key: MessageKey, lang: Lang, **params: object) -> str`; `Lang = Literal["pt", "en"]`; `MessageKey` (StrEnum com as chaves da seção 9 do spec) (produz)
  - CT-2 — `api.observability.logging` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Implementa o catálogo da seção 8.3 como `ErrorCode` e os handlers FastAPI que devolvem o envelope padrão, sem stack trace. `api/messages.py` guarda os textos fixos ao Cliente (seção 9 do spec) em pt e en, incluindo bloqueio, resposta degradada, limite do agente, rate limit, teto diário, idioma não suportado (sempre en), base indisponível e validação.
- **Done when**:
  - [ ] teste prova que uma exceção não tratada vira HTTP 500 com corpo `{"error": {"code": "INTERNAL_ERROR", ...}}` sem a palavra `Traceback`
  - [ ] teste prova que `AtlasError(ErrorCode.LLM_UNAVAILABLE, 502)` vira HTTP 502 com o `trace_id` corrente
  - [ ] teste prova que toda `MessageKey` tem texto não vazio em `pt` e em `en`
  - [ ] `uv run pytest -q tests/unit/api/test_errors.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 4 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não criar rotas de negócio
  - Não traduzir mensagens geradas pelo LLM

---

### TASK-004 — Async database engine and pgvector baseline migration

- **Requisito**: `FND-01`
- **Tipo**: infra
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-001
- **Arquivos de produção**:
  - `api/db/engine.py`
  - `api/db/migrations/versions/0001_enable_pgvector.py`
- **Arquivos de teste**:
  - `tests/integration/api/db/test_engine.py`
- **Wiring permitido**:
  - `tests/conftest.py` (criar: fixtures `test_database_url` e `db_session`, banco `atlas_test_<uuid>` por sessão)
  - `alembic.ini` (criar; `script_location = api/db/migrations`)
  - `api/db/__init__.py` (criar vazio)
  - `api/db/migrations/env.py` (boilerplate async do Alembic usando `Base.metadata` e `ATLAS_DATABASE_URL`)
  - `api/db/migrations/script.py.mako` (template padrão do Alembic)
  - `docker-compose.yml` (criar SÓ com o serviço `db`: imagem `pgvector/pgvector:pg16`, porta 5432, healthcheck `pg_isready`)
- **Reusa**: —
- **Contrato**:
  - CT-5 — `api.db.engine`: `Base` (DeclarativeBase), `get_engine() -> AsyncEngine`, `session_scope() -> AsyncIterator[AsyncSession]` (async context manager com commit/rollback), `get_db_session()` (dependência FastAPI) (produz)
  - CT-1 — `api.config.Settings` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: SQLAlchemy 2 async + psycopg 3 (DA-3). A migration 0001 executa `CREATE EXTENSION IF NOT EXISTS vector`. `tests/conftest.py` cria um banco `atlas_test_<uuid>` por sessão de pytest, roda `alembic upgrade head`, oferece a fixture `db_session` e derruba o banco no fim. É isso que deixa os testes de integração paralelo-seguros (seção 4, DA-12).
- **Done when**:
  - [ ] com `docker compose up -d db` rodando, teste de integração prova que `SELECT extname FROM pg_extension WHERE extname='vector'` retorna 1 linha no banco de teste
  - [ ] teste prova que `session_scope()` faz rollback quando o bloco levanta exceção
  - [ ] `uv run alembic upgrade head` seguido de `uv run alembic downgrade base` (executar em `.`) termina com exit 0 contra o banco do compose
  - [ ] `uv run pytest -q tests/integration/api/db/test_engine.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não criar tabelas de domínio (cada fase cria a sua migration)

---

### TASK-005 — LLM client contract and scripted fake adapter

- **Requisito**: `CHAT-01`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-001
- **Arquivos de produção**:
  - `api/llm/client.py`
  - `api/llm/fake.py`
- **Arquivos de teste**:
  - `tests/unit/api/llm/test_fake_client.py`
- **Wiring permitido**:
  - `api/llm/__init__.py` (criar vazio)
- **Reusa**: —
- **Contrato**:
  - CT-6 — `api.llm.client`: `class LLMClient(Protocol)` com `async def complete(self, request: LLMRequest) -> LLMResponse` e `def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]`; dataclasses `SystemBlock(text, cache_breakpoint: bool = False)`, `ChatMessage(role: Literal["user","assistant","tool"], content: str, tool_calls: list[ToolCall] = [], tool_call_id: str | None = None)`, `ToolSpec(name, description, input_schema: dict)`, `ToolCall(id, name, arguments: dict)`, `LLMRequest(system: list[SystemBlock], messages: list[ChatMessage], tools: list[ToolSpec] = [], response_schema: dict | None = None, max_tokens: int = 1024, purpose: str = "chat")`, `Usage(input_tokens, output_tokens, cache_read_tokens=0, cache_write_tokens=0)`, `LLMResponse(text, tool_calls, usage, model, provider, stop_reason)`, `LLMStreamEvent(kind: Literal["text_delta","tool_call","usage","stop"], text="", tool_call=None, usage=None, stop_reason=None)`; exceções `LLMError(message, recoverable: bool, status: int | None)`, `LLMTimeoutError(LLMError)`; `api.llm.fake.FakeLLMClient(script: list[LLMResponse])` (produz)
- **Testes**: unit
- **Descrição**: Define a interface própria `LLMClient` (DA-5), independente de provedor, e os tipos de request/response/stream. `FakeLLMClient` reproduz um roteiro de respostas (texto, tool calls, usage, erros), inclusive em `stream()` quebrando o texto em deltas. É o adaptador de todos os testes e do modo `--provider fake` dos demos.
- **Done when**:
  - [ ] teste prova que `FakeLLMClient` devolve as respostas do roteiro em ordem e registra os `LLMRequest` recebidos
  - [ ] teste prova que `stream()` emite `text_delta`s cuja concatenação é igual ao texto roteirizado e termina com `usage` e `stop`
  - [ ] teste prova que um item de roteiro do tipo `LLMError(recoverable=True)` é levantado na chamada correspondente
  - [ ] `uv run pytest -q tests/unit/api/llm/test_fake_client.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não importar SDKs de provedor neste módulo

---

### TASK-006 — Anthropic adapter with tool calling and structured output

- **Requisito**: `CHAT-01`, `FND-90`, `TRI-02`
- **Tipo**: integração-externa
- **Risco**: alto
- **Âncora de risco**: AS-8 (primeira integração Anthropic — `api/llm/anthropic_adapter.py`) — Jev a_terceiro p=0.91
- **Perfil**: backend
- **Depende de**: TASK-005
- **Arquivos de produção**:
  - `api/llm/anthropic_adapter.py`
- **Arquivos de teste**:
  - `tests/unit/api/llm/test_anthropic_adapter.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-1 — `api.config.Settings` (assinatura completa: plan 8.2) (consome)
  - CT-6 — `api.llm.client` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Adapter do provedor primário sobre o SDK `anthropic` (cliente async, `max_retries=0`: o retry é nosso, DA-14). Converte `LLMRequest` (system, mensagens, tools, `response_schema`) para a API Messages e normaliza a resposta, o uso de tokens e os erros (429/5xx/timeout = recuperáveis). `response_schema` usa o recurso nativo de saída estruturada do SDK fixado; se a versão fixada não o tiver, usa tool forçada (`tool_choice` de tipo `tool`). Ver risco R-4.
- **Done when**:
  - [ ] teste com `httpx.MockTransport` injetado no SDK prova a conversão de uma resposta com `tool_use` em `LLMResponse.tool_calls`
  - [ ] teste prova que HTTP 429 e 529/500 viram `LLMError(recoverable=True)` e HTTP 400/401 viram `LLMError(recoverable=False)`
  - [ ] teste prova que `response_schema` produz no request o mecanismo de schema obrigatório (payload capturado no MockTransport)
  - [ ] teste prova que construir o adapter sem `ANTHROPIC_API_KEY` levanta `MissingConfigError` citando o nome da variável
  - [ ] nenhum teste faz chamada de rede real (o MockTransport falha em qualquer host não mapeado)
  - [ ] `uv run pytest -q tests/unit/api/llm/test_anthropic_adapter.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 5 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não implementar `stream()` real (fase 9), deixar `NotImplementedError` explícito
  - Não implementar cache_control (fase 11)

---

### TASK-007 — Model pricing table and cost estimation

- **Requisito**: `FND-04`, `CACHE-03`
- **Tipo**: lógica-negócio
- **Risco**: crítico
- **Âncora de risco**: AS-2 (custo real da API de LLM — `api/llm/pricing.py`) — Jev c_dinheiro p=0.72
- **Perfil**: backend
- **Depende de**: TASK-005
- **Arquivos de produção**:
  - `api/llm/pricing.py`
  - `config/pricing.json`
- **Arquivos de teste**:
  - `tests/unit/api/llm/test_pricing.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-7 — `api.llm.pricing.estimate_cost(model: str, usage: Usage) -> Decimal` (USD; preços por milhão de tokens em `config/pricing.json`: `input`, `output`, `cache_read`, `cache_write`); `UnknownModelPricing` (produz)
  - CT-6 — `api.llm.client` (assinatura completa: plan 8.2) (consome)
  - CT-1 — `api.config.Settings` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Estimativa de custo em USD (Decimal) por chamada, a partir de `config/pricing.json` (preço por milhão de tokens de entrada, saída, leitura e escrita de cache por modelo). O arquivo é preenchido pelo Operador para os modelos que ele configurar (DA-15). Um modelo ausente da tabela levanta `UnknownModelPricing`: nunca custo zero silencioso, porque o teto diário depende dessa conta.
- **Done when**:
  - [ ] teste prova que 1.000.000 de tokens de entrada de um modelo com `input: 3.0` custa `Decimal('3.0')`
  - [ ] teste prova que tokens de cache usam os preços `cache_read`/`cache_write` e não o de entrada
  - [ ] teste prova que modelo ausente levanta `UnknownModelPricing`
  - [ ] `uv run pytest -q tests/unit/api/llm/test_pricing.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não persistir custo em banco (ledger fica na demo pública, `api/limits/ledger.py`)

---

### TASK-008 — Token counting utility

- **Requisito**: `CHAT-05`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-005
- **Arquivos de produção**:
  - `api/llm/tokens.py`
- **Arquivos de teste**:
  - `tests/unit/api/llm/test_tokens.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-8 — `api.llm.tokens.count_tokens(text: str) -> int` e `count_messages(system: list[SystemBlock], messages: list[ChatMessage]) -> int` (tiktoken `o200k_base`) (produz)
  - CT-6 — `api.llm.client` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Contagem local de tokens com `tiktoken` (`o200k_base`), usada no orçamento de contexto, no chunking e no log de contexto (DA-9). É uma estimativa: o custo usa o `usage` devolvido pelo provedor.
- **Done when**:
  - [ ] teste prova que `count_tokens('')` é 0 e que um texto de 2.000 palavras em inglês dá entre 1.500 e 3.500 tokens
  - [ ] teste prova que `count_messages` soma system e mensagens e é ≥ à soma das partes
  - [ ] `uv run pytest -q tests/unit/api/llm/test_tokens.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não chamar a API de contagem do provedor (custo de rede)

---

### TASK-009 — Instrumented LLM client with per-call cost log and factory

- **Requisito**: `FND-04`, `CHAT-05`, `FND-90`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-006, TASK-007, TASK-008, TASK-002
- **Arquivos de produção**:
  - `api/llm/instrumented.py`
  - `api/llm/factory.py`
- **Arquivos de teste**:
  - `tests/unit/api/llm/test_instrumented.py`
- **Wiring permitido**: —
- **Reusa**:
  - `api/llm/pricing.py` → `estimate_cost`
  - `api/llm/tokens.py` → `count_messages`
  - `api/observability/logging.py` → `log_event`
- **Contrato**:
  - CT-9 — `api.llm.instrumented.InstrumentedLLMClient(inner: LLMClient, model: str, provider: str)` (implementa CT‑6 e emite `log_event("llm_call", trace_id, provider, model, purpose, context_tokens, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, latency_ms, outcome)`); `api.llm.factory.build_llm_client(settings: Settings) -> LLMClient` (produz)
  - CT-2 — `api.observability.logging` (assinatura completa: plan 8.2) (consome)
  - CT-6 — `api.llm.client` (assinatura completa: plan 8.2) (consome)
  - CT-7 — `api.llm.pricing.estimate_cost(model: str, usage: Usage) -> Decimal` (assinatura completa: plan 8.2) (consome)
  - CT-8 — `api.llm.tokens.count_tokens(text: str) -> int` (assinatura completa: plan 8.2) (consome)
  - CT-1 — `api.config.Settings` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Decorador de `LLMClient` que, ao terminar cada chamada (sucesso ou erro), emite o evento `llm_call` com trace_id, modelo, tokens de contexto estimados, tokens de entrada/saída/cache, custo e latência (FND-04, CHAT-05). `build_llm_client(settings)` monta `Instrumented(Anthropic)` ou `Instrumented(Fake)` conforme `ATLAS_LLM_PROVIDER`. A cadeia de resiliência entra na fase 12.
- **Done when**:
  - [ ] teste prova que uma chamada bem-sucedida gera exatamente 1 log `llm_call` com os campos `trace_id`, `model`, `input_tokens`, `output_tokens`, `context_tokens` e `cost_usd`
  - [ ] teste prova que uma chamada que levanta `LLMError` também gera o log, com `outcome='error'`, e relança o erro
  - [ ] teste prova que `build_llm_client` com provider `fake` não exige chave e com provider `anthropic` sem chave levanta `MissingConfigError`
  - [ ] `uv run pytest -q tests/unit/api/llm/test_instrumented.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 4 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não adicionar retry/fallback (fase 12)
  - Não criar spans OTel (fase 14)

---

### TASK-010 — FastAPI app factory and health endpoint

- **Requisito**: `FND-01`, `FND-03`
- **Tipo**: infra
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-003, TASK-004
- **Arquivos de produção**:
  - `api/main.py`
  - `api/health.py`
- **Arquivos de teste**:
  - `tests/integration/api/test_health.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-1 — `api.config.Settings` (assinatura completa: plan 8.2) (consome)
  - CT-2 — `api.observability.logging` (assinatura completa: plan 8.2) (consome)
  - CT-3 — `api.errors` (assinatura completa: plan 8.2) (consome)
  - CT-5 — `api.db.engine` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: `create_app()` registra o middleware de trace_id, os handlers de erro e o router de saúde. `GET /health` responde 200 `{"status": "ok", "db": "ok"}` após um `SELECT 1`, ou 503 com `db: "unavailable"`. O módulo expõe `app = create_app()` para o uvicorn.
- **Done when**:
  - [ ] teste com `httpx.AsyncClient(transport=ASGITransport(app))` prova `GET /health` → 200 com `db == 'ok'` e cabeçalho `X-Trace-Id` presente
  - [ ] teste prova 503 quando o engine aponta para um banco inexistente
  - [ ] `uv run pytest -q tests/integration/api/test_health.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não registrar rotas de chat/auth (tasks próprias, via wiring de `api/main.py`)

---

### TASK-011 — Minimal eval runner with JSONL datasets

- **Requisito**: `FND-05`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-001
- **Arquivos de produção**:
  - `evals/runner.py`
  - `evals/dataset.py`
- **Arquivos de teste**:
  - `tests/unit/evals/test_runner.py`
  - `evals/datasets/smoke.jsonl`
- **Wiring permitido**:
  - `evals/__init__.py` (criar vazio)
  - `evals/__main__.py` (CLI `run` chamando `run_cases` e gravando o JSON)
- **Reusa**: —
- **Contrato**:
  - CT-10 — Caso de eval JSONL `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}`; `evals.dataset.load_cases(paths: list[Path]) -> list[EvalCase]`; `evals.runner.run_cases(cases, target: EvalTarget, graders: Mapping[str, Grader]) -> EvalReport`; relatório JSON `{"run_id", "status": "ok"|"error", "cases": [{"id","category","language","score","passed","details"}], "scores": {"overall": float, "by_category": {str: float}}}`; CLI `python -m evals run --dataset <glob> --target <nome> --out <arquivo>` (produz)
  - CT-1 — `api.config.Settings` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Embrião da fase 13 (DA-17): carrega casos JSONL validados por Pydantic, roda cada caso contra um `EvalTarget` (protocolo `async run(case) -> dict`) e um grader nomeado, e grava o relatório JSON do CT-10, com score de 0 a 100 por caso, por categoria e geral. O grader inicial é `exact_match`.
- **Done when**:
  - [ ] teste prova que um dataset de 1 caso com target fake gera relatório com `scores.overall == 100.0` e `status == 'ok'`
  - [ ] teste prova que uma linha JSONL inválida faz `load_cases` levantar erro citando o número da linha
  - [ ] `uv run python -m evals run --dataset evals/datasets/smoke.jsonl --target echo --out reports/eval-smoke.json` (executar em `.`) termina com exit 0 e cria o arquivo
  - [ ] `uv run pytest -q tests/unit/evals/test_runner.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não implementar LLM-as-judge (fase 3, `evals/judge.py`) nem o gate de CI (fase 13)

---

### TASK-012 — Angular workspace scaffold with Vitest JUnit, ESLint and Playwright

- **Requisito**: `FND-01`
- **Tipo**: infra
- **Risco**: médio
- **Perfil**: frontend
- **Depende de**: —
- **Arquivos de produção**:
  - `web/package.json`
  - `web/angular.json`
- **Arquivos de teste**:
  - `web/src/app/app.spec.ts`
- **Wiring permitido**:
  - `web/` (arquivos gerados por `npx -y @angular/cli@22 new web --standalone --style=scss --ssr=false --skip-git`, `ng add angular-eslint` e `npm init playwright@latest`: `tsconfig*.json`, `eslint.config.js`, `src/**`, `public/**`, `package-lock.json`)
  - `web/vitest-base.config.ts` (reporter `junit` em `reports/junit.xml`, referenciado em `angular.json` `test.options.runnerConfig`)
  - `web/playwright.config.ts` (testDir `e2e`, reporter junit `reports/e2e-junit.xml`, webServer `npx ng serve --proxy-config proxy.conf.json`, porta 4200)
  - `web/proxy.conf.json` (`/api` → `http://localhost:8000`, `pathRewrite` removendo `/api`)
  - `web/Dockerfile` e `web/nginx.conf` (build estático + proxy `/api` → serviço `api:8000`)
- **Reusa**: —
- **Contrato**: —
- **Testes**: unit
- **Descrição**: Workspace Angular 22 standalone em `web/` (DA-6), seguindo o padrão observado em `~/Projects/interview-reviewer/frontend` (vitest-base.config.ts, angular-eslint, Playwright). O componente raiz só renderiza o `<router-outlet>`.
- **Done when**:
  - [ ] `npm ci` (executar em `web`) termina com exit 0
  - [ ] `npx ng test --watch=false --include=src/app/app.spec.ts` (executar em `web`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
  - [ ] `npx ng build` (executar em `web`) termina com exit 0
  - [ ] `npx ng lint` (executar em `web`) termina com exit 0
  - [ ] `web/reports/junit.xml` existe após o teste
- **Não fazer**:
  - Não criar telas (login/chat têm tasks próprias)
  - Não adicionar biblioteca de UI/i18n de terceiros (DA-7)

---

### TASK-013 — Docker images and one-command compose stack

- **Requisito**: `FND-01`
- **Tipo**: infra
- **Risco**: médio
- **Perfil**: infra
- **Depende de**: TASK-010, TASK-012
- **Arquivos de produção**:
  - `docker-compose.yml`
  - `Dockerfile`
- **Arquivos de teste**:
  - `tests/unit/infra/test_compose.py`
- **Wiring permitido**:
  - `.dockerignore` (criar)
  - `docker/api-entrypoint.sh` (criar: `alembic upgrade head` e depois `uvicorn api.main:app --host 0.0.0.0 --port 8000`)
- **Reusa**: —
- **Contrato**: —
- **Testes**: unit
- **Descrição**: Compose com `db` (pgvector/pgvector:pg16 + healthcheck), `api` (imagem `Dockerfile` com uv; aplica migrations no start) e `web` (`web/Dockerfile`, nginx na porta 8080), com `depends_on: condition: service_healthy` e `env_file: .env` (DA-18). O teste valida a estrutura do YAML sem subir containers.
- **Done when**:
  - [ ] teste prova que `docker-compose.yml` tem os serviços `db`, `api` e `web`, que `api` depende de `db` com `service_healthy` e que `web` publica a porta 8080
  - [ ] `docker compose config -q` (executar em `.`) termina com exit 0 com um `.env` copiado de `.env.example`
  - [ ] `uv run pytest -q tests/unit/infra/test_compose.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não adicionar Langfuse ao compose (fase 14, perfil `observability`)

---

### TASK-014 — CI workflow for lint, tests and minimal evals

- **Requisito**: `FND-02`
- **Tipo**: infra
- **Risco**: médio
- **Perfil**: infra
- **Depende de**: TASK-011, TASK-012, TASK-004
- **Arquivos de produção**:
  - `.github/workflows/ci.yml`
- **Arquivos de teste**:
  - `tests/unit/infra/test_ci_workflow.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**: —
- **Testes**: unit
- **Descrição**: Workflow em `pull_request` e `push`, com os jobs `python` (serviço Postgres `pgvector/pgvector:pg16`, `uv sync`, ruff, mypy, pytest), `web` (`npm ci`, `ng lint`, `ng test`, `ng build`) e `evals-smoke` (`python -m evals run` sobre `evals/datasets/smoke.jsonl` com `ATLAS_LLM_PROVIDER=fake`). Qualquer job falho marca o PR como falho.
- **Done when**:
  - [ ] teste carrega o YAML e prova que os jobs `python`, `web` e `evals-smoke` existem, que o gatilho inclui `pull_request` e que nenhum step tem `continue-on-error: true`
  - [ ] teste prova que o job `python` executa `uv run ruff check`, `uv run mypy` e `uv run pytest`
  - [ ] `uv run pytest -q tests/unit/infra/test_ci_workflow.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não rodar evals completos com LLM real (fase 13, `.github/workflows/evals.yml`)

---

### TASK-015 — Initial English README and results log

- **Requisito**: `FND-01`, `DOD-04`
- **Tipo**: config
- **Risco**: médio
- **Perfil**: infra
- **Depende de**: TASK-013
- **Arquivos de produção**:
  - `README.md`
  - `docs/results.md`
- **Arquivos de teste**:
  - `tests/unit/docs/test_docs_initial.py`
- **Wiring permitido**:
  - `docs/adr/0000-template.md` (template de ADR em inglês: Context, Decision, Alternatives considered, Consequences)
- **Reusa**: —
- **Contrato**: —
- **Testes**: unit
- **Descrição**: README em inglês com quickstart (`cp .env.example .env`, `docker compose up`), tabela das variáveis obrigatórias e seções vazias que a task final completa. `docs/results.md` traz a tabela `| Phase | Metric | Before | After | Notes |` com uma linha por fase de 1 a 14, vazia, que `record_result` atualiza.
- **Done when**:
  - [ ] teste prova que README contém `docker compose up` e cada variável obrigatória de `.env.example`
  - [ ] teste prova que `docs/results.md` tem 14 linhas de fase (`| 01 |` … `| 14 |`)
  - [ ] `uv run pytest -q tests/unit/docs/test_docs_initial.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
- **Não fazer**:
  - Não escrever ainda a frase de impacto, GIF, link da demo nem tabela de tags (task final de README)

---

### TASK-016 — Phase definition-of-done checker script

- **Requisito**: `DOD-01`, `DOD-04`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-011
- **Arquivos de produção**:
  - `scripts/check_phase.py`
- **Arquivos de teste**:
  - `tests/unit/scripts/test_check_phase.py`
- **Wiring permitido**: —
- **Reusa**:
  - `evals/dataset.py` → `load_cases`
- **Contrato**:
  - CT-11 — CLI `python scripts/check_phase.py <NN> [--require-tag]`: exit 0 se a fase está pronta (DOD-01), exit 1 listando o que falta (produz)
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Para a fase NN, verifica: há casos com `phase == NN` em `evals/datasets/*.jsonl`; `demos/demo_NN_*.py` existe; a linha NN de `docs/results.md` tem Before e After preenchidos; `docs/adr/00NN-*.md` existe, tem as seções Context/Decision/Alternatives e é detectado como inglês por `langdetect`; com `--require-tag`, a tag `phase-NN` existe (`git rev-parse`).
- **Done when**:
  - [ ] teste (repositório temporário) prova exit 0 quando os 4 artefatos estão presentes e exit 1 listando cada artefato ausente
  - [ ] teste prova que uma ADR em português faz a checagem de idioma falhar
  - [ ] `uv run pytest -q tests/unit/scripts/test_check_phase.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 4 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não criar tags git (ação do Operador, seção 16 do plan)

---

### TASK-017 — Demo harness and results writer

- **Requisito**: `DOD-02`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-009
- **Arquivos de produção**:
  - `demos/common.py`
- **Arquivos de teste**:
  - `tests/unit/demos/test_common.py`
- **Wiring permitido**:
  - `demos/__init__.py` (criar vazio)
  - `demos/data/` (pasta de entradas dos demos)
- **Reusa**: —
- **Contrato**:
  - CT-12 — `demos.common`: `parse_demo_args(description: str) -> argparse.Namespace` (flags `--provider {real,fake}`, `--results PATH` padrão `docs/results.md`, `--out-dir PATH` padrão `docs/img`), `build_demo_llm(provider: str) -> LLMClient`, `record_result(phase: int, metric: str, before: str, after: str, notes: str = "", results_path: Path = ...) -> None` (atualiza a linha da fase na tabela de `docs/results.md`, idempotente) (produz)
  - CT-9 — `api.llm.instrumented.InstrumentedLLMClient(inner: LLMClient, model: str, provider: str)` (assinatura completa: plan 8.2) (consome)
  - CT-1 — `api.config.Settings` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Utilitários dos scripts `demo_NN`: argumentos comuns, construção do LLM real ou fake e gravação idempotente da métrica da fase em `docs/results.md` (DA-16). `--provider fake` permite rodar todo demo no CI e nos testes sem chave.
- **Done when**:
  - [ ] teste prova que `record_result(3, ...)` chamado duas vezes deixa exatamente uma linha `| 03 |` com os valores da última chamada
  - [ ] teste prova que `build_demo_llm('fake')` devolve um `LLMClient` sem exigir chave
  - [ ] `uv run pytest -q tests/unit/demos/test_common.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não escrever demos específicos (uma task por fase)

---

### TASK-018 — Prompt loader and base system prompt

- **Requisito**: `CHAT-10`, `RAG-04`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-001
- **Arquivos de produção**:
  - `api/prompts.py`
  - `prompts/system.md`
- **Arquivos de teste**:
  - `tests/unit/api/test_prompts.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-13 — `api.prompts.load_prompt(name: str) -> str` (lê `prompts/<name>.md`, cacheado; `PromptNotFound`) (produz)
- **Testes**: unit
- **Descrição**: Prompts versionados em `prompts/` (pasta que dispara evals, EVAL-03). O `system.md`, em inglês, define o papel do Atlas, a regra de responder no idioma informado em `{language}`, a regra de citar `[n]` e a de dizer que não sabe fora dos trechos. Não leva nenhum dado dinâmico (CACHE-02).
- **Done when**:
  - [ ] teste prova que `load_prompt('system')` contém os placeholders `{language}` e a instrução de citação `[n]`
  - [ ] teste prova que `load_prompt('inexistente')` levanta `PromptNotFound`
  - [ ] `uv run pytest -q tests/unit/api/test_prompts.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não incluir data/hora nem nome de cliente no prompt de sistema

---

## Fase 1 — Chamada à API

### TASK-019 — Stateless chat service and request schemas

- **Requisito**: `CHAT-01`, `CHAT-09`, `CHAT-90`, `CHAT-91`, `CHAT-92`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-003, TASK-009, TASK-018
- **Arquivos de produção**:
  - `api/chat/service.py`
  - `api/chat/schemas.py`
- **Arquivos de teste**:
  - `tests/unit/api/chat/test_service_stateless.py`
- **Wiring permitido**:
  - `api/chat/__init__.py` (criar vazio)
- **Reusa**: —
- **Contrato**:
  - CT-14 — HTTP `POST /chat` — request `ChatRequest{message: str, conversation_id: UUID | None, ui_language: "pt"|"en" | None}`; response 200 `ChatResponse{conversation_id: UUID | None, message_id: int | None, status: "completed"|"awaiting_confirmation"|"blocked"|"degraded"|"limit_reached", content: str, language: "pt"|"en", citations: list[Citation{index, document, section, page}], confirmation: ConfirmationPrompt{id: UUID, tool: str, target: str, amount_cents: int | None, currency: "USD" | None} | None, trace_id: str}` (schemas em `api/chat/schemas.py`) (produz)
  - CT-15 — `api.chat.service.ChatService.handle_message(request: ChatRequest, principal: CurrentCustomer | None) -> AsyncIterator[AgentEvent]` e `ChatService.collect(events) -> ChatResponse`; fábrica `get_chat_service()` (dependência FastAPI) (produz)
  - CT-3 — `api.errors` (assinatura completa: plan 8.2) (consome)
  - CT-4 — `api.messages.user_message(key: MessageKey, lang: Lang, **params: object) -> str` (assinatura completa: plan 8.2) (consome)
  - CT-6 — `api.llm.client` (assinatura completa: plan 8.2) (consome)
  - CT-9 — `api.llm.instrumented.InstrumentedLLMClient(inner: LLMClient, model: str, provider: str)` (assinatura completa: plan 8.2) (consome)
  - CT-13 — `api.prompts.load_prompt(name: str) -> str` (assinatura completa: plan 8.2) (consome)
  - CT-1 — `api.config.Settings` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Primeira versão do `ChatService`: valida a mensagem (vazia ou só espaços → `VALIDATION_ERROR`; acima de `max_message_chars` → `MESSAGE_TOO_LONG`), chama o LLM uma vez sem histórico e devolve `ChatResponse` com `status='completed'`. Erro do provedor vira `AtlasError(LLM_UNAVAILABLE, 502)` e nada é persistido. Os schemas já têm a forma final do CT-14 (campos ainda não usados ficam `None`/vazios). `handle_message` produz `AgentEvent`s (`done`/`error`) e `collect` monta a resposta JSON.
- **Done when**:
  - [ ] teste com `FakeLLMClient` prova que a resposta contém o texto roteirizado e `status == 'completed'`
  - [ ] teste prova que mensagem `'   '` e mensagem com `max_message_chars + 1` caracteres levantam erro de validação sem nenhuma chamada registrada no fake
  - [ ] teste prova que `LLMError` do fake vira `AtlasError` com código `LLM_UNAVAILABLE`
  - [ ] `uv run pytest -q tests/unit/api/chat/test_service_stateless.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 4 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não persistir nada (fase 2)
  - Não chamar retrieval (fase 3)

---

### TASK-020 — POST /chat endpoint

- **Requisito**: `CHAT-01`, `CHAT-90`, `CHAT-91`, `CHAT-92`
- **Tipo**: crud-padrão
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-019, TASK-010
- **Arquivos de produção**:
  - `api/chat/routes.py`
- **Arquivos de teste**:
  - `tests/integration/api/chat/test_chat_route.py`
- **Wiring permitido**:
  - `api/main.py` (apenas `app.include_router(chat_router)`)
- **Reusa**: —
- **Contrato**:
  - CT-14 — `POST /chat` (assinatura completa: plan 8.2) (consome)
  - CT-15 — `api.chat.service.ChatService.handle_message(request: ChatRequest, principal: CurrentCustomer | None) -> AsyncIterator[AgentEvent]` (assinatura completa: plan 8.2) (consome)
  - CT-3 — `api.errors` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Rota `POST /chat` (CT-14), que delega ao `ChatService` via dependência sobrescrevível nos testes (`app.dependency_overrides[get_chat_service]`).
- **Done when**:
  - [ ] teste prova 200 com `content` não vazio usando o fake via `dependency_overrides`
  - [ ] teste prova 422 `VALIDATION_ERROR` para mensagem vazia e `MESSAGE_TOO_LONG` para mensagem longa, sem chamada ao fake
  - [ ] teste prova 502 `LLM_UNAVAILABLE` sem `Traceback` no corpo quando o fake levanta `LLMError`
  - [ ] `uv run pytest -q tests/integration/api/chat/test_chat_route.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não exigir sessão ainda (fase 2)

---

### TASK-021 — Phase 01 stateless demo script

- **Requisito**: `CHAT-02`, `DOD-02`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-020, TASK-017
- **Arquivos de produção**:
  - `demos/demo_01_stateless.py`
- **Arquivos de teste**:
  - `tests/unit/demos/test_demo_01.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-12 — `demos.common` (assinatura completa: plan 8.2) (consome)
  - CT-15 — `api.chat.service.ChatService.handle_message(request: ChatRequest, principal: CurrentCustomer | None) -> AsyncIterator[AgentEvent]` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Envia "My name is Rubens" / "Meu nome é Rubens" e depois "What is my name?" / "Qual é o meu nome?" em chamadas independentes (sem `conversation_id`), imprime as respostas e grava em `docs/results.md` se a 2ª contém o nome (antes) e a referência para a fase 2 (depois). Continua reproduzível em HEAD, porque chamadas sem `conversation_id` sempre criam conversa nova (DA-16).
- **Done when**:
  - [ ] teste roda `main(['--provider','fake','--results',<tmp>])` com roteiro em que a 2ª resposta não tem o nome, e prova saída `name_remembered=False` e a linha `| 01 |` no arquivo temporário
  - [ ] `uv run pytest -q tests/unit/demos/test_demo_01.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não usar histórico (é exatamente o problema reproduzido)

---

### TASK-022 — Phase 01 ADR and eval cases

- **Requisito**: `DOD-01`, `DOD-04`
- **Tipo**: config
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-021, TASK-016
- **Arquivos de produção**:
  - `docs/adr/0001-llm-api-call.md`
  - `evals/datasets/phase_01.jsonl`
- **Arquivos de teste**:
  - `tests/unit/evals/test_phase_01_cases.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
  - CT-11 — `python scripts/check_phase.py <NN> [--require-tag]` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: ADR em inglês (LLMClient próprio, adapter por provedor, chave por variável de ambiente) e casos de eval da fase (pt e en: resposta não vazia, categoria `tone`).
- **Done when**:
  - [ ] teste prova que `phase_01.jsonl` carrega com `load_cases`, tem ≥ 2 casos com `phase == 1` e ao menos um em `pt` e um em `en`
  - [ ] `uv run python scripts/check_phase.py 01` lista como faltante no máximo a métrica de `docs/results.md` (preenchida pelo Operador ao rodar o demo com provedor real)
  - [ ] `uv run pytest -q tests/unit/evals/test_phase_01_cases.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
- **Não fazer**:
  - Não criar a tag `phase-01` (Operador, seção 16)

---

## Fase 2 — Janela de contexto (+ login, UI base)

### TASK-023 — Customer and session schema

- **Requisito**: `TOOL-95`, `DEMO-06`
- **Tipo**: migration
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-022
- **Arquivos de produção**:
  - `api/auth/models.py`
  - `api/db/migrations/versions/0002_customers_sessions.py`
- **Arquivos de teste**:
  - `tests/integration/api/auth/test_auth_models.py`
- **Wiring permitido**:
  - `api/auth/__init__.py` (criar vazio)
  - `api/db/migrations/env.py` (apenas importar `api.auth.models`)
- **Reusa**: —
- **Contrato**:
  - CT-18 — ORM `api.auth.models.Customer(id UUID, username, display_name, email, password_hash, language)` e `CustomerSession(id, customer_id, token_hash, created_at, expires_at)` (produz)
  - CT-5 — `api.db.engine` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Tabelas `customers` (username único, e-mail fictício, hash de senha, idioma) e `customer_sessions` (token guardado só como SHA-256, expiração). Migration com `downgrade`.
- **Done when**:
  - [ ] teste prova `alembic upgrade head` e `downgrade -1` sem erro no banco de teste
  - [ ] teste prova que inserir dois customers com o mesmo `username` viola a constraint única
  - [ ] `uv run pytest -q tests/integration/api/auth/test_auth_models.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não implementar login (task de `api/auth/service.py`)

---

### TASK-024 — Conversation and message schema

- **Requisito**: `CHAT-03`, `CHAT-93`
- **Tipo**: migration
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-023
- **Arquivos de produção**:
  - `api/chat/models.py`
  - `api/db/migrations/versions/0003_conversations_messages.py`
- **Arquivos de teste**:
  - `tests/integration/api/chat/test_chat_models.py`
- **Wiring permitido**:
  - `api/db/migrations/env.py` (apenas importar `api.chat.models`)
- **Reusa**: —
- **Contrato**:
  - CT-19 — ORM `api.chat.models.Conversation(id UUID, customer_id, created_at, summary, summary_upto_message_id)` e `Message(id bigint, conversation_id, role, content, status: complete|cancelled|blocked|error|degraded, language, trace_id, created_at)` (produz)
  - CT-5 — `api.db.engine` (assinatura completa: plan 8.2) (consome)
  - CT-18 — `api.auth.models.Customer(id UUID, username, display_name, email, password_hash, language)` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Tabelas `conversations` (FK customer, `summary`, `summary_upto_message_id`) e `messages` (id `bigserial`, que dá a ordem de chegada; role; content já mascarado; `status`; language; trace_id). Índice `(conversation_id, id)`. O status `cancelled` já existe para a fase 9 (DA-8).
- **Done when**:
  - [ ] teste prova que duas mensagens inseridas em sequência são lidas em ordem crescente de `id`
  - [ ] teste prova que `status` fora de `complete|cancelled|blocked|error|degraded` é rejeitado por CHECK
  - [ ] `uv run pytest -q tests/integration/api/chat/test_chat_models.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não criar repositório (task própria)

---

### TASK-025 — Session authentication service and FastAPI dependencies

- **Requisito**: `TOOL-95`, `DEMO-01`, `TOOL-03`
- **Tipo**: lógica-negócio
- **Risco**: crítico
- **Âncora de risco**: AS-1 (autenticação/sessão — `api/auth/service.py`, `api/auth/deps.py`), AS-7 (hash de senha e token — `api/auth/service.py`) — Jev c_auth p=0.98, c_cripto p=0.96
- **Perfil**: backend
- **Depende de**: TASK-023
- **Arquivos de produção**:
  - `api/auth/service.py`
  - `api/auth/deps.py`
- **Arquivos de teste**:
  - `tests/integration/api/auth/test_auth_service.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-16 — `api.auth.deps`: `CurrentCustomer(id: UUID, email: str, display_name: str, language: Lang)`, `require_customer() -> CurrentCustomer` (cookie `atlas_session`; 401 `AUTH_REQUIRED`), `Principal = CurrentCustomer | OperatorPrincipal`, `require_customer_or_operator() -> Principal` (cabeçalho `X-Operator-Key` comparado com `secrets.compare_digest`); `api.auth.service`: `authenticate(session, username, password) -> CurrentCustomer`, `create_session(session, customer_id) -> str`, `resolve_session(session, token) -> CurrentCustomer | None`, `revoke_session(session, token) -> None`, `hash_password(raw) -> str` (produz)
  - CT-18 — `api.auth.models.Customer(id UUID, username, display_name, email, password_hash, language)` (assinatura completa: plan 8.2) (consome)
  - CT-3 — `api.errors` (assinatura completa: plan 8.2) (consome)
  - CT-1 — `api.config.Settings` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Senha com argon2 (`argon2-cffi`), token de sessão com `secrets.token_urlsafe(32)` guardado como SHA-256 e expiração em `session_ttl_hours` (DA-10). `require_customer` lê o cookie `atlas_session` e devolve 401 `AUTH_REQUIRED` sem sessão válida. `require_customer_or_operator` também aceita `X-Operator-Key` igual a `ATLAS_OPERATOR_API_KEY`, comparada com `secrets.compare_digest` (LAC-35). Sem chave configurada, o caminho de operador fica desligado.
- **Done when**:
  - [ ] teste prova login certo → sessão resolvível; senha errada → `INVALID_CREDENTIALS`; token expirado → `None`
  - [ ] teste prova que a tabela guarda só o hash do token (o token em claro não aparece em `customer_sessions`)
  - [ ] teste prova que `require_customer_or_operator` aceita a chave correta, recusa chave errada e recusa qualquer chave quando `ATLAS_OPERATOR_API_KEY` não está definida
  - [ ] `uv run pytest -q tests/integration/api/auth/test_auth_service.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 6 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não criar rotas HTTP (task `api/auth/routes.py`)
  - Não aceitar identificação de cliente vinda do corpo da requisição

---

### TASK-026 — Login, logout and me endpoints

- **Requisito**: `DEMO-01`, `TOOL-95`
- **Tipo**: crud-padrão
- **Risco**: crítico
- **Âncora de risco**: AS-1 (sessão por cookie — `api/auth/routes.py`) — acima do teto de crud-padrão porque emite a sessão (Jev c_auth p=0.80)
- **Perfil**: backend
- **Depende de**: TASK-025
- **Arquivos de produção**:
  - `api/auth/routes.py`
- **Arquivos de teste**:
  - `tests/integration/api/auth/test_auth_routes.py`
- **Wiring permitido**:
  - `api/main.py` (apenas `include_router(auth_router)`)
- **Reusa**: —
- **Contrato**:
  - CT-17 — HTTP `POST /auth/login {username, password}` → 200 `{customer: {display_name, language}}` + `Set-Cookie: atlas_session=<token>; HttpOnly; SameSite=Strict; Path=/; Secure (se ATLAS_COOKIE_SECURE)`; 401 `INVALID_CREDENTIALS`; `POST /auth/logout` → 204; `GET /auth/me` → 200 `{display_name, language}` ou 401 `AUTH_REQUIRED` (produz)
  - CT-16 — `api.auth.deps` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Rotas do CT-17. O cookie sai com `HttpOnly`, `SameSite=Strict`, `Path=/` e `Secure` conforme `ATLAS_COOKIE_SECURE`. O login responde igual para usuário inexistente e senha errada.
- **Done when**:
  - [ ] teste prova `POST /auth/login` → 200 com `Set-Cookie` contendo `HttpOnly` e `SameSite=Strict`
  - [ ] teste prova 401 `INVALID_CREDENTIALS` com corpo idêntico para usuário inexistente e senha errada
  - [ ] teste prova `GET /auth/me` 200 com o cookie e 401 após `POST /auth/logout`
  - [ ] `uv run pytest -q tests/integration/api/auth/test_auth_routes.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 4 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não implementar cadastro nem recuperação de senha (fora de escopo)

---

### TASK-027 — Fixed test personas seed command

- **Requisito**: `DEMO-06`
- **Tipo**: crud-padrão
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-025
- **Arquivos de produção**:
  - `api/seed/personas.py`
  - `api/seed/__main__.py`
- **Arquivos de teste**:
  - `tests/integration/api/seed/test_personas.py`
- **Wiring permitido**:
  - `api/seed/__init__.py` (criar vazio)
  - `docker/api-entrypoint.sh` (apenas executar `python -m api.seed` após as migrations)
- **Reusa**: —
- **Contrato**:
  - CT-23 — `api.seed.personas.PERSONAS` (tupla de `PersonaSpec(username, display_name, email, language)`, ≥ 2 itens: `alex` (en) e `bia` (pt)) e `seed_personas(session, password: str) -> list[UUID]`; CLI `python -m api.seed [--commerce] [--reset]` (produz)
  - CT-16 — `api.auth.deps` (assinatura completa: plan 8.2) (consome)
  - CT-18 — `api.auth.models.Customer(id UUID, username, display_name, email, password_hash, language)` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Pelo menos duas personas fixas (`alex`, en, e `bia`, pt) com e-mail fictício em `@example.com` e a senha de `ATLAS_PERSONA_PASSWORD`. O seed é idempotente (upsert por username).
- **Done when**:
  - [ ] teste prova que rodar `seed_personas` duas vezes deixa exatamente 2 customers e que os dois autenticam com a senha configurada
  - [ ] `uv run python -m api.seed` (executar em `.`) termina com exit 0 contra o banco do compose
  - [ ] `uv run pytest -q tests/integration/api/seed/test_personas.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não semear pedidos/assinaturas (fase 6, `api/seed/commerce.py`)

---

### TASK-028 — Conversation repository with ownership checks

- **Requisito**: `CHAT-03`, `CHAT-93`, `CHAT-94`
- **Tipo**: crud-padrão
- **Risco**: crítico
- **Âncora de risco**: AS-1 (posse de conversa — `api/chat/repository.py`) — acima do teto de crud-padrão porque decide acesso a conversas (Jev c_auth p=0.81)
- **Perfil**: backend
- **Depende de**: TASK-024
- **Arquivos de produção**:
  - `api/chat/repository.py`
- **Arquivos de teste**:
  - `tests/integration/api/chat/test_repository.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-20 — `api.chat.repository.ConversationRepository(session)`: `get_owned(conversation_id, customer_id) -> Conversation` (levanta `AtlasError(CONVERSATION_NOT_FOUND, 404)` igual para inexistente e alheia), `create(customer_id) -> Conversation`, `append_message(conversation_id, role, content, status, language, trace_id) -> Message`, `list_messages(conversation_id) -> list[Message]`, `save_summary(conversation_id, summary, upto_message_id) -> None`, `delete_for_customers(customer_ids) -> int` (produz)
  - CT-19 — `api.chat.models.Conversation(id UUID, customer_id, created_at, summary, summary_upto_message_id)` (assinatura completa: plan 8.2) (consome)
  - CT-3 — `api.errors` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Acesso a conversas sempre filtrado por `customer_id`. Conversa inexistente e conversa de outro cliente levantam o mesmo `CONVERSATION_NOT_FOUND` 404, sem revelar a existência (CHAT-94). `append_message` faz um INSERT por mensagem, sem lock: a ordem vem do `bigserial` (CHAT-93).
- **Done when**:
  - [ ] teste prova que `get_owned` com id inexistente e com id de outro cliente levanta erros de mesmo código, status e mensagem
  - [ ] teste prova que 2 `append_message` concorrentes (`asyncio.gather`, sessões distintas) persistem as 2 mensagens
  - [ ] teste prova que `list_messages` devolve em ordem de inserção
  - [ ] `uv run pytest -q tests/integration/api/chat/test_repository.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não mascarar PII aqui (fase 10, o serviço entrega o texto já mascarado)

---

### TASK-029 — Message language detection (pt, en, other)

- **Requisito**: `CHAT-10`, `CHAT-95`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-022
- **Arquivos de produção**:
  - `api/chat/language.py`
- **Arquivos de teste**:
  - `tests/unit/api/chat/test_language.py`
- **Wiring permitido**:
  - `pyproject.toml` (apenas adicionar `langdetect` se não estiver)
- **Reusa**: —
- **Contrato**:
  - CT-21 — `api.chat.language.detect_language(text: str, fallback: Lang) -> Literal["pt", "en", "other"]` (produz)
- **Testes**: unit
- **Descrição**: Usa `langdetect` com seed fixa (DA-9). Devolve `other` só se o texto tem ≥ 20 caracteres alfabéticos e a probabilidade do idioma principal (não pt/en) é ≥ 0,90. Nos demais casos ambíguos, devolve o `fallback` (idioma da UI) para não bloquear mensagens curtas.
- **Done when**:
  - [ ] teste prova `pt` para 'Quero cancelar meu pedido de ontem', `en` para 'I want to cancel my order from yesterday' e `other` para uma frase longa em alemão
  - [ ] teste prova que 'ok' devolve o fallback informado
  - [ ] `uv run pytest -q tests/unit/api/chat/test_language.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 4 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não chamar LLM para detectar idioma

---

### TASK-030 — Context budget with summarization of older messages

- **Requisito**: `CHAT-06`, `CHAT-08`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-024
- **Arquivos de produção**:
  - `api/chat/context.py`
  - `prompts/summarize.md`
- **Arquivos de teste**:
  - `tests/unit/api/chat/test_context.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-22 — `api.chat.context.build_history(messages: list[Message], summary: str | None, budget_tokens: int, summarizer: Summarizer) -> HistoryWindow(summary: str | None, messages: list[ChatMessage], tokens: int, summarized_upto: int | None)`; `HISTORY_MODE=full` desliga o resumo (produz)
  - CT-8 — `api.llm.tokens.count_tokens(text: str) -> int` (assinatura completa: plan 8.2) (consome)
  - CT-13 — `api.prompts.load_prompt(name: str) -> str` (assinatura completa: plan 8.2) (consome)
  - CT-6 — `api.llm.client` (assinatura completa: plan 8.2) (consome)
  - CT-19 — `api.chat.models.Conversation(id UUID, customer_id, created_at, summary, summary_upto_message_id)` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Monta a janela de histórico: mantém íntegras as mensagens mais recentes que cabem no orçamento e resume (via LLM, prompt `summarize`) as mais antigas, incluindo o resumo anterior. A soma de tokens (resumo + recentes) nunca passa de `context_budget_tokens`. `ATLAS_HISTORY_MODE=full` envia o histórico inteiro, usado no 'antes' do demo_02 (DA-16).
- **Done when**:
  - [ ] teste com 60 mensagens de ~300 tokens e orçamento 8.000 prova `window.tokens <= 8000`, a última mensagem presente e intacta e o resumo não vazio
  - [ ] teste prova que histórico abaixo do orçamento não chama o summarizer
  - [ ] teste prova que `full` não resume
  - [ ] `uv run pytest -q tests/unit/api/chat/test_context.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não persistir o resumo aqui (o serviço chama `save_summary`)

---

### TASK-031 — Conversational chat service with persisted history

- **Requisito**: `CHAT-03`, `CHAT-04`, `CHAT-06`, `CHAT-10`, `CHAT-95`, `CHAT-93`
- **Tipo**: lógica-negócio
- **Risco**: alto
- **Âncora de risco**: AS-3 (primeira persistência de texto do visitante e vínculo conversa↔cliente — `api/chat/service.py`) — decisão humana LAC-38 (Jev c_auth p=0.61, a_regra_central p=0.62)
- **Perfil**: backend
- **Depende de**: TASK-028, TASK-029, TASK-030, TASK-025
- **Arquivos de produção**:
  - `api/chat/service.py`
- **Arquivos de teste**:
  - `tests/integration/api/chat/test_service_history.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-15 — `api.chat.service.ChatService.handle_message(request: ChatRequest, principal: CurrentCustomer | None) -> AsyncIterator[AgentEvent]` (assinatura completa: plan 8.2) (consome)
  - CT-16 — `api.auth.deps` (assinatura completa: plan 8.2) (consome)
  - CT-20 — `api.chat.repository.ConversationRepository(session)` (assinatura completa: plan 8.2) (consome)
  - CT-21 — `api.chat.language.detect_language(text: str, fallback: Lang) -> Literal["pt", "en", "other"]` (assinatura completa: plan 8.2) (consome)
  - CT-22 — `api.chat.context.build_history(messages: list[Message], summary: str | None, budget_tokens: int, summarizer: Summarizer) -> HistoryWindow(summary: str | None, messages: list[ChatMessage], tokens: int, summarized_upto: int | None)` (assinatura completa: plan 8.2) (consome)
  - CT-4 — `api.messages.user_message(key: MessageKey, lang: Lang, **params: object) -> str` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: O `ChatService` passa a exigir `CurrentCustomer`: cria ou abre a conversa do dono, persiste a mensagem do usuário ao chegar, monta o histórico (CT-22), detecta o idioma e grava a resposta do assistente. Idioma `other` → mensagem fixa em inglês (`UNSUPPORTED_LANGUAGE`) sem LLM e sem tools (CHAT-95). Erro do LLM não persiste resposta vazia (CHAT-92).
- **Done when**:
  - [ ] teste prova, com fake, que a 2ª mensagem da mesma conversa envia ao LLM o histórico contendo 'Rubens' (CHAT-04)
  - [ ] teste prova que, após um turno, `messages` tem user + assistant na ordem certa
  - [ ] teste prova que uma mensagem em alemão devolve o texto fixo em inglês e o fake registra 0 chamadas
  - [ ] teste prova que um erro do LLM deixa só a mensagem do usuário persistida
  - [ ] `uv run pytest -q tests/integration/api/chat/test_service_history.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 4 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não adicionar retrieval nem tools

---

### TASK-032 — Authenticated chat route with conversation ownership

- **Requisito**: `CHAT-94`, `TOOL-95`, `DEMO-01`
- **Tipo**: crud-padrão
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-031, TASK-026
- **Arquivos de produção**:
  - `api/chat/routes.py`
- **Arquivos de teste**:
  - `tests/integration/api/chat/test_chat_route_auth.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-14 — `POST /chat` (assinatura completa: plan 8.2) (consome)
  - CT-16 — `api.auth.deps` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: `POST /chat` passa a exigir `require_customer`. Sem sessão → 401 `AUTH_REQUIRED` antes de qualquer chamada ao LLM. `conversation_id` de outro cliente → 404 `CONVERSATION_NOT_FOUND`.
- **Done when**:
  - [ ] teste prova 401 sem cookie e 0 chamadas registradas no fake
  - [ ] teste prova 404 idêntico para conversa inexistente e conversa de outra persona
  - [ ] `uv run pytest -q tests/integration/api/chat/test_chat_route_auth.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não implementar rate limit (demo pública)

---

### TASK-033 — Web i18n service with pt and en dictionaries

- **Requisito**: `STR-08`, `STR-09`, `DEMO-11`
- **Tipo**: ui-puro
- **Risco**: médio
- **Perfil**: frontend
- **Depende de**: TASK-012, TASK-022
- **Arquivos de produção**:
  - `web/src/app/core/i18n/i18n.service.ts`
  - `web/src/app/core/i18n/messages.ts`
- **Arquivos de teste**:
  - `web/src/app/core/i18n/i18n.service.spec.ts`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-24 — Web `I18nService` (`lang: Signal<'pt'|'en'>`, `setLang(l)`, `t(key: MessageKey, params?) : string`, idioma inicial pelo `navigator.language`, persistido em `localStorage['atlas.lang']`) (produz)
- **Testes**: unit
- **Descrição**: Serviço próprio com signals (DA-7), sem biblioteca. Idioma inicial = `navigator.language`, se começar com `pt` ou `en`, senão `en` (STR-09). A troca manual persiste em `localStorage`. `messages.ts` tem um dicionário `pt` e um `en` com as mesmas chaves.
- **Done when**:
  - [ ] teste prova idioma inicial `pt` para `pt-BR`, `en` para `en-US` e `en` para `de-DE`
  - [ ] teste prova que `setLang('pt')` muda `t('login.title')` e persiste em `localStorage`
  - [ ] teste prova que os dicionários `pt` e `en` têm exatamente o mesmo conjunto de chaves
  - [ ] `npx ng test --watch=false --include=src/app/core/i18n/i18n.service.spec.ts` (executar em `web`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 4 testes, todos passando
  - [ ] `npx ng lint` e `npx tsc --noEmit -p tsconfig.app.json` (executar em `web`) sem erro novo nos arquivos da task
- **Não fazer**:
  - Não usar `@angular/localize` (compila um build por idioma e não troca em tempo de execução)

---

### TASK-034 — Web auth service and route guard

- **Requisito**: `DEMO-01`
- **Tipo**: crud-padrão
- **Risco**: médio
- **Perfil**: frontend
- **Depende de**: TASK-012, TASK-026
- **Arquivos de produção**:
  - `web/src/app/core/auth/auth.service.ts`
  - `web/src/app/core/auth/auth.guard.ts`
- **Arquivos de teste**:
  - `web/src/app/core/auth/auth.service.spec.ts`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-25 — Web `AuthService` (`login(username, password): Observable<Me>`, `logout()`, `me(): Observable<Me | null>`, `isAuthenticated: Signal<boolean>`) e `authGuard: CanActivateFn` (redireciona para `/login`) (produz)
  - CT-17 — `POST /auth/login {username, password}` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Cliente HTTP de `/api/auth/*` com `withCredentials`. O guard redireciona para `/login` quando `GET /api/auth/me` devolve 401.
- **Done when**:
  - [ ] teste com `HttpTestingController` prova que `login` faz `POST /api/auth/login` e atualiza `isAuthenticated()` para `true`
  - [ ] teste prova que o guard devolve `UrlTree('/login')` quando `me` responde 401
  - [ ] `npx ng test --watch=false --include=src/app/core/auth/auth.service.spec.ts` (executar em `web`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `npx ng lint` e `npx tsc --noEmit -p tsconfig.app.json` (executar em `web`) sem erro novo nos arquivos da task
- **Não fazer**:
  - Não guardar token no `localStorage` (a sessão é cookie HttpOnly)

---

### TASK-035 — Bilingual login page with persona instructions

- **Requisito**: `DEMO-01`, `DEMO-06`, `DEMO-11`, `STR-09`
- **Tipo**: ui-puro
- **Risco**: médio
- **Perfil**: frontend
- **Depende de**: TASK-033, TASK-034
- **Arquivos de produção**:
  - `web/src/app/features/login/login.page.ts`
  - `web/src/app/features/login/login.page.html`
- **Arquivos de teste**:
  - `web/src/app/features/login/login.page.spec.ts`
- **Wiring permitido**:
  - `web/src/app/app.routes.ts` (apenas rotas `login` e redirect padrão)
  - `web/src/app/core/i18n/messages.ts` (apenas adicionar chaves `login.*`)
- **Reusa**: —
- **Contrato**:
  - CT-24 — `I18nService` (assinatura completa: plan 8.2) (consome)
  - CT-25 — `AuthService` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Formulário de usuário e senha, seletor de idioma PT/EN e um bloco que explica as personas de teste (nomes `alex`/`bia` e que a senha está no README), em pt e en. Erro 401 mostra a mensagem traduzida.
- **Done when**:
  - [ ] teste prova que, com idioma `pt`, o título e as instruções aparecem em português e, após `setLang('en')`, em inglês
  - [ ] teste prova que submit chama `AuthService.login` e navega para `/chat` no sucesso
  - [ ] `npx ng test --watch=false --include=src/app/features/login/login.page.spec.ts` (executar em `web`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `npx ng lint` e `npx tsc --noEmit -p tsconfig.app.json` (executar em `web`) sem erro novo nos arquivos da task
- **Não fazer**:
  - Não criar cadastro de usuário

---

### TASK-036 — Web chat API client and shared models

- **Requisito**: `CHAT-01`, `TOOL-05`
- **Tipo**: crud-padrão
- **Risco**: médio
- **Perfil**: frontend
- **Depende de**: TASK-012, TASK-022
- **Arquivos de produção**:
  - `web/src/app/core/chat/chat-api.service.ts`
  - `web/src/app/core/chat/chat.models.ts`
- **Arquivos de teste**:
  - `web/src/app/core/chat/chat-api.service.spec.ts`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-26 — Web `ChatApiService` (`send(req: ChatRequest): Observable<ChatResponse>`, `confirm(id: string, decision: 'confirm'|'refuse'): Observable<ChatResponse>`) e tipos TS espelhando CT‑14/CT‑46 em `chat.models.ts` (produz)
  - CT-14 — `POST /chat` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Tipos TS espelhando `ChatRequest`/`ChatResponse`/`ConfirmationPrompt`/`Citation` (CT-14) e o envelope de erro. `send()` faz `POST /api/chat`. `confirm()` faz `POST /api/chat/confirmations/{id}` (contrato CT-46, endpoint entregue na fase 6).
- **Done when**:
  - [ ] teste prova que `send` faz POST em `/api/chat` com `withCredentials: true` e o corpo `{message, conversation_id, ui_language}`
  - [ ] teste prova que `confirm('abc','refuse')` faz POST em `/api/chat/confirmations/abc` com `{decision:'refuse'}`
  - [ ] `npx ng test --watch=false --include=src/app/core/chat/chat-api.service.spec.ts` (executar em `web`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `npx ng lint` e `npx tsc --noEmit -p tsconfig.app.json` (executar em `web`) sem erro novo nos arquivos da task
- **Não fazer**:
  - Não implementar SSE (fase 9, `chat-stream.service.ts`)

---

### TASK-037 — Chat page with conversation thread and input validation

- **Requisito**: `CHAT-01`, `CHAT-90`, `CHAT-91`, `STR-08`
- **Tipo**: ui-puro
- **Risco**: médio
- **Perfil**: frontend
- **Depende de**: TASK-033, TASK-036, TASK-034
- **Arquivos de produção**:
  - `web/src/app/features/chat/chat.page.ts`
  - `web/src/app/features/chat/chat.page.html`
- **Arquivos de teste**:
  - `web/src/app/features/chat/chat.page.spec.ts`
- **Wiring permitido**:
  - `web/src/app/app.routes.ts` (apenas rota `chat` com `authGuard`)
  - `web/src/app/core/i18n/messages.ts` (apenas chaves `chat.*`)
  - `web/src/styles.scss` (estilos do chat)
- **Reusa**: —
- **Contrato**:
  - CT-24 — `I18nService` (assinatura completa: plan 8.2) (consome)
  - CT-26 — `ChatApiService` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Tela de conversa: lista de mensagens, campo de entrada e envio. O campo valida no próprio componente (vazio, ou acima de 4.000 caracteres, o padrão da API) e mostra a mensagem traduzida inline. Mantém o `conversation_id` devolvido.
- **Done when**:
  - [ ] teste prova que enviar texto só com espaços não chama `ChatApiService.send` e exibe a mensagem de validação
  - [ ] teste prova que, após a resposta, a mensagem do assistente aparece e o próximo envio leva o `conversation_id` recebido
  - [ ] `npx ng test --watch=false --include=src/app/features/chat/chat.page.spec.ts` (executar em `web`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `npx ng lint` e `npx tsc --noEmit -p tsconfig.app.json` (executar em `web`) sem erro novo nos arquivos da task
- **Não fazer**:
  - Não implementar confirmação, streaming, feedback nem trace_id (fases 6, 9 e 14)

---

### TASK-038 — Phase 02 context window demo with token chart

- **Requisito**: `CHAT-02`, `CHAT-07`, `DOD-02`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-032, TASK-027
- **Arquivos de produção**:
  - `demos/demo_02_context_window.py`
- **Arquivos de teste**:
  - `tests/integration/demos/test_demo_02.py`
- **Wiring permitido**:
  - `pyproject.toml` (apenas adicionar `matplotlib` ao grupo `dev`)
- **Reusa**: —
- **Contrato**:
  - CT-12 — `demos.common` (assinatura completa: plan 8.2) (consome)
  - CT-15 — `api.chat.service.ChatService.handle_message(request: ChatRequest, principal: CurrentCustomer | None) -> AsyncIterator[AgentEvent]` (assinatura completa: plan 8.2) (consome)
  - CT-22 — `api.chat.context.build_history(messages: list[Message], summary: str | None, budget_tokens: int, summarizer: Summarizer) -> HistoryWindow(summary: str | None, messages: list[ChatMessage], tokens: int, summarized_upto: int | None)` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Repete o cenário do demo_01 numa mesma conversa, agora lembrando o nome, e roda 50 turnos nos modos `full` e `summarized`. Plota tokens por chamada dos dois modos em `docs/img/phase02_tokens.png` e registra em `docs/results.md` o total de tokens de cada modo.
- **Done when**:
  - [ ] teste (`--provider fake`, results e out-dir temporários) prova que o PNG é criado, que a linha `| 02 |` existe e que, no modo `summarized`, nenhuma chamada passa de `context_budget_tokens` tokens de histórico
  - [ ] `uv run pytest -q tests/integration/demos/test_demo_02.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não mudar o algoritmo de resumo (task `api/chat/context.py`)

---

### TASK-039 — Phase 02 ADR and eval cases

- **Requisito**: `DOD-01`, `DOD-04`
- **Tipo**: config
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-038, TASK-037, TASK-035
- **Arquivos de produção**:
  - `docs/adr/0002-context-window.md`
  - `evals/datasets/phase_02.jsonl`
- **Arquivos de teste**:
  - `tests/unit/evals/test_phase_02_cases.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: ADR (histórico em banco, orçamento de 8.000 tokens, resumo vs janela deslizante) e casos de memória multi-turno em pt e en.
- **Done when**:
  - [ ] teste prova ≥ 2 casos com `phase == 2`, um `pt` e um `en`
  - [ ] `uv run pytest -q tests/unit/evals/test_phase_02_cases.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
- **Não fazer**:
  - Não criar a tag `phase-02` (Operador)

---

## Fase 3 — RAG full-text

### TASK-040 — English support corpus and manifest

- **Requisito**: `RAG-02`, `RAG-14`, `TOOL-16`, `TOOL-19`
- **Tipo**: config
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-039
- **Arquivos de produção**:
  - `corpus/en/`
  - `corpus/manifest.yaml`
- **Arquivos de teste**:
  - `tests/unit/corpus/test_manifest.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**: —
- **Testes**: unit
- **Descrição**: ~20 documentos em inglês (LAC-37): manual do cliente, políticas internas e FAQ, com `format: md` ou `pdf`. Os de formato pdf são escritos em Markdown e convertidos pela task de PDFs. O doc de encerramento usa só "end subscription", nunca "cancel plan" (RAG-16). A política de reembolso traz exatamente a regra do TOOL-16, e os valores aparecem em USD. Inclui a fonte do PDF malicioso (`malicious: true`), com instruções de injection em en e pt. O manifesto lista `doc_key`, `title`, `doc_type`, `format`, `languages: [en, pt]`, `malicious`.
- **Done when**:
  - [ ] teste prova que todo item do manifesto tem arquivo-fonte em `corpus/en/`, que há os três `doc_type` e os dois `format`, e que há exatamente 1 item `malicious: true`
  - [ ] teste prova que o documento `end-subscription` contém 'end subscription' e não contém 'cancel plan'
  - [ ] teste prova que o documento `refund-policy` contém '7 days' e 'USD'
  - [ ] `uv run pytest -q tests/unit/corpus/test_manifest.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 4 testes, todos passando
- **Não fazer**:
  - Não escrever a versão em português (task própria)
  - Não gerar PDFs aqui

---

### TASK-041 — Portuguese support corpus mirroring the manifest

- **Requisito**: `RAG-02`, `RAG-14`, `TOOL-16`, `TOOL-19`
- **Tipo**: config
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-040
- **Arquivos de produção**:
  - `corpus/pt/`
- **Arquivos de teste**:
  - `tests/unit/corpus/test_corpus_parity.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**: —
- **Testes**: unit
- **Descrição**: Tradução de todos os documentos do manifesto para pt. O doc de encerramento usa só "encerrar assinatura", nunca "cancelar plano" (RAG-05). Valores em USD (LAC-30).
- **Done when**:
  - [ ] teste prova que cada `doc_key` do manifesto tem fonte em `corpus/pt/` e em `corpus/en/`
  - [ ] teste prova que `corpus/pt/end-subscription.md` contém 'encerrar assinatura' e não contém 'cancelar plano'
  - [ ] teste prova que `corpus/pt/refund-policy.md` contém '7 dias' e 'USD'
  - [ ] `uv run pytest -q tests/unit/corpus/test_corpus_parity.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
- **Não fazer**:
  - Não alterar o manifesto (só a task do corpus EN)

---

### TASK-042 — Corpus PDF builder and generated PDFs

- **Requisito**: `RAG-02`
- **Tipo**: infra
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-041
- **Arquivos de produção**:
  - `scripts/build_corpus_pdfs.py`
  - `corpus/pdf/`
- **Arquivos de teste**:
  - `tests/unit/scripts/test_build_corpus_pdfs.py`
- **Wiring permitido**:
  - `pyproject.toml` (apenas `reportlab` no grupo `dev` e `pypdf` nas dependências)
- **Reusa**: —
- **Contrato**: —
- **Testes**: unit
- **Descrição**: Converte as fontes com `format: pdf` em `corpus/pdf/<lang>/<doc_key>.pdf` com `reportlab`, numa página por seção. Os PDFs gerados são versionados, para a ingestão não depender do builder.
- **Done when**:
  - [ ] teste gera os PDFs num diretório temporário e prova que `pypdf` extrai de cada um o título do documento
  - [ ] `uv run python scripts/build_corpus_pdfs.py` (executar em `.`) termina com exit 0 e gera um PDF por item pdf × idioma
  - [ ] `uv run pytest -q tests/unit/scripts/test_build_corpus_pdfs.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não ingerir nada

---

### TASK-043 — Document and chunk schema with bilingual full-text column

- **Requisito**: `RAG-14`, `RAG-15`
- **Tipo**: migration
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-039
- **Arquivos de produção**:
  - `api/rag/models.py`
  - `api/db/migrations/versions/0004_documents_chunks.py`
- **Arquivos de teste**:
  - `tests/integration/api/rag/test_rag_models.py`
- **Wiring permitido**:
  - `api/rag/__init__.py` (criar vazio)
  - `api/db/migrations/env.py` (apenas importar `api.rag.models`)
- **Reusa**: —
- **Contrato**:
  - CT-27 — ORM `api.rag.models.Document(id, doc_key, title, language, doc_type: manual|policy|faq, format: md|pdf, source_path, content_hash, status: indexed|quarantined|failed, error, ingested_at)` e `Chunk(id, document_id, ordinal, content, language, section, page, token_count, ts_config: regconfig, tsv (gerada), embedding vector(ATLAS_EMBEDDING_DIMENSIONS) NULL)` (produz)
  - CT-5 — `api.db.engine` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: `documents` (único por `(doc_key, language)`) e `chunks` com `ts_config regconfig` e `tsv tsvector GENERATED ALWAYS AS (to_tsvector(ts_config, content)) STORED` + índice GIN (DA-13). A coluna `embedding` entra na fase 4.
- **Done when**:
  - [ ] teste prova que um chunk pt com 'assinaturas' casa com `to_tsquery('portuguese','assinatura')` e um chunk en não casa com essa query
  - [ ] teste prova que remover um document apaga os chunks em cascata
  - [ ] `uv run pytest -q tests/integration/api/rag/test_rag_models.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não criar coluna de embedding (fase 4)

---

### TASK-044 — Markdown and PDF parsers with token-based chunker

- **Requisito**: `RAG-01`, `RAG-91`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-039
- **Arquivos de produção**:
  - `ingest/parsers.py`
  - `ingest/chunking.py`
- **Arquivos de teste**:
  - `tests/unit/ingest/test_parsers_chunking.py`
- **Wiring permitido**:
  - `ingest/__init__.py` (criar vazio)
- **Reusa**: —
- **Contrato**:
  - CT-8 — `api.llm.tokens.count_tokens(text: str) -> int` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: O parser Markdown divide por cabeçalhos (seção = cabeçalho) e o PDF por página (`pypdf`). O chunker faz janelas de ~500 tokens com sobreposição de 80 (constantes nomeadas), sem cortar no meio de uma seção quando ela cabe inteira. Arquivo ilegível levanta `UnreadableDocument`.
- **Done when**:
  - [ ] teste prova que um texto de 1.400 tokens gera 3 chunks de no máximo 500 tokens, com sobreposição ≥ 60 tokens entre consecutivos
  - [ ] teste prova que cada chunk carrega `section` (md) ou `page` (pdf)
  - [ ] teste prova que um PDF corrompido levanta `UnreadableDocument`
  - [ ] `uv run pytest -q tests/unit/ingest/test_parsers_chunking.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não gravar em banco

---

### TASK-045 — Ingestion pipeline CLI and report

- **Requisito**: `RAG-01`, `RAG-02`, `RAG-14`, `RAG-91`, `RAG-92`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-043, TASK-044, TASK-042
- **Arquivos de produção**:
  - `ingest/pipeline.py`
  - `ingest/report.py`
- **Arquivos de teste**:
  - `tests/integration/ingest/test_pipeline.py`
- **Wiring permitido**:
  - `ingest/__main__.py` (CLI `run` do CT-28)
- **Reusa**: —
- **Contrato**:
  - CT-28 — CLI `python -m ingest run [--corpus corpus] [--report reports/ingest-report.json] [--skip-embeddings]` e `python -m ingest reindex-hnsw`; `IngestReport{documents: [{doc_key, language, doc_type, format, status, chunks, error}], totals: {indexed, quarantined, failed}}` (produz)
  - CT-27 — `api.rag.models.Document(id, doc_key, title, language, doc_type: manual|policy|faq, format: md|pdf, source_path, content_hash, status: indexed|quarantined|failed, error, ingested_at)` (assinatura completa: plan 8.2) (consome)
  - CT-5 — `api.db.engine` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Lê o manifesto, processa cada documento × idioma numa transação própria (falha num não para os outros, RAG-91), substitui os chunks de documento já indexado (delete + insert por `(doc_key, language)`, RAG-92) e grava o relatório JSON do CT-28 com tipo, formato e status de cada documento (RAG-02).
- **Done when**:
  - [ ] teste com corpus mínimo temporário prova status `indexed` para md e pdf e `failed` para um arquivo corrompido, com os demais processados
  - [ ] teste prova que ingerir 2 vezes não duplica chunks (contagem igual)
  - [ ] teste prova que todo chunk tem `language` e metadado de fonte
  - [ ] `uv run pytest -q tests/integration/ingest/test_pipeline.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não gerar embeddings (fase 4)
  - Não fazer varredura de injection (fase 10)

---

### TASK-046 — Language-aware full-text search

- **Requisito**: `RAG-15`, `RAG-05`, `RAG-16`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-043
- **Arquivos de produção**:
  - `api/rag/fulltext.py`
- **Arquivos de teste**:
  - `tests/integration/api/rag/test_fulltext.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-29 — `api.rag.fulltext.search_fulltext(session, query: str, language: Lang, k: int) -> list[ScoredChunk(chunk_id: int, score: float)]` (config `english`/`portuguese` pelo idioma) (produz)
  - CT-27 — `api.rag.models.Document(id, doc_key, title, language, doc_type: manual|policy|faq, format: md|pdf, source_path, content_hash, status: indexed|quarantined|failed, error, ingested_at)` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: `websearch_to_tsquery(<config do idioma>, query)` e `ts_rank_cd`, filtrando `chunks.language = idioma da pergunta` (RAG-15), só sobre documentos `indexed`.
- **Done when**:
  - [ ] teste prova que 'cancelar plano' (pt) NÃO retorna o chunk que só fala em 'encerrar assinatura' (RAG-05)
  - [ ] teste prova que 'cancel plan' (en) NÃO retorna o chunk que só fala em 'end subscription' (RAG-16)
  - [ ] teste prova que 'reembolso' retorna só chunks `pt`
  - [ ] `uv run pytest -q tests/integration/api/rag/test_fulltext.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não implementar busca vetorial

---

### TASK-047 — Retriever facade with pluggable retrieval modes

- **Requisito**: `RAG-03`, `RAG-90`, `RAG-94`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-046
- **Arquivos de produção**:
  - `api/rag/retriever.py`
- **Arquivos de teste**:
  - `tests/integration/api/rag/test_retriever.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-30 — `api.rag.retriever.Retriever(session_factory, embedder, settings)`: `async retrieve(query: str, language: Lang, k: int | None = None, mode: RetrievalMode | None = None) -> list[RetrievedChunk(chunk_id, document_title, doc_key, section, page, language, content, score)]`; `RetrievalUnavailable` quando busca/embedding falha; `RetrievalMode = Literal["none","fulltext","vector_bruteforce","hnsw","hybrid"]`; `register_mode(mode, searcher)` (produz)
  - CT-29 — `api.rag.fulltext.search_fulltext(session, query: str, language: Lang, k: int) -> list[ScoredChunk(chunk_id: int, score: float)]` (assinatura completa: plan 8.2) (consome)
  - CT-1 — `api.config.Settings` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Fachada que despacha por `ATLAS_RETRIEVAL_MODE` (registro de modos, DA-16). Esta task registra `none` e `fulltext`, e as fases 4 e 5 registram os demais. Hidrata os chunks com título, seção e página. Qualquer exceção de busca vira `RetrievalUnavailable`. Índice vazio devolve lista vazia, sem erro (RAG-90).
- **Done when**:
  - [ ] teste prova que, com banco sem documentos, `retrieve` devolve `[]`
  - [ ] teste prova que uma falha simulada no buscador levanta `RetrievalUnavailable`
  - [ ] teste prova que o modo `none` devolve `[]` sem consultar o banco
  - [ ] `uv run pytest -q tests/integration/api/rag/test_retriever.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não registrar modos vetoriais (fases 4 e 5)

---

### TASK-048 — Prompt builder with numbered chunks and citation extraction

- **Requisito**: `RAG-03`, `RAG-04`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-047
- **Arquivos de produção**:
  - `api/chat/prompt_builder.py`
  - `api/rag/citations.py`
- **Arquivos de teste**:
  - `tests/unit/api/chat/test_prompt_builder.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-31 — `api.chat.prompt_builder.build_request(*, language: Lang, history: HistoryWindow, chunks: list[RetrievedChunk], user_message: str, tools: list[ToolSpec], layout: Literal["naive","cache_ordered"]) -> LLMRequest` (produz)
  - CT-32 — `api.rag.citations.extract_citations(answer: str, chunks: list[RetrievedChunk]) -> list[Citation]` (marcadores `[n]`, 1-based, só índices existentes) (produz)
  - CT-6 — `api.llm.client` (assinatura completa: plan 8.2) (consome)
  - CT-13 — `api.prompts.load_prompt(name: str) -> str` (assinatura completa: plan 8.2) (consome)
  - CT-22 — `api.chat.context.build_history(messages: list[Message], summary: str | None, budget_tokens: int, summarizer: Summarizer) -> HistoryWindow(summary: str | None, messages: list[ChatMessage], tokens: int, summarized_upto: int | None)` (assinatura completa: plan 8.2) (consome)
  - CT-30 — `api.rag.retriever.Retriever(session_factory, embedder, settings)` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Monta o `LLMRequest` com system (`{language}` preenchido), trechos numerados `[n]` com fonte, histórico e pergunta. Sem trechos, inclui a instrução explícita de responder que não sabe (RAG-04). `extract_citations` converte marcadores `[n]` em `Citation`s, ignorando índices inexistentes.
- **Done when**:
  - [ ] teste prova que 3 chunks aparecem no request numerados [1]..[3] com título e seção
  - [ ] teste prova que, com 0 chunks, o request contém a instrução de não saber
  - [ ] teste prova que 'ver [2] e [9]' com 3 chunks gera exatamente 1 citação (índice 2)
  - [ ] `uv run pytest -q tests/unit/api/chat/test_prompt_builder.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não marcar trechos como não confiáveis (fase 10)
  - Não reordenar para cache (fase 11)

---

### TASK-049 — Retrieval-augmented chat service

- **Requisito**: `RAG-03`, `RAG-04`, `RAG-90`, `RAG-94`
- **Tipo**: lógica-negócio
- **Risco**: alto
- **Âncora de risco**: AS-3 (mensagens persistidas — `api/chat/service.py`) — regra central RAG, Jev a_regra_central p=0.93
- **Perfil**: backend
- **Depende de**: TASK-048
- **Arquivos de produção**:
  - `api/chat/service.py`
- **Arquivos de teste**:
  - `tests/integration/api/chat/test_service_rag.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-15 — `api.chat.service.ChatService.handle_message(request: ChatRequest, principal: CurrentCustomer | None) -> AsyncIterator[AgentEvent]` (assinatura completa: plan 8.2) (consome)
  - CT-30 — `api.rag.retriever.Retriever(session_factory, embedder, settings)` (assinatura completa: plan 8.2) (consome)
  - CT-31 — `api.chat.prompt_builder.build_request(*, language: Lang, history: HistoryWindow, chunks: list[RetrievedChunk], user_message: str, tools: list[ToolSpec], layout: Literal["naive","cache_ordered"]) -> LLMRequest` (assinatura completa: plan 8.2) (consome)
  - CT-32 — `api.rag.citations.extract_citations(answer: str, chunks: list[RetrievedChunk]) -> list[Citation]` (assinatura completa: plan 8.2) (consome)
  - CT-4 — `api.messages.user_message(key: MessageKey, lang: Lang, **params: object) -> str` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Antes de chamar o LLM, recupera os top-k no idioma da mensagem, monta o request pelo prompt builder e devolve as citações na resposta. `RetrievalUnavailable` → mensagem fixa `KNOWLEDGE_UNAVAILABLE`, sem chamar o LLM (RAG-94).
- **Done when**:
  - [ ] teste prova que a resposta inclui `citations` correspondentes aos `[n]` do texto roteirizado
  - [ ] teste prova que, com o retriever falhando, a resposta é o texto fixo de base indisponível e o fake registra 0 chamadas
  - [ ] teste prova que, com índice vazio, o LLM é chamado com a instrução de não saber e a resposta é devolvida sem erro
  - [ ] `uv run pytest -q tests/integration/api/chat/test_service_rag.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não adicionar tools

---

### TASK-050 — LLM-as-judge faithfulness grader

- **Requisito**: `RAG-18`, `EVAL-02`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-039
- **Arquivos de produção**:
  - `evals/judge.py`
  - `prompts/judge_faithfulness.md`
- **Arquivos de teste**:
  - `tests/unit/evals/test_judge.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-33 — `evals.judge.judge_faithfulness(llm: LLMClient, question: str, answer: str, chunks: list[str]) -> JudgeVerdict(faithful: bool, score: float, rationale: str)` (produz)
  - CT-6 — `api.llm.client` (assinatura completa: plan 8.2) (consome)
  - CT-13 — `api.prompts.load_prompt(name: str) -> str` (assinatura completa: plan 8.2) (consome)
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Juiz com rubrica de fidelidade aos trechos e saída estruturada (`response_schema`). Registra-se como grader `faithfulness` no runner.
- **Done when**:
  - [ ] teste com fake prova que um veredito JSON válido vira `JudgeVerdict` e que um JSON inválido levanta erro tipado
  - [ ] teste prova que o grader `faithfulness` aparece no registro de graders do runner
  - [ ] `uv run pytest -q tests/unit/evals/test_judge.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não implementar o gate de CI

---

### TASK-051 — Phase 03 RAG demo with hallucination benchmark

- **Requisito**: `RAG-05`, `RAG-06`, `RAG-16`, `RAG-18`, `RAG-19`, `DOD-02`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-049, TASK-050, TASK-045
- **Arquivos de produção**:
  - `demos/demo_03_rag.py`
  - `demos/data/phase03_questions.jsonl`
- **Arquivos de teste**:
  - `tests/integration/demos/test_demo_03.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-12 — `demos.common` (assinatura completa: plan 8.2) (consome)
  - CT-29 — `api.rag.fulltext.search_fulltext(session, query: str, language: Lang, k: int) -> list[ScoredChunk(chunk_id: int, score: float)]` (assinatura completa: plan 8.2) (consome)
  - CT-33 — `evals.judge.judge_faithfulness(llm: LLMClient, question: str, answer: str, chunks: list[str]) -> JudgeVerdict(faithful: bool, score: float, rationale: str)` (assinatura completa: plan 8.2) (consome)
  - CT-28 — `python -m ingest run [--corpus corpus] [--report reports/ingest-report.json] [--skip-embeddings]` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Mostra 'cancelar plano' e 'cancel plan' sem acerto no full-text (problema da fase 3, em pt e en) e mede a taxa de alucinação nas 30 perguntas (15 pt + 15 en), sem RAG (`mode=none`) e com RAG, usando o juiz. Gera uma amostra para revisão manual (`docs/review/phase03_sample.csv`, 10 respostas) e grava as taxas por idioma e o tamanho da amostra em `docs/results.md`.
- **Done when**:
  - [ ] `phase03_questions.jsonl` tem exatamente 30 linhas, 15 `pt` e 15 `en` (verificado no teste)
  - [ ] teste com `--provider fake` prova a linha `| 03 |` com taxas por idioma e o CSV de amostra com 10 linhas
  - [ ] teste prova que o relatório do demo marca `fulltext_synonym_hit=False` para 'cancelar plano' e para 'cancel plan'
  - [ ] `uv run pytest -q tests/integration/demos/test_demo_03.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não habilitar embeddings

---

### TASK-052 — Phase 03 ADR and eval cases

- **Requisito**: `DOD-01`, `DOD-04`
- **Tipo**: config
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-051
- **Arquivos de produção**:
  - `docs/adr/0003-rag-full-text.md`
  - `evals/datasets/phase_03.jsonl`
- **Arquivos de teste**:
  - `tests/unit/evals/test_phase_03_cases.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: ADR (chunking ~500/80, `tsvector` por idioma, citação por marcador) e casos `rag` (resposta + citação esperada) em pt e en.
- **Done when**:
  - [ ] teste prova ≥ 4 casos `phase == 3` de categoria `rag`, metade `pt`, metade `en`, com `expected.citation_doc_key`
  - [ ] `uv run pytest -q tests/unit/evals/test_phase_03_cases.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
- **Não fazer**:
  - Não criar a tag `phase-03`

---

## Fase 4 — Embeddings

### TASK-053 — OpenAI embedding client with deterministic fake

- **Requisito**: `RAG-07`, `RAG-93`, `RAG-94`
- **Tipo**: integração-externa
- **Risco**: alto
- **Âncora de risco**: AS-8 (primeira integração de embeddings — `api/rag/embeddings.py`) — Jev a_terceiro p=0.91
- **Perfil**: backend
- **Depende de**: TASK-052
- **Arquivos de produção**:
  - `api/rag/embeddings.py`
- **Arquivos de teste**:
  - `tests/unit/api/rag/test_embeddings.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-34 — `api.rag.embeddings.EmbeddingClient(Protocol).embed(texts: list[str]) -> list[list[float]]`; `OpenAIEmbeddingClient(settings)`, `FakeEmbeddingClient(dim)` (determinístico por hash); `EmbeddingError`; `build_embedder(settings) -> EmbeddingClient` (produz)
  - CT-1 — `api.config.Settings` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Cliente de embeddings OpenAI `text-embedding-3-small` com `dimensions = ATLAS_EMBEDDING_DIMENSIONS` (DA-19, premissa LAC-19), em lotes de até 100 textos. Qualquer erro vira `EmbeddingError`. `FakeEmbeddingClient` gera vetores determinísticos por hash, com uma tabela opcional de sinônimos para os testes.
- **Done when**:
  - [ ] teste com `httpx.MockTransport` prova o envio em lotes (250 textos → 3 requisições) e a ordem preservada
  - [ ] teste prova que HTTP 500 vira `EmbeddingError`
  - [ ] teste prova que `FakeEmbeddingClient` devolve o mesmo vetor para o mesmo texto
  - [ ] `uv run pytest -q tests/unit/api/rag/test_embeddings.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não chamar a rede em teste

---

### TASK-054 — Chunk embedding column migration

- **Requisito**: `RAG-07`
- **Tipo**: migration
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-052
- **Arquivos de produção**:
  - `api/db/migrations/versions/0005_chunk_embeddings.py`
  - `api/rag/models.py`
- **Arquivos de teste**:
  - `tests/integration/api/rag/test_embedding_column.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-27 — `api.rag.models.Document(id, doc_key, title, language, doc_type: manual|policy|faq, format: md|pdf, source_path, content_hash, status: indexed|quarantined|failed, error, ingested_at)` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Adiciona `chunks.embedding vector(ATLAS_EMBEDDING_DIMENSIONS)` anulável (tabela sem dados em produção, AS-4 NÃO) e o mapeamento `pgvector.sqlalchemy.Vector`.
- **Done when**:
  - [ ] teste prova gravar e ler um vetor de `ATLAS_EMBEDDING_DIMENSIONS` posições
  - [ ] `uv run pytest -q tests/integration/api/rag/test_embedding_column.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não criar índice HNSW (fase 5)

---

### TASK-055 — Embedding step in ingestion with failure isolation

- **Requisito**: `RAG-07`, `RAG-93`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-053, TASK-054
- **Arquivos de produção**:
  - `ingest/pipeline.py`
- **Arquivos de teste**:
  - `tests/integration/ingest/test_pipeline_embeddings.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-34 — `api.rag.embeddings.EmbeddingClient(Protocol).embed(texts: list[str]) -> list[list[float]]` (assinatura completa: plan 8.2) (consome)
  - CT-28 — `python -m ingest run [--corpus corpus] [--report reports/ingest-report.json] [--skip-embeddings]` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Gera os embeddings dos chunks de um documento antes do commit. `EmbeddingError` marca o documento `failed`, reprocessável, sem gravar nenhum chunk dele (RAG-93). `--skip-embeddings` mantém o comportamento da fase 3.
- **Done when**:
  - [ ] teste prova que, após a ingestão com o fake, `SELECT count(*) FROM chunks WHERE embedding IS NULL` = 0
  - [ ] teste prova que, com o embedder falhando no 2º documento, ele fica `failed`, tem 0 chunks e os outros ficam `indexed`
  - [ ] `uv run pytest -q tests/integration/ingest/test_pipeline_embeddings.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não mexer no chunker

---

### TASK-056 — Brute-force cosine vector search

- **Requisito**: `RAG-08`, `RAG-17`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-054, TASK-053
- **Arquivos de produção**:
  - `api/rag/vector_bruteforce.py`
- **Arquivos de teste**:
  - `tests/integration/api/rag/test_vector_bruteforce.py`
- **Wiring permitido**:
  - `api/rag/retriever.py` (apenas `register_mode('vector_bruteforce', ...)` e chamada ao embedder da consulta)
- **Reusa**: —
- **Contrato**:
  - CT-35 — Buscadores vetoriais com a assinatura `async search(session, query_vector: list[float], language: Lang, k: int) -> list[ScoredChunk]`: `api.rag.vector_bruteforce.search_bruteforce` e `api.rag.vector_hnsw.search_hnsw` (produz)
  - CT-30 — `api.rag.retriever.Retriever(session_factory, embedder, settings)` (assinatura completa: plan 8.2) (consome)
  - CT-34 — `api.rag.embeddings.EmbeddingClient(Protocol).embed(texts: list[str]) -> list[list[float]]` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Carrega os embeddings do idioma da pergunta numa matriz NumPy e ordena por similaridade de cosseno (força bruta, fase 4).
- **Done when**:
  - [ ] teste com fake de sinônimos prova que 'cancelar plano' traz o chunk de 'encerrar assinatura' no top-5 (RAG-08)
  - [ ] teste prova o mesmo para 'cancel plan' / 'end subscription' (RAG-17)
  - [ ] teste prova que os scores estão em ordem decrescente
  - [ ] `uv run pytest -q tests/integration/api/rag/test_vector_bruteforce.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não usar operador `<=>` do pgvector (é a fase 5)

---

### TASK-057 — Synthetic vector benchmark harness

- **Requisito**: `RAG-09`, `RAG-11`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-054
- **Arquivos de produção**:
  - `demos/bench.py`
- **Arquivos de teste**:
  - `tests/unit/demos/test_bench.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-37 — `demos.bench`: `generate_vectors(n: int, dim: int, seed: int) -> np.ndarray`, `load_bench_table(session, vectors) -> None`, `measure(search_fn, queries, k) -> BenchResult(p50_ms, p95_ms, recall_at_5)` (produz)
  - CT-12 — `demos.common` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Gera vetores sintéticos normalizados com seed e mede latência (p50/p95) e recall@5 contra o ground truth exato da força bruta. A carga em `bench_vectors` usa `COPY`.
- **Done when**:
  - [ ] teste prova que `generate_vectors(1000, 8, seed=1)` é determinístico e normalizado (norma 1 ± 1e-6)
  - [ ] teste prova que `measure` de um buscador exato devolve `recall_at_5 == 1.0`
  - [ ] `uv run pytest -q tests/unit/demos/test_bench.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não criar a tabela `bench_vectors` (migration da fase 5); nesta fase o benchmark da força bruta é só em memória

---

### TASK-058 — Phase 04 embeddings demo with recall and latency

- **Requisito**: `RAG-08`, `RAG-09`, `RAG-17`, `DOD-02`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-056, TASK-057, TASK-055
- **Arquivos de produção**:
  - `demos/demo_04_embeddings.py`
  - `demos/data/synonym_questions.jsonl`
- **Arquivos de teste**:
  - `tests/integration/demos/test_demo_04.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-12 — `demos.common` (assinatura completa: plan 8.2) (consome)
  - CT-37 — `demos.bench` (assinatura completa: plan 8.2) (consome)
  - CT-35 — `async search(session, query_vector: list[float], language: Lang, k: int) -> list[ScoredChunk]` (assinatura completa: plan 8.2) (consome)
  - CT-29 — `api.rag.fulltext.search_fulltext(session, query: str, language: Lang, k: int) -> list[ScoredChunk(chunk_id: int, score: float)]` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Mede o recall@5 de full-text vs embedding nas perguntas com sinônimos (pt e en) e a latência da força bruta em 10k, 100k e 1M vetores sintéticos (`--sizes` configurável, padrão `10000,100000,1000000`). Registra tudo em `docs/results.md`.
- **Done when**:
  - [ ] teste com `--provider fake --sizes 1000,2000` prova a linha `| 04 |` com recall dos dois métodos e latências
  - [ ] `uv run pytest -q tests/integration/demos/test_demo_04.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não usar HNSW

---

### TASK-059 — Phase 04 ADR and eval cases

- **Requisito**: `DOD-01`, `DOD-04`
- **Tipo**: config
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-058
- **Arquivos de produção**:
  - `docs/adr/0004-embeddings.md`
  - `evals/datasets/phase_04.jsonl`
- **Arquivos de teste**:
  - `tests/unit/evals/test_phase_04_cases.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: ADR (provedor de embeddings OpenAI, dimensão, força bruta em NumPy) e casos `rag` de sinônimos pt/en.
- **Done when**:
  - [ ] teste prova ≥ 2 casos `phase == 4`, um `pt` e um `en`
  - [ ] `uv run pytest -q tests/unit/evals/test_phase_04_cases.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
- **Não fazer**:
  - Não criar a tag

---

## Fase 5 — Vector database

### TASK-060 — HNSW index migration, bench table and reindex command

- **Requisito**: `RAG-10`
- **Tipo**: migration
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-059
- **Arquivos de produção**:
  - `api/db/migrations/versions/0006_hnsw_and_bench.py`
  - `ingest/reindex.py`
- **Arquivos de teste**:
  - `tests/integration/ingest/test_reindex.py`
- **Wiring permitido**:
  - `ingest/__main__.py` (apenas subcomando `reindex-hnsw`)
- **Reusa**: —
- **Contrato**:
  - CT-28 — `python -m ingest run [--corpus corpus] [--report reports/ingest-report.json] [--skip-embeddings]` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Cria o índice `hnsw (embedding vector_cosine_ops) WITH (m = ATLAS_HNSW_M, ef_construction = ATLAS_HNSW_EF_CONSTRUCTION)` e a tabela `bench_vectors`. `reindex-hnsw` recria o índice com os valores atuais da configuração, sem mudar código (RAG-10).
- **Done when**:
  - [ ] teste prova que, após `reindex-hnsw` com `ATLAS_HNSW_M=8`, `pg_indexes.indexdef` contém `m='8'`
  - [ ] `uv run pytest -q tests/integration/ingest/test_reindex.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não alterar a busca

---

### TASK-061 — HNSW vector search with configurable ef_search

- **Requisito**: `RAG-10`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-060
- **Arquivos de produção**:
  - `api/rag/vector_hnsw.py`
- **Arquivos de teste**:
  - `tests/integration/api/rag/test_vector_hnsw.py`
- **Wiring permitido**:
  - `api/rag/retriever.py` (apenas `register_mode('hnsw', ...)`)
- **Reusa**: —
- **Contrato**:
  - CT-35 — `async search(session, query_vector: list[float], language: Lang, k: int) -> list[ScoredChunk]` (assinatura completa: plan 8.2) (consome)
  - CT-30 — `api.rag.retriever.Retriever(session_factory, embedder, settings)` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: `SET LOCAL hnsw.ef_search = ATLAS_HNSW_EF_SEARCH` na transação e `ORDER BY embedding <=> :q LIMIT k`, filtrando o idioma.
- **Done when**:
  - [ ] teste prova que o plano (`EXPLAIN`) usa o índice HNSW com `enable_seqscan=off`
  - [ ] teste prova que o valor efetivo de `SHOW hnsw.ef_search` dentro da busca é o configurado
  - [ ] `uv run pytest -q tests/integration/api/rag/test_vector_hnsw.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não remover a força bruta (demo_04 continua reproduzível, DA-16)

---

### TASK-062 — Hybrid search with Reciprocal Rank Fusion

- **Requisito**: `RAG-12`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-061
- **Arquivos de produção**:
  - `api/rag/hybrid.py`
- **Arquivos de teste**:
  - `tests/unit/api/rag/test_hybrid.py`
- **Wiring permitido**:
  - `api/rag/retriever.py` (apenas `register_mode('hybrid', ...)` combinando full-text e HNSW)
- **Reusa**: —
- **Contrato**:
  - CT-36 — `api.rag.hybrid.rrf_fuse(rankings: list[list[ScoredChunk]], k: int, rrf_k: int = 60) -> list[ScoredChunk]` (produz)
  - CT-29 — `api.rag.fulltext.search_fulltext(session, query: str, language: Lang, k: int) -> list[ScoredChunk(chunk_id: int, score: float)]` (assinatura completa: plan 8.2) (consome)
  - CT-35 — `async search(session, query_vector: list[float], language: Lang, k: int) -> list[ScoredChunk]` (assinatura completa: plan 8.2) (consome)
  - CT-30 — `api.rag.retriever.Retriever(session_factory, embedder, settings)` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: RRF: score = Σ 1/(rrf_k + rank), com `rrf_k = ATLAS_RRF_K` (padrão 60). É o modo padrão de produção. Premissa LAC-18: sem reranker.
- **Done when**:
  - [ ] teste prova que um item em 1º nas duas listas fica em 1º no resultado fundido
  - [ ] teste prova que um item presente só numa lista ainda aparece no top-k
  - [ ] `uv run pytest -q tests/unit/api/rag/test_hybrid.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não adicionar reranker (fora de escopo)

---

### TASK-063 — Phase 05 HNSW vs brute-force benchmark demo

- **Requisito**: `RAG-11`, `RAG-13`, `DOD-02`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-062
- **Arquivos de produção**:
  - `demos/demo_05_hnsw.py`
- **Arquivos de teste**:
  - `tests/integration/demos/test_demo_05.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-12 — `demos.common` (assinatura completa: plan 8.2) (consome)
  - CT-37 — `demos.bench` (assinatura completa: plan 8.2) (consome)
  - CT-35 — `async search(session, query_vector: list[float], language: Lang, k: int) -> list[ScoredChunk]` (assinatura completa: plan 8.2) (consome)
  - CT-36 — `api.rag.hybrid.rrf_fuse(rankings: list[list[ScoredChunk]], k: int, rrf_k: int = 60) -> list[ScoredChunk]` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Carrega 1M vetores sintéticos (`--size`) em `bench_vectors` e mede p50/p95 e recall@5 da força bruta e do HNSW. Também mede o recall@5 da busca híbrida no conjunto de sinônimos. Grava a tabela em `docs/results.md`.
- **Done when**:
  - [ ] teste com `--size 2000 --provider fake` prova a linha `| 05 |` com p50, p95 e recall dos dois métodos mais o recall da híbrida
  - [ ] `uv run pytest -q tests/integration/demos/test_demo_05.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não mudar o índice de produção

---

### TASK-064 — Phase 05 ADR and eval cases

- **Requisito**: `DOD-01`, `DOD-04`
- **Tipo**: config
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-063
- **Arquivos de produção**:
  - `docs/adr/0005-vector-database.md`
  - `evals/datasets/phase_05.jsonl`
- **Arquivos de teste**:
  - `tests/unit/evals/test_phase_05_cases.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: ADR (pgvector HNSW, trade-off `m`/`ef_search`, RRF) e casos de termos exatos + sinônimos.
- **Done when**:
  - [ ] teste prova ≥ 2 casos `phase == 5`, um `pt` e um `en`
  - [ ] `uv run pytest -q tests/unit/evals/test_phase_05_cases.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
- **Não fazer**:
  - Não criar a tag

---

## Fase 6 — Tool calling

### TASK-065 — Orders, subscriptions, refunds, audit and email outbox schema

- **Requisito**: `TOOL-17`, `TOOL-19`, `TOOL-93`, `TOOL-06`
- **Tipo**: migration
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-064
- **Arquivos de produção**:
  - `api/tools/models.py`
  - `api/db/migrations/versions/0007_commerce.py`
- **Arquivos de teste**:
  - `tests/integration/api/tools/test_commerce_models.py`
- **Wiring permitido**:
  - `api/tools/__init__.py` (criar vazio)
  - `api/db/migrations/env.py` (apenas importar `api.tools.models`)
- **Reusa**: —
- **Contrato**:
  - CT-38 — ORM `api.tools.models`: `OrderStatus` (pending, paid, shipped, delivered, cancelled), `Order(id int, customer_id, status, total_cents, currency='USD', items JSONB, created_at, shipped_at, delivered_at, cancelled_at)`, `Subscription(id, customer_id UNIQUE, plan_code, status, price_cents, currency, renews_at)`, `Refund(id, order_id UNIQUE, customer_id, amount_cents, currency, created_at, trace_id)`, `AuditLog(id, customer_id, tool, arguments JSONB, result JSONB, outcome: success|error|refused, trace_id, created_at)`, `EmailOutbox(id, customer_id, recipient, subject, body, trace_id, created_at)` (produz)
  - CT-5 — `api.db.engine` (assinatura completa: plan 8.2) (consome)
  - CT-18 — `api.auth.models.Customer(id UUID, username, display_name, email, password_hash, language)` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Tabelas do CT-38 com `CHECK (currency = 'USD')`, enum de status do pedido, `UNIQUE(refunds.order_id)` (no máximo um reembolso por pedido, TOOL-93) e índices por `customer_id` e `trace_id`.
- **Done when**:
  - [ ] teste prova que inserir 2 refunds para o mesmo pedido viola a unicidade
  - [ ] teste prova que `currency='BRL'` é rejeitada
  - [ ] teste prova que status fora dos 5 valores é rejeitado
  - [ ] `uv run pytest -q tests/integration/api/tools/test_commerce_models.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não implementar tools

---

### TASK-066 — Commerce seed data per persona

- **Requisito**: `TOOL-01`, `DEMO-06`, `TOOL-19`, `TOOL-98`
- **Tipo**: crud-padrão
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-065
- **Arquivos de produção**:
  - `api/seed/commerce.py`
- **Arquivos de teste**:
  - `tests/integration/api/seed/test_commerce_seed.py`
- **Wiring permitido**:
  - `api/seed/__main__.py` (apenas flag `--commerce`)
- **Reusa**: —
- **Contrato**:
  - CT-23 — `api.seed.personas.PERSONAS` (assinatura completa: plan 8.2) (consome)
  - CT-38 — `api.tools.models` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Para cada persona: uma assinatura e pedidos em todos os estados, com datas relativas a `now` (pedido pago cancelável, enviado, entregue há 3, 7 e 10 dias, cancelado), valores em centavos USD. É idempotente e devolve o estado inicial (base da restauração diária).
- **Done when**:
  - [ ] teste prova que cada persona tem 1 assinatura e pedidos nos 5 status, incluindo um entregue há exatamente 7 dias e um há 10 dias
  - [ ] teste prova que rodar o seed 2 vezes resulta na mesma contagem
  - [ ] `uv run pytest -q tests/integration/api/seed/test_commerce_seed.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não apagar conversas (restauração da demo pública)

---

### TASK-067 — Audit log recorder for side-effect tools

- **Requisito**: `TOOL-06`, `TOOL-07`, `TOOL-08`
- **Tipo**: crud-padrão
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-065
- **Arquivos de produção**:
  - `api/tools/audit.py`
- **Arquivos de teste**:
  - `tests/integration/api/tools/test_audit.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-39 — `api.tools.audit`: `record_audit(session, *, customer_id, tool, arguments, result, outcome, trace_id) -> AuditLog` e `list_successful_actions(session, trace_id) -> list[AuditLog]` (produz)
  - CT-38 — `api.tools.models` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Grava a auditoria com cliente, tool, argumentos, resultado, outcome, data/hora e `trace_id`. `list_successful_actions(trace_id)` alimenta o AGT-08 e o GRD-08.
- **Done when**:
  - [ ] teste prova que `record_audit` persiste todos os campos do TOOL-06
  - [ ] teste prova que `list_successful_actions` só devolve `outcome='success'` do trace informado
  - [ ] `uv run pytest -q tests/integration/api/tools/test_audit.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não decidir quando auditar (cada handler decide)

---

### TASK-068 — Tool registry with Pydantic schemas and session-bound context

- **Requisito**: `TOOL-02`, `TOOL-03`, `TOOL-12`, `TOOL-94`, `AGT-91`
- **Tipo**: lógica-negócio
- **Risco**: crítico
- **Âncora de risco**: AS-1 (identidade das tools só da sessão — `api/tools/registry.py`) — decisão humana LAC-39 (Jev c_auth p=0.46)
- **Perfil**: backend
- **Depende de**: TASK-064
- **Arquivos de produção**:
  - `api/tools/registry.py`
- **Arquivos de teste**:
  - `tests/unit/api/tools/test_registry.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-40 — `api.tools.registry`: `ToolDefinition(name, description, args_model: type[BaseModel], destructive: bool, handler)`, `ToolContext(customer_id: UUID, customer_email: str, trace_id: str, session: AsyncSession, language: Lang, now: datetime)`, `ToolResult(ok: bool, content: dict, error_code: str | None)`, `ToolRegistry.register(defn)`, `.specs() -> list[ToolSpec]`, `.is_destructive(name) -> bool`, `async .execute(call: ToolCall, ctx: ToolContext) -> ToolResult` (valida args; tool inexistente → `UNKNOWN_TOOL`; args inválidos → `INVALID_ARGUMENTS`) (produz)
  - CT-6 — `api.llm.client` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Registro de tools cujo JSON schema sai do mesmo `args_model` Pydantic que valida a chamada (TOOL-02). Os args models são `extra='forbid'`, e o registro rejeita, ao registrar, qualquer campo de identidade (`customer_id`, `user_id`, `email`, `to`, `recipient`) (TOOL-03, TOOL-18). A identidade só entra via `ToolContext`.
- **Done when**:
  - [ ] teste prova que `specs()` devolve o schema gerado por `args_model.model_json_schema()`
  - [ ] teste prova que registrar uma tool cujo args model tem `customer_id` levanta erro
  - [ ] teste prova que argumentos inválidos devolvem `ToolResult(ok=False, error_code='INVALID_ARGUMENTS')` sem chamar o handler (TOOL-94)
  - [ ] teste prova que uma tool inexistente devolve `UNKNOWN_TOOL` (AGT-91)
  - [ ] `uv run pytest -q tests/unit/api/tools/test_registry.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 4 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não implementar as tools de domínio

---

### TASK-069 — Order tools with owner scoping and atomic cancellation

- **Requisito**: `TOOL-04`, `TOOL-06`, `TOOL-10`, `TOOL-17`, `TOOL-19`, `TOOL-90`, `TOOL-91`, `TOOL-92`, `TOOL-99`, `DEMO-91`
- **Tipo**: lógica-negócio
- **Risco**: crítico
- **Âncora de risco**: AS-1 (escopo por cliente — `api/tools/orders.py`) — Jev c_auth p=0.87, a_regra_central p=0.72
- **Perfil**: backend
- **Depende de**: TASK-068, TASK-067
- **Arquivos de produção**:
  - `api/tools/orders.py`
- **Arquivos de teste**:
  - `tests/integration/api/tools/test_orders.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-42 — Handlers de tool `async (args, ctx: ToolContext) -> ToolResult`: `list_orders(ListOrdersArgs{limit: int = 10})`, `get_order(GetOrderArgs{order_id: int})`, `cancel_order(CancelOrderArgs{order_id: int})`, `check_refund_policy(CheckRefundArgs{order_id: int})`, `issue_refund(IssueRefundArgs{order_id: int})`, `get_subscription(EmptyArgs)`, `send_email(SendEmailArgs{subject: str, body: str})` — nenhum args model tem campo de cliente nem de destinatário (produz)
  - CT-38 — `api.tools.models` (assinatura completa: plan 8.2) (consome)
  - CT-39 — `api.tools.audit` (assinatura completa: plan 8.2) (consome)
  - CT-40 — `api.tools.registry` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: `list_orders`, `get_order` e `cancel_order`, sempre com `WHERE customer_id = ctx.customer_id`. Pedido inexistente ou alheio → mesmo `ORDER_NOT_FOUND` (TOOL-90). O cancelamento é um único `UPDATE ... WHERE id=:id AND customer_id=:c AND status IN ('pending','paid') RETURNING` (DA-21): 0 linhas → `ORDER_NOT_CANCELLABLE` (TOOL-91/99) e sucesso → auditoria. Valores saem como `{amount_cents, currency: 'USD'}`.
- **Done when**:
  - [ ] teste prova que a persona A não vê nem cancela pedido da persona B (resultado idêntico a id inexistente)
  - [ ] teste prova que pedido `shipped`, `delivered` e `cancelled` devolve `ORDER_NOT_CANCELLABLE` sem alterar o status
  - [ ] teste prova que 2 `cancel_order` concorrentes do mesmo pedido (`asyncio.gather`, sessões distintas) resultam em exatamente 1 sucesso e 1 `ORDER_NOT_CANCELLABLE`, com 1 linha de auditoria `success`
  - [ ] teste prova que `list_orders` não grava auditoria nem altera dados
  - [ ] `uv run pytest -q tests/integration/api/tools/test_orders.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 6 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não pedir confirmação aqui (o loop faz)
  - Não implementar reembolso

---

### TASK-070 — Refund eligibility policy

- **Requisito**: `TOOL-16`, `TOOL-98`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-065
- **Arquivos de produção**:
  - `api/tools/refund_policy.py`
- **Arquivos de teste**:
  - `tests/unit/api/tools/test_refund_policy.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-41 — `api.tools.refund_policy.evaluate_refund(order: Order, now: datetime) -> RefundEligibility(eligible: bool, reason_code: Literal["cancelled_before_shipment","delivered_within_7_days","delivered_over_7_days","not_delivered_yet","already_refunded"], amount_cents: int)` (produz)
  - CT-38 — `api.tools.models` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Função pura (LAC-06): elegível se `cancelled` sem `shipped_at`, ou se `delivered` com `now - delivered_at <= 7 dias` (inclusive). O valor é sempre o total integral do pedido.
- **Done when**:
  - [ ] teste prova elegível com entrega há exatamente 7 dias (7×24h) e não elegível com 7 dias + 1 segundo
  - [ ] teste prova elegível para cancelado antes do envio e não elegível para `paid`/`shipped`
  - [ ] teste prova `amount_cents == order.total_cents`
  - [ ] `uv run pytest -q tests/unit/api/tools/test_refund_policy.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 5 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não acessar banco

---

### TASK-071 — Refund tools with server-side policy enforcement

- **Requisito**: `TOOL-04`, `TOOL-07`, `TOOL-09`, `TOOL-15`, `TOOL-90`, `TOOL-93`, `DEMO-91`
- **Tipo**: lógica-negócio
- **Risco**: crítico
- **Âncora de risco**: AS-1 (escopo por cliente — `api/tools/refunds.py`) — Jev a_regra_central p=0.83; c_auth p=0.56 em dúvida com âncora → nível maior
- **Perfil**: backend
- **Depende de**: TASK-070, TASK-069
- **Arquivos de produção**:
  - `api/tools/refunds.py`
- **Arquivos de teste**:
  - `tests/integration/api/tools/test_refunds.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-41 — `api.tools.refund_policy.evaluate_refund(order: Order, now: datetime) -> RefundEligibility(eligible: bool, reason_code: Literal["cancelled_before_shipment","delivered_within_7_days","delivered_over_7_days","not_delivered_yet","already_refunded"], amount_cents: int)` (assinatura completa: plan 8.2) (consome)
  - CT-39 — `api.tools.audit` (assinatura completa: plan 8.2) (consome)
  - CT-40 — `api.tools.registry` (assinatura completa: plan 8.2) (consome)
  - CT-38 — `api.tools.models` (assinatura completa: plan 8.2) (consome)
  - CT-42 — `async (args, ctx: ToolContext) -> ToolResult` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: `check_refund_policy` devolve `{eligible, reason_code, amount_cents, currency}`. `issue_refund` reavalia a política no servidor (LAC-05) com o pedido travado (`SELECT ... FOR UPDATE`) e insere o reembolso. Conflito de unicidade → `ALREADY_REFUNDED` (TOOL-93). Não elegível → `REFUND_NOT_ELIGIBLE`, sem inserir.
- **Done when**:
  - [ ] teste prova que `issue_refund` de pedido não elegível devolve erro e não cria linha, mesmo sem `check_refund_policy` antes
  - [ ] teste prova que 2 `issue_refund` concorrentes do mesmo pedido criam exatamente 1 reembolso
  - [ ] teste prova que pedido de outra persona devolve `ORDER_NOT_FOUND`
  - [ ] teste prova auditoria `success` com `trace_id` no reembolso emitido
  - [ ] `uv run pytest -q tests/integration/api/tools/test_refunds.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 5 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não integrar gateway de pagamento (fora de escopo)

---

### TASK-072 — Subscription lookup and fake email tools

- **Requisito**: `TOOL-04`, `TOOL-08`, `TOOL-10`, `TOOL-18`
- **Tipo**: lógica-negócio
- **Risco**: crítico
- **Âncora de risco**: AS-1 (escopo por cliente e destinatário fixo — `api/tools/subscriptions.py`, `api/tools/email.py`) — decisão humana LAC-39 (Jev c_auth p=0.37)
- **Perfil**: backend
- **Depende de**: TASK-068, TASK-067
- **Arquivos de produção**:
  - `api/tools/subscriptions.py`
  - `api/tools/email.py`
- **Arquivos de teste**:
  - `tests/integration/api/tools/test_subscriptions_email.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-40 — `api.tools.registry` (assinatura completa: plan 8.2) (consome)
  - CT-39 — `api.tools.audit` (assinatura completa: plan 8.2) (consome)
  - CT-38 — `api.tools.models` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: `get_subscription` só lê a assinatura do cliente da sessão. `send_email` grava em `email_outbox` com `recipient = ctx.customer_email` (o schema não tem destinatário, TOOL-18), audita e não envia nada.
- **Done when**:
  - [ ] teste prova que `send_email` grava o destinatário igual ao e-mail da persona da sessão e 1 linha de auditoria
  - [ ] teste prova que o schema de `send_email` não tem propriedade de destinatário
  - [ ] teste prova que `get_subscription` não grava auditoria
  - [ ] `uv run pytest -q tests/integration/api/tools/test_subscriptions_email.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não enviar e-mail real (fora de escopo)

---

### TASK-073 — Default registry with the seven tools

- **Requisito**: `TOOL-02`, `TOOL-12`
- **Tipo**: config
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-071, TASK-072
- **Arquivos de produção**:
  - `api/tools/catalog.py`
- **Arquivos de teste**:
  - `tests/unit/api/tools/test_catalog.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-43 — `api.tools.catalog.build_default_registry() -> ToolRegistry` (exatamente as 7 tools; `destructive` só em `cancel_order` e `issue_refund`) (produz)
  - CT-40 — `api.tools.registry` (assinatura completa: plan 8.2) (consome)
  - CT-42 — `async (args, ctx: ToolContext) -> ToolResult` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Monta o registro com exatamente as 7 tools e as descrições usadas pelo modelo. `destructive=True` só em `cancel_order` e `issue_refund` (LAC-03).
- **Done when**:
  - [ ] teste prova que os nomes são exatamente o conjunto das 7 tools
  - [ ] teste prova `is_destructive` verdadeiro só para `cancel_order` e `issue_refund`
  - [ ] `uv run pytest -q tests/unit/api/tools/test_catalog.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não adicionar tools extras (fora de escopo)

---

### TASK-074 — Pending confirmation schema

- **Requisito**: `TOOL-05`, `TOOL-96`
- **Tipo**: migration
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-065
- **Arquivos de produção**:
  - `api/agent/models.py`
  - `api/db/migrations/versions/0008_pending_confirmations.py`
- **Arquivos de teste**:
  - `tests/integration/api/agent/test_confirmation_models.py`
- **Wiring permitido**:
  - `api/agent/__init__.py` (criar vazio)
  - `api/db/migrations/env.py` (apenas importar `api.agent.models`)
- **Reusa**: —
- **Contrato**:
  - CT-5 — `api.db.engine` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Tabela `pending_confirmations` (CT-44) com estado serializado do loop (`agent_state` JSONB) e índice `(conversation_id, status)`.
- **Done when**:
  - [ ] teste prova `upgrade`/`downgrade` e CHECK do status
  - [ ] `uv run pytest -q tests/integration/api/agent/test_confirmation_models.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não implementar a lógica (task do store)

---

### TASK-075 — Confirmation store with owner checks and expiry

- **Requisito**: `TOOL-05`, `TOOL-13`, `TOOL-96`, `TOOL-97`
- **Tipo**: lógica-negócio
- **Risco**: crítico
- **Âncora de risco**: AS-1 (confirmação só do dono — `api/agent/confirmations.py`) — Jev c_auth p=0.75
- **Perfil**: backend
- **Depende de**: TASK-074
- **Arquivos de produção**:
  - `api/agent/confirmations.py`
- **Arquivos de teste**:
  - `tests/integration/api/agent/test_confirmations.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-44 — ORM `api.agent.models.PendingConfirmation(id UUID, conversation_id, customer_id, tool, arguments JSONB, agent_state JSONB, status: pending|confirmed|refused|expired, trace_id, created_at, resolved_at)`; `api.agent.confirmations.ConfirmationStore(session)`: `create(...) -> PendingConfirmation`, `resolve(confirmation_id, customer_id, decision: Literal["confirm","refuse"]) -> PendingConfirmation` (404 igual para inexistente/alheia; 409 se não está pending/expirada), `refuse_pending(conversation_id) -> list[PendingConfirmation]`, `expire_stale(now) -> int` (produz)
  - CT-3 — `api.errors` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Transições do spec (seção 7): a resolução é um `UPDATE ... WHERE id AND customer_id AND status='pending' AND created_at > now - ttl RETURNING`, então dupla confirmação, confirmação alheia ou expirada nunca executa (TOOL-96). `refuse_pending(conversation_id)` atende o TOOL-97 e `expire_stale` marca `expired`.
- **Done when**:
  - [ ] teste prova que confirmar duas vezes devolve 409 na segunda
  - [ ] teste prova que confirmação de outra persona devolve 404 igual a inexistente
  - [ ] teste prova que confirmação com mais de `confirmation_ttl_minutes` não resolve como `confirmed`
  - [ ] teste prova que `refuse_pending` marca `refused` todas as pendentes da conversa
  - [ ] `uv run pytest -q tests/integration/api/agent/test_confirmations.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 4 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não executar tools aqui

---

### TASK-076 — Agent loop with tool execution and destructive-tool suspension

- **Requisito**: `AGT-01`, `AGT-04`, `AGT-90`, `AGT-91`, `TOOL-05`, `TOOL-12`, `TOOL-13`, `TOOL-14`
- **Tipo**: lógica-negócio
- **Risco**: crítico
- **Âncora de risco**: AS-1 (tool destrutiva só após confirmação — `api/agent/loop.py`) — Jev a_regra_central p=0.77; c_auth p=0.38 em dúvida com âncora → nível maior
- **Perfil**: backend
- **Depende de**: TASK-073, TASK-075
- **Arquivos de produção**:
  - `api/agent/loop.py`
  - `api/agent/events.py`
- **Arquivos de teste**:
  - `tests/unit/api/agent/test_loop.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-45 — `api.agent.events.AgentEvent` (união discriminada por `type`: `token{text}`, `tool_start{call_id, tool, label_key}`, `tool_end{call_id, tool, ok}`, `citation{index, document, section, page}`, `done{status, message_id, conversation_id, trace_id, confirmation?}`, `error{code, message, trace_id}`); `api.agent.loop.AgentLoop(llm, registry, settings, audit_reader)`: `run(turn: AgentTurn) -> AsyncIterator[AgentEvent]` e `resume(state: AgentState, decision: Literal["confirm","refuse"], ctx: ToolContext) -> AsyncIterator[AgentEvent]`; `AgentState` serializável em JSON (mensagens, passos, custo, chamadas recusadas, histórico de repetição) (produz)
  - CT-6 — `api.llm.client` (assinatura completa: plan 8.2) (consome)
  - CT-40 — `api.tools.registry` (assinatura completa: plan 8.2) (consome)
  - CT-44 — `api.agent.models.PendingConfirmation(id UUID, conversation_id, customer_id, tool, arguments JSONB, agent_state JSONB, status: pending|confirmed|refused|expired, trace_id, created_at, resolved_at)` (assinatura completa: plan 8.2) (consome)
  - CT-31 — `api.chat.prompt_builder.build_request(*, language: Lang, history: HistoryWindow, chunks: list[RetrievedChunk], user_message: str, tools: list[ToolSpec], layout: Literal["naive","cache_ordered"]) -> LLMRequest` (assinatura completa: plan 8.2) (consome)
  - CT-39 — `api.tools.audit` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Loop modelo → tool → resultado → modelo até a resposta final (DA-20). Erro de tool volta como resultado (AGT-04). Tool destrutiva: o loop serializa o `AgentState`, emite `done{status:'awaiting_confirmation'}` e para. `resume` executa (confirm) ou devolve 'customer_refused' como resultado da tool (refuse) e continua. Uma chamada recusada com os mesmos argumentos dentro da mesma requisição volta como erro `ACTION_ALREADY_REFUSED`, sem nova confirmação (TOOL-14). Os limites entram na fase 7: aqui só há um teto fixo de segurança de 8 passos.
- **Done when**:
  - [ ] teste com fake e registro falso prova a sequência de eventos `tool_start`/`tool_end` para uma tool não destrutiva e a resposta final
  - [ ] teste prova que `cancel_order` gera `done` com `awaiting_confirmation` e que o handler NÃO foi chamado
  - [ ] teste prova que `resume(..., 'refuse')` envia ao modelo um resultado de tool com 'customer_refused' e que uma nova solicitação idêntica devolve `ACTION_ALREADY_REFUSED` sem criar confirmação
  - [ ] teste prova que o modelo respondendo sem tool termina em 1 passo (AGT-90)
  - [ ] `uv run pytest -q tests/unit/api/agent/test_loop.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 6 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não implementar limite configurável de passos/custo nem detector de repetição (fase 7)
  - Não emitir tokens em streaming (fase 9)

---

### TASK-077 — Chat service driven by the agent loop with confirmations

- **Requisito**: `TOOL-05`, `TOOL-11`, `TOOL-13`, `TOOL-96`, `TOOL-97`
- **Tipo**: lógica-negócio
- **Risco**: alto
- **Âncora de risco**: AS-3 (`api/chat/service.py`) — regra central de ações com confirmação, Jev a_regra_central p=0.77
- **Perfil**: backend
- **Depende de**: TASK-076, TASK-066
- **Arquivos de produção**:
  - `api/chat/service.py`
- **Arquivos de teste**:
  - `tests/integration/api/chat/test_service_tools.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-15 — `api.chat.service.ChatService.handle_message(request: ChatRequest, principal: CurrentCustomer | None) -> AsyncIterator[AgentEvent]` (assinatura completa: plan 8.2) (consome)
  - CT-45 — `api.agent.events.AgentEvent` (assinatura completa: plan 8.2) (consome)
  - CT-44 — `api.agent.models.PendingConfirmation(id UUID, conversation_id, customer_id, tool, arguments JSONB, agent_state JSONB, status: pending|confirmed|refused|expired, trace_id, created_at, resolved_at)` (assinatura completa: plan 8.2) (consome)
  - CT-43 — `api.tools.catalog.build_default_registry() -> ToolRegistry` (assinatura completa: plan 8.2) (consome)
  - CT-20 — `api.chat.repository.ConversationRepository(session)` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: O `ChatService` passa a rodar o `AgentLoop` com o `ToolContext` da sessão. Mensagem nova numa conversa com confirmação pendente: `refuse_pending` e a recusa entra no histórico (TOOL-97). Ganha `resolve_confirmation(confirmation_id, decision, customer)`, que retoma o loop pelo estado salvo.
- **Done when**:
  - [ ] teste de ponta a ponta com fake: 'cancela meu pedido' → `awaiting_confirmation` → confirm → pedido `cancelled` no banco e auditoria existente (TOOL-11)
  - [ ] teste prova que, com confirmação pendente, uma nova mensagem marca a pendente como `refused` e o pedido continua inalterado
  - [ ] `uv run pytest -q tests/integration/api/chat/test_service_tools.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não alterar o loop

---

### TASK-078 — Confirmation endpoint

- **Requisito**: `TOOL-05`, `TOOL-13`
- **Tipo**: crud-padrão
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-077
- **Arquivos de produção**:
  - `api/chat/routes.py`
- **Arquivos de teste**:
  - `tests/integration/api/chat/test_confirmation_route.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-46 — HTTP `POST /chat/confirmations/{confirmation_id}` body `{decision: "confirm"|"refuse"}` → 200 `ChatResponse` (CT‑14); 404 `CONFIRMATION_NOT_FOUND`; 409 `CONFIRMATION_NOT_PENDING` (produz)
  - CT-15 — `api.chat.service.ChatService.handle_message(request: ChatRequest, principal: CurrentCustomer | None) -> AsyncIterator[AgentEvent]` (assinatura completa: plan 8.2) (consome)
  - CT-16 — `api.auth.deps` (assinatura completa: plan 8.2) (consome)
  - CT-44 — `api.agent.models.PendingConfirmation(id UUID, conversation_id, customer_id, tool, arguments JSONB, agent_state JSONB, status: pending|confirmed|refused|expired, trace_id, created_at, resolved_at)` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: `POST /chat/confirmations/{id}` (CT-46), exigindo `require_customer`.
- **Done when**:
  - [ ] teste prova 200 com `status='completed'` após confirm, 404 para confirmação alheia e 409 na segunda chamada
  - [ ] `uv run pytest -q tests/integration/api/chat/test_confirmation_route.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não criar SSE (fase 9)

---

### TASK-079 — Web confirmation card and USD money pipe

- **Requisito**: `TOOL-05`, `TOOL-19`
- **Tipo**: ui-puro
- **Risco**: médio
- **Perfil**: frontend
- **Depende de**: TASK-064
- **Arquivos de produção**:
  - `web/src/app/features/chat/confirmation-card.component.ts`
  - `web/src/app/shared/money.pipe.ts`
- **Arquivos de teste**:
  - `web/src/app/features/chat/confirmation-card.component.spec.ts`
- **Wiring permitido**:
  - `web/src/app/core/i18n/messages.ts` (apenas chaves `confirm.*` e rótulos das tools)
  - `web/src/app/app.config.ts` (apenas `registerLocaleData(localePt)`)
- **Reusa**: —
- **Contrato**:
  - CT-24 — `I18nService` (assinatura completa: plan 8.2) (consome)
  - CT-26 — `ChatApiService` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Card "Confirmar: <ação> <alvo>?" com botões confirmar/recusar, que emite `decision`. Template inline (componente pequeno). O `MoneyPipe` formata centavos USD por idioma (`en-US` → `$12.50`; `pt-BR` → `US$ 12,50`) com `Intl.NumberFormat` (LAC-30).
- **Done when**:
  - [ ] teste prova o texto do card em pt e en para `cancel_order` do pedido #123
  - [ ] teste prova que clicar em recusar emite `'refuse'`
  - [ ] teste prova `money(1250,'en')` = '$12.50' e `money(1250,'pt')` contendo '12,50' e 'US$'
  - [ ] `npx ng test --watch=false --include=src/app/features/chat/confirmation-card.component.spec.ts` (executar em `web`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 4 testes, todos passando
  - [ ] `npx ng lint` e `npx tsc --noEmit -p tsconfig.app.json` (executar em `web`) sem erro novo nos arquivos da task
- **Não fazer**:
  - Não chamar a API no card (a página chama)

---

### TASK-080 — Chat page confirmation flow

- **Requisito**: `TOOL-05`, `TOOL-13`
- **Tipo**: ui-puro
- **Risco**: médio
- **Perfil**: frontend
- **Depende de**: TASK-079, TASK-078
- **Arquivos de produção**:
  - `web/src/app/features/chat/chat.page.ts`
  - `web/src/app/features/chat/chat.page.html`
- **Arquivos de teste**:
  - `web/src/app/features/chat/chat.page.confirm.spec.ts`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-26 — `ChatApiService` (assinatura completa: plan 8.2) (consome)
  - CT-46 — `POST /chat/confirmations/{confirmation_id}` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Quando `status === 'awaiting_confirmation'`, a página mostra o card e bloqueia o campo de envio até a decisão. Depois chama `confirm()` e renderiza a resposta seguinte.
- **Done when**:
  - [ ] teste prova que a resposta `awaiting_confirmation` exibe o card e que confirmar chama `ChatApiService.confirm(id,'confirm')`
  - [ ] `npx ng test --watch=false --include=src/app/features/chat/chat.page.confirm.spec.ts` (executar em `web`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `npx ng lint` e `npx tsc --noEmit -p tsconfig.app.json` (executar em `web`) sem erro novo nos arquivos da task
- **Não fazer**:
  - Não implementar streaming

---

### TASK-081 — Phase 06 tool calling demo

- **Requisito**: `TOOL-11`, `DOD-02`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-077
- **Arquivos de produção**:
  - `demos/demo_06_tools.py`
- **Arquivos de teste**:
  - `tests/integration/demos/test_demo_06.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-12 — `demos.common` (assinatura completa: plan 8.2) (consome)
  - CT-15 — `api.chat.service.ChatService.handle_message(request: ChatRequest, principal: CurrentCustomer | None) -> AsyncIterator[AgentEvent]` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Autentica como persona, envia 'cancela meu pedido #N' / 'cancel my order #N', confirma, consulta o banco e a auditoria e registra o resultado. O 'antes' é a mesma pergunta com `ATLAS_AGENT_TOOLS_ENABLED=false`, que só gera texto.
- **Done when**:
  - [ ] teste com `--provider fake` prova pedido `cancelled` e linha de auditoria criada, e a linha `| 06 |`
  - [ ] `uv run pytest -q tests/integration/demos/test_demo_06.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não usar multi-step

---

### TASK-082 — Phase 06 ADR and eval cases

- **Requisito**: `DOD-01`, `DOD-04`
- **Tipo**: config
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-081, TASK-080
- **Arquivos de produção**:
  - `docs/adr/0006-tool-calling.md`
  - `evals/datasets/phase_06.jsonl`
- **Arquivos de teste**:
  - `tests/unit/evals/test_phase_06_cases.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: ADR (identidade só da sessão, confirmação na UI, política no servidor) e casos `tool_trajectory` em pt e en.
- **Done when**:
  - [ ] teste prova ≥ 2 casos `phase == 6` de categoria `tool_trajectory`, um pt e um en
  - [ ] `uv run pytest -q tests/unit/evals/test_phase_06_cases.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
- **Não fazer**:
  - Não criar a tag

---

## Fase 7 — Agente

### TASK-083 — Agent step, cost and repeated-call guards

- **Requisito**: `AGT-02`, `AGT-03`, `AGT-05`, `AGT-09`, `AGT-10`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-082
- **Arquivos de produção**:
  - `api/agent/guards.py`
- **Arquivos de teste**:
  - `tests/unit/api/agent/test_guards.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-47 — `api.agent.guards`: `StepBudget(max_steps).consume() -> bool`, `CostBudget(max_usd).add(cost) -> bool`, `RepeatDetector.check(call: ToolCall) -> Literal["ok","repeat_error","terminate"]`, `limit_reason` em `step_limit|cost_limit|repeat_limit` (produz)
  - CT-6 — `api.llm.client` (assinatura completa: plan 8.2) (consome)
  - CT-7 — `api.llm.pricing.estimate_cost(model: str, usage: Usage) -> Decimal` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Orçamentos por requisição: passos (`ATLAS_AGENT_MAX_STEPS`, padrão 8) e custo (`ATLAS_AGENT_MAX_COST_USD`, padrão 0,25, premissa P-3). O detector de repetição compara `(tool, arguments canônicos JSON)` com a chamada imediatamente anterior: 2ª igual seguida → `repeat_error`; 3ª → `terminate` (LAC-21).
- **Done when**:
  - [ ] teste prova que `StepBudget(8)` permite 8 consumos e nega o 9º
  - [ ] teste prova que `CostBudget` nega ao atingir o limite
  - [ ] teste prova `ok`, `repeat_error`, `terminate` para 3 chamadas idênticas seguidas e `ok` quando os argumentos mudam
  - [ ] `uv run pytest -q tests/unit/api/agent/test_guards.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 4 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não alterar o loop (task seguinte)

---

### TASK-084 — Agent loop limits, repeat handling and limit termination message

- **Requisito**: `AGT-02`, `AGT-03`, `AGT-05`, `AGT-06`, `AGT-08`, `AGT-09`, `AGT-10`
- **Tipo**: lógica-negócio
- **Risco**: crítico
- **Âncora de risco**: AS-2 (limite de custo real por requisição — `api/agent/loop.py`) — Jev c_dinheiro p=0.78
- **Perfil**: backend
- **Depende de**: TASK-083
- **Arquivos de produção**:
  - `api/agent/loop.py`
- **Arquivos de teste**:
  - `tests/unit/api/agent/test_loop_limits.py`
  - `tests/integration/api/agent/test_multistep_trajectory.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-45 — `api.agent.events.AgentEvent` (assinatura completa: plan 8.2) (consome)
  - CT-47 — `api.agent.guards` (assinatura completa: plan 8.2) (consome)
  - CT-39 — `api.tools.audit` (assinatura completa: plan 8.2) (consome)
  - CT-4 — `api.messages.user_message(key: MessageKey, lang: Lang, **params: object) -> str` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Integra os guards. Ao atingir passo/custo/3ª repetição, encerra sem nova chamada ao modelo, responde com `AGENT_LIMIT_REACHED` (pt/en) listando as ações bem-sucedidas da auditoria do `trace_id` e registra `agent_limit` com o motivo no log (AGT-08). A detecção de repetição gera o log `agent_repeat_detected` (AGT-05).
- **Done when**:
  - [ ] teste prova que, com max_steps=2 e o modelo pedindo tools sem parar, o loop faz exatamente 2 passos e a mensagem lista as ações auditadas
  - [ ] teste prova que a 2ª chamada idêntica volta ao modelo como erro 'repeated call' e a 3ª encerra
  - [ ] teste de integração com fake roteirizado e confirmações prova a trajetória exata `list_orders → get_order → check_refund_policy → cancel_order → issue_refund → send_email` (AGT-06)
  - [ ] `uv run pytest -q tests/unit/api/agent/test_loop_limits.py tests/integration/api/agent/test_multistep_trajectory.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 5 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não mudar a semântica de confirmação da fase 6

---

### TASK-085 — Phase 07 multi-step agent benchmark demo

- **Requisito**: `AGT-06`, `AGT-07`, `DOD-02`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-084
- **Arquivos de produção**:
  - `demos/demo_07_agent.py`
  - `demos/data/phase07_tasks.jsonl`
- **Arquivos de teste**:
  - `tests/integration/demos/test_demo_07.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-12 — `demos.common` (assinatura completa: plan 8.2) (consome)
  - CT-15 — `api.chat.service.ChatService.handle_message(request: ChatRequest, principal: CurrentCustomer | None) -> AsyncIterator[AgentEvent]` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Roda as 15 tarefas multi-step (pt e en, com a trajetória esperada, confirmando automaticamente as destrutivas) e registra a taxa de sucesso e a média de passos. O 'antes' usa `ATLAS_AGENT_MAX_STEPS=1`.
- **Done when**:
  - [ ] `phase07_tasks.jsonl` tem exatamente 15 tarefas, com pt e en (verificado no teste)
  - [ ] teste com `--provider fake` prova a linha `| 07 |` com taxa de sucesso e média de passos
  - [ ] `uv run pytest -q tests/integration/demos/test_demo_07.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não alterar o loop

---

### TASK-086 — Phase 07 ADR and eval cases

- **Requisito**: `DOD-01`, `DOD-04`
- **Tipo**: config
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-085
- **Arquivos de produção**:
  - `docs/adr/0007-agent-loop.md`
  - `evals/datasets/phase_07.jsonl`
- **Arquivos de teste**:
  - `tests/unit/evals/test_phase_07_cases.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: ADR (loop próprio, limites, repetição) e casos de trajetória multi-step.
- **Done when**:
  - [ ] teste prova ≥ 2 casos `phase == 7` `tool_trajectory` com `expected.trajectory` de 6 tools, pt e en
  - [ ] `uv run pytest -q tests/unit/evals/test_phase_07_cases.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
- **Não fazer**:
  - Não criar a tag

---

## Fase 8 — Structured output

### TASK-087 — Triage schemas and prompt

- **Requisito**: `TRI-01`, `TRI-05`, `TRI-06`, `TRI-08`, `TRI-90`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-086
- **Arquivos de produção**:
  - `api/triage/schemas.py`
  - `prompts/triage.md`
- **Arquivos de teste**:
  - `tests/unit/api/triage/test_schemas.py`
- **Wiring permitido**:
  - `api/triage/__init__.py` (criar vazio)
- **Reusa**: —
- **Contrato**:
  - CT-48 — `api.triage.schemas`: `TriageRequest{text: str (1..ATLAS_MAX_MESSAGE_CHARS)}` (extra=forbid), `TriageResult{categoria: Literal[pedido,assinatura,reembolso,entrega,conta,outro], prioridade: Literal[baixa,media,alta,urgente], sentimento: Literal[positivo,neutro,negativo], precisa_humano: bool, resumo: str}` (extra=forbid) (produz)
  - CT-13 — `api.prompts.load_prompt(name: str) -> str` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Schemas do CT-48 com `extra='forbid'` e enums fechados (LAC-11). O prompt pede o `resumo` no idioma da entrada (TRI-08) e os enums sempre com os valores fixos.
- **Done when**:
  - [ ] teste prova que um objeto com campo extra ou `categoria='billing'` é rejeitado
  - [ ] teste prova que `TriageRequest(text='')` e só espaços são rejeitados
  - [ ] teste prova que o JSON schema gerado tem exatamente os 5 campos obrigatórios
  - [ ] `uv run pytest -q tests/unit/api/triage/test_schemas.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não aceitar `conversation_id` (TRI-05)

---

### TASK-088 — Triage service with schema enforcement and repair retries

- **Requisito**: `TRI-02`, `TRI-03`, `TRI-07`, `TRI-08`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-087
- **Arquivos de produção**:
  - `api/triage/service.py`
- **Arquivos de teste**:
  - `tests/unit/api/triage/test_service.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-74 — `api.triage.service.TriageService(llm: LLMClient, settings)`: `async classify(text: str, mode: Literal["prompt_only","schema"] | None = None) -> TriageResult` (levanta `AtlasError(TRIAGE_UNAVAILABLE, 503)`) (produz)
  - CT-48 — `api.triage.schemas` (assinatura completa: plan 8.2) (consome)
  - CT-6 — `api.llm.client` (assinatura completa: plan 8.2) (consome)
  - CT-9 — `api.llm.instrumented.InstrumentedLLMClient(inner: LLMClient, model: str, provider: str)` (assinatura completa: plan 8.2) (consome)
  - CT-3 — `api.errors` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Modo `schema`: `response_schema` nativo (TRI-02). Modo `prompt_only`: só instrução, usado no 'antes' do demo_08. Valida com Pydantic e, se falhar, repete até `triage_max_repairs` (2) vezes, incluindo o erro de validação no prompt (TRI-03). Esgotado → `TRIAGE_UNAVAILABLE` + log `triage_failed` (TRI-07).
- **Done when**:
  - [ ] teste prova que 1 resposta inválida seguida de 1 válida resulta em sucesso com 2 chamadas e que o 2º request contém o texto do erro de validação
  - [ ] teste prova que 3 respostas inválidas resultam em `TRIAGE_UNAVAILABLE` após exatamente 3 chamadas
  - [ ] teste prova que o modo `schema` envia `response_schema` e o `prompt_only` não
  - [ ] `uv run pytest -q tests/unit/api/triage/test_service.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não criar rota

---

### TASK-089 — Triage endpoint for personas and operator key

- **Requisito**: `TRI-01`, `TRI-05`, `TRI-07`, `TRI-90`
- **Tipo**: crud-padrão
- **Risco**: crítico
- **Âncora de risco**: AS-1 (acesso persona OU operador — `api/triage/routes.py`) — acima do teto de crud-padrão porque decide autorização (Jev c_auth p=0.83)
- **Perfil**: backend
- **Depende de**: TASK-088
- **Arquivos de produção**:
  - `api/triage/routes.py`
- **Arquivos de teste**:
  - `tests/integration/api/triage/test_triage_route.py`
- **Wiring permitido**:
  - `api/main.py` (apenas `include_router(triage_router)`)
- **Reusa**: —
- **Contrato**:
  - CT-49 — HTTP `POST /triage` body `TriageRequest` → 200 `TriageResult`; 422 `VALIDATION_ERROR`; 401 `AUTH_REQUIRED`; 503 `TRIAGE_UNAVAILABLE`; acesso: sessão de persona OU `X-Operator-Key` (LAC-35) (produz)
  - CT-48 — `api.triage.schemas` (assinatura completa: plan 8.2) (consome)
  - CT-74 — `api.triage.service.TriageService(llm: LLMClient, settings)` (assinatura completa: plan 8.2) (consome)
  - CT-16 — `api.auth.deps` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: `POST /triage` com `require_customer_or_operator` (LAC-35). Sem sessão e sem chave → 401 sem chamar o LLM. Entrada vazia → 422 sem chamar o LLM (TRI-90).
- **Done when**:
  - [ ] teste prova 200 com cookie de persona e 200 com `X-Operator-Key` correto
  - [ ] teste prova 401 sem credenciais e com chave errada, e 0 chamadas no fake
  - [ ] teste prova 422 para `{"text": ""}` e para `{"conversation_id": "..."}`
  - [ ] teste prova 503 `TRIAGE_UNAVAILABLE` sem objeto de triagem no corpo
  - [ ] `uv run pytest -q tests/integration/api/triage/test_triage_route.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 5 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não aplicar rate limit/teto ainda (demo pública)

---

### TASK-090 — Phase 08 structured output demo

- **Requisito**: `TRI-04`, `DOD-02`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-088
- **Arquivos de produção**:
  - `demos/demo_08_triage.py`
  - `demos/data/phase08_inputs.jsonl`
- **Arquivos de teste**:
  - `tests/unit/demos/test_demo_08.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-12 — `demos.common` (assinatura completa: plan 8.2) (consome)
  - CT-74 — `api.triage.service.TriageService(llm: LLMClient, settings)` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: 200 chamadas (`--n`) em cada modo (`prompt_only` e `schema`) sobre entradas pt/en e o percentual de JSON válido de cada um em `docs/results.md`.
- **Done when**:
  - [ ] teste com `--provider fake --n 10` prova a linha `| 08 |` com dois percentuais
  - [ ] `uv run pytest -q tests/unit/demos/test_demo_08.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não chamar o endpoint HTTP (usa o serviço direto)

---

### TASK-091 — Phase 08 ADR and eval cases

- **Requisito**: `DOD-01`, `DOD-04`
- **Tipo**: config
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-090, TASK-089
- **Arquivos de produção**:
  - `docs/adr/0008-structured-output.md`
  - `evals/datasets/phase_08.jsonl`
- **Arquivos de teste**:
  - `tests/unit/evals/test_phase_08_cases.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: ADR (schema nativo + validação + reparo) e casos `json_valid` pt/en.
- **Done when**:
  - [ ] teste prova ≥ 2 casos `phase == 8` `json_valid`, pt e en
  - [ ] `uv run pytest -q tests/unit/evals/test_phase_08_cases.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
- **Não fazer**:
  - Não criar a tag

---

## Fase 9 — Streaming

### TASK-092 — Anthropic adapter streaming support

- **Requisito**: `STR-01`
- **Tipo**: integração-externa
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-091
- **Arquivos de produção**:
  - `api/llm/anthropic_adapter.py`
- **Arquivos de teste**:
  - `tests/unit/api/llm/test_anthropic_stream.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-6 — `api.llm.client` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Implementa `stream()` sobre o streaming de eventos do SDK, convertendo deltas de texto, blocos de tool (argumentos acumulados) e uso final em `LLMStreamEvent`.
- **Done when**:
  - [ ] teste com MockTransport servindo um stream SSE gravado prova a sequência `text_delta`* → `tool_call` → `usage` → `stop`
  - [ ] teste prova que erro HTTP antes do 1º evento vira `LLMError`
  - [ ] `uv run pytest -q tests/unit/api/llm/test_anthropic_stream.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não implementar fallback

---

### TASK-093 — SSE event encoder

- **Requisito**: `STR-01`, `STR-05`, `STR-90`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-091
- **Arquivos de produção**:
  - `api/streaming/events.py`
- **Arquivos de teste**:
  - `tests/unit/api/streaming/test_events.py`
- **Wiring permitido**:
  - `api/streaming/__init__.py` (criar vazio)
- **Reusa**: —
- **Contrato**:
  - CT-50 — `api.streaming.events.encode_sse(event: AgentEvent) -> bytes` (`event: <type>\ndata: <json>\n\n`) e `guard_terminal(events: AsyncIterator[AgentEvent]) -> AsyncIterator[AgentEvent]` (`done` único e último; nada após `error`) (produz)
  - CT-45 — `api.agent.events.AgentEvent` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Serializa `AgentEvent` em `event: <type>\ndata: <json>\n\n`. Também fornece `guard_terminal(events)`, que garante `done` exatamente uma vez como último evento e nada depois de `error`.
- **Done when**:
  - [ ] teste prova o formato byte a byte de `token` e de `done`
  - [ ] teste prova que `guard_terminal` descarta eventos após `error` e não emite `done` depois dele
  - [ ] `uv run pytest -q tests/unit/api/streaming/test_events.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não criar rota

---

### TASK-094 — Streaming agent loop with cancellation persistence

- **Requisito**: `STR-01`, `STR-03`, `STR-04`, `STR-05`, `STR-07`, `STR-91`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-092, TASK-093
- **Arquivos de produção**:
  - `api/agent/loop.py`
  - `api/chat/service.py`
- **Arquivos de teste**:
  - `tests/integration/api/chat/test_streaming_cancel.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-45 — `api.agent.events.AgentEvent` (assinatura completa: plan 8.2) (consome)
  - CT-6 — `api.llm.client` (assinatura completa: plan 8.2) (consome)
  - CT-20 — `api.chat.repository.ConversationRepository(session)` (assinatura completa: plan 8.2) (consome)
  - CT-15 — `api.chat.service.ChatService.handle_message(request: ChatRequest, principal: CurrentCustomer | None) -> AsyncIterator[AgentEvent]` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: O loop usa `llm.stream()` e emite `token` por delta, `tool_start`/`tool_end` com `label_key` por tool (STR-02) e `citation` por marcador ao final. Ao receber `CancelledError` (cancelamento ou desconexão), o serviço persiste a resposta parcial com `status='cancelled'` (entra no histórico) e mantém as tools já executadas (LAC-16).
- **Done when**:
  - [ ] teste prova a ordem `tool_start, tool_end, token+, citation, done` para um turno com 1 tool e 1 citação
  - [ ] teste cancela a task do gerador após 3 tokens e prova uma mensagem `cancelled` com o texto parcial e a auditoria da tool já executada intacta
  - [ ] `uv run pytest -q tests/integration/api/chat/test_streaming_cancel.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não aplicar guardrail de saída (fase 10)

---

### TASK-095 — Streaming chat and confirmation SSE endpoints

- **Requisito**: `STR-01`, `STR-04`, `STR-05`, `STR-90`, `STR-91`
- **Tipo**: crud-padrão
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-094
- **Arquivos de produção**:
  - `api/chat/stream_routes.py`
- **Arquivos de teste**:
  - `tests/integration/api/chat/test_stream_routes.py`
- **Wiring permitido**:
  - `api/main.py` (apenas `include_router(stream_router)`)
- **Reusa**: —
- **Contrato**:
  - CT-76 — HTTP `POST /chat/stream` (body CT‑14) e `POST /chat/confirmations/{id}/stream` (body CT‑46) → 200 `text/event-stream` com os eventos de CT‑45 (`event: token|tool_start|tool_end|citation|done|error`); 401 antes de abrir o stream (produz)
  - CT-50 — `api.streaming.events.encode_sse(event: AgentEvent) -> bytes` (assinatura completa: plan 8.2) (consome)
  - CT-45 — `api.agent.events.AgentEvent` (assinatura completa: plan 8.2) (consome)
  - CT-15 — `api.chat.service.ChatService.handle_message(request: ChatRequest, principal: CurrentCustomer | None) -> AsyncIterator[AgentEvent]` (assinatura completa: plan 8.2) (consome)
  - CT-16 — `api.auth.deps` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: `StreamingResponse` `text/event-stream` com `Cache-Control: no-cache` e `X-Accel-Buffering: no`. Verifica `request.is_disconnected()` entre eventos e cancela a geração (STR-91). Exceção → evento `error` e fim do stream (STR-90).
- **Done when**:
  - [ ] teste prova `Content-Type: text/event-stream` e último evento `done` único
  - [ ] teste prova que exceção simulada gera `event: error` sem `done` depois
  - [ ] teste prova 401 sem sessão antes de abrir o stream
  - [ ] `uv run pytest -q tests/integration/api/chat/test_stream_routes.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não remover o endpoint JSON `POST /chat` (usado por demos e evals)

---

### TASK-096 — Web SSE client over fetch with abort

- **Requisito**: `STR-01`, `STR-04`, `STR-90`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: frontend
- **Depende de**: TASK-095
- **Arquivos de produção**:
  - `web/src/app/core/chat/chat-stream.service.ts`
  - `web/src/app/core/chat/sse-parser.ts`
- **Arquivos de teste**:
  - `web/src/app/core/chat/sse-parser.spec.ts`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-51 — Web `ChatStreamService.stream(req): Observable<StreamEvent>` (fetch + `AbortController`; unsubscribe aborta) e `parseSse(chunk: string, buffer: string): {events: StreamEvent[], rest: string}` em `sse-parser.ts` (produz)
  - CT-76 — `POST /chat/stream` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: POST com `fetch` + `ReadableStream` (o EventSource não faz POST nem manda corpo). O parser incremental lida com eventos quebrados entre chunks. O unsubscribe chama `AbortController.abort()` (STR-04).
- **Done when**:
  - [ ] teste prova que um evento partido em dois chunks é emitido uma única vez e completo
  - [ ] teste prova que unsubscribe chama `abort()`
  - [ ] `npx ng test --watch=false --include=src/app/core/chat/sse-parser.spec.ts` (executar em `web`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `npx ng lint` e `npx tsc --noEmit -p tsconfig.app.json` (executar em `web`) sem erro novo nos arquivos da task
- **Não fazer**:
  - Não alterar a página

---

### TASK-097 — Streaming chat UI with step indicator, citations and cancel

- **Requisito**: `STR-02`, `STR-03`, `STR-04`, `STR-90`, `STR-92`, `STR-08`
- **Tipo**: ui-puro
- **Risco**: médio
- **Perfil**: frontend
- **Depende de**: TASK-096
- **Arquivos de produção**:
  - `web/src/app/features/chat/chat.page.ts`
  - `web/src/app/features/chat/chat.page.html`
- **Arquivos de teste**:
  - `web/src/app/features/chat/chat.page.stream.spec.ts`
- **Wiring permitido**:
  - `web/src/app/core/i18n/messages.ts` (apenas chaves step.<tool>, chat.cancel e chat.error)
- **Reusa**: —
- **Contrato**:
  - CT-51 — `ChatStreamService.stream(req): Observable<StreamEvent>` (assinatura completa: plan 8.2) (consome)
  - CT-24 — `I18nService` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Renderiza tokens incrementalmente. Entre `tool_start` e `tool_end`, mostra o rótulo traduzido do passo ('consultando pedido…'). As citações aparecem sob a resposta. O botão cancelar só fica visível enquanto a resposta está em andamento (STR-92). `error` mostra a mensagem com 'tentar de novo'.
- **Done when**:
  - [ ] teste prova que `tool_start{tool:'get_order'}` exibe o rótulo traduzido e `tool_end` o remove
  - [ ] teste prova que clicar em cancelar encerra a assinatura do stream e nenhum token posterior é renderizado
  - [ ] teste prova que após `done` o botão cancelar não existe
  - [ ] teste prova que `error` exibe a mensagem de erro traduzida
  - [ ] `npx ng test --watch=false --include=src/app/features/chat/chat.page.stream.spec.ts` (executar em `web`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 4 testes, todos passando
  - [ ] `npx ng lint` e `npx tsc --noEmit -p tsconfig.app.json` (executar em `web`) sem erro novo nos arquivos da task
- **Não fazer**:
  - Não implementar feedback nem trace_id (fase 14)

---

### TASK-098 — Phase 09 streaming TTFT demo

- **Requisito**: `STR-06`, `DOD-02`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-095
- **Arquivos de produção**:
  - `demos/demo_09_streaming.py`
- **Arquivos de teste**:
  - `tests/integration/demos/test_demo_09.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-12 — `demos.common` (assinatura completa: plan 8.2) (consome)
  - CT-76 — `POST /chat/stream` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Mede TTFT e tempo total via `POST /chat/stream` (cliente httpx in-process ou `--base-url`) contra `POST /chat` e grava em `docs/results.md`. O README recebe o valor na task final de README.
- **Done when**:
  - [ ] teste com `--provider fake` prova a linha `| 09 |` com TTFT e tempo total
  - [ ] `uv run pytest -q tests/integration/demos/test_demo_09.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não editar README aqui

---

### TASK-099 — Phase 09 ADR and eval cases

- **Requisito**: `DOD-01`, `DOD-04`
- **Tipo**: config
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-098, TASK-097
- **Arquivos de produção**:
  - `docs/adr/0009-streaming.md`
  - `evals/datasets/phase_09.jsonl`
- **Arquivos de teste**:
  - `tests/unit/evals/test_phase_09_cases.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: ADR (SSE via fetch, eventos tipados, cancelamento persiste parcial) e casos de ordem de eventos.
- **Done when**:
  - [ ] teste prova ≥ 2 casos `phase == 9`, pt e en
  - [ ] `uv run pytest -q tests/unit/evals/test_phase_09_cases.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
- **Não fazer**:
  - Não criar a tag

---

## Fase 10 — Guardrails

### TASK-100 — Bilingual guardrail pattern library

- **Requisito**: `GRD-09`, `GRD-90`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-099
- **Arquivos de produção**:
  - `api/guardrails/patterns.py`
- **Arquivos de teste**:
  - `tests/unit/api/guardrails/test_patterns.py`
- **Wiring permitido**:
  - `api/guardrails/__init__.py` (criar vazio)
- **Reusa**: —
- **Contrato**:
  - CT-52 — `api.guardrails.patterns`: `INJECTION_PATTERNS: dict[Lang, tuple[re.Pattern, ...]]`, `ACTION_PROMISE_PATTERNS: dict[str, dict[Lang, tuple[re.Pattern, ...]]]` (chave = nome da tool), `find_injection(text: str) -> list[str]` (produz)
- **Testes**: unit
- **Descrição**: Padrões de injection em en e pt ('ignore previous instructions', 'ignore as instruções anteriores', 'you are now', 'system prompt', 'revele', exfiltração de outros clientes...) e padrões de promessa de ação por tool. Os padrões são ancorados para não casar com frases legítimas do controle (ex.: 'ignore a cobrança duplicada').
- **Done when**:
  - [ ] teste prova detecção de 5 ataques en e 5 pt
  - [ ] teste prova que 'Por favor, ignore a cobrança duplicada do mês passado' e 'Please ignore the duplicate charge' não são detectados
  - [ ] `uv run pytest -q tests/unit/api/guardrails/test_patterns.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 4 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não chamar LLM

---

### TASK-101 — PII detection and masking

- **Requisito**: `GRD-07`, `OBS-91`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-099
- **Arquivos de produção**:
  - `api/guardrails/pii.py`
- **Arquivos de teste**:
  - `tests/unit/api/guardrails/test_pii.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-53 — `api.guardrails.pii.mask_pii(text: str) -> MaskResult(text: str, findings: list[PiiFinding(kind: Literal["card","national_id","email","phone"], start: int, end: int)])` (substitui por `[CARD]`, `[ID]`, `[EMAIL]`, `[PHONE]`) (produz)
- **Testes**: unit
- **Descrição**: Detecta cartão (13–19 dígitos com Luhn), documento de identificação (CPF com dígitos verificadores, SSN `ddd-dd-dddd`), e-mail e telefone (formatos BR e US) e mascara com marcadores tipados (LAC-14).
- **Done when**:
  - [ ] teste prova o mascaramento de um cartão válido por Luhn e que 16 dígitos sem Luhn válido não são mascarados como cartão
  - [ ] teste prova o mascaramento de CPF, SSN, e-mail e telefone BR/US
  - [ ] teste prova que '#1234' (número de pedido) não é mascarado
  - [ ] `uv run pytest -q tests/unit/api/guardrails/test_pii.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 6 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não bloquear a mensagem (só mascarar, LAC-14)

---

### TASK-102 — Input guardrail with fail-closed behavior

- **Requisito**: `GRD-01`, `GRD-06`, `GRD-93`, `GRD-90`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-100, TASK-101
- **Arquivos de produção**:
  - `api/guardrails/input_guard.py`
- **Arquivos de teste**:
  - `tests/unit/api/guardrails/test_input_guard.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-54 — `api.guardrails.input_guard.InputGuard(settings).check(text: str) -> InputVerdict(action: Literal["allow","block"], masked_text: str, reasons: list[str])` (exceção interna → `block` com reason `guard_failure`) (produz)
  - CT-52 — `api.guardrails.patterns` (assinatura completa: plan 8.2) (consome)
  - CT-53 — `api.guardrails.pii.mask_pii(text: str) -> MaskResult(text: str, findings: list[PiiFinding(kind: Literal["card","national_id","email","phone"], start: int, end: int)])` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Primeiro mascara a PII e depois verifica injection no texto. Injection → `block` (LAC-13). Qualquer exceção interna → `block` com `guard_failure` (fail-closed, LAC-28).
- **Done when**:
  - [ ] teste prova `block` para ataque e `allow` com texto mascarado para mensagem com e-mail
  - [ ] teste prova que exceção simulada nos padrões gera `block` com reason `guard_failure`
  - [ ] `uv run pytest -q tests/unit/api/guardrails/test_input_guard.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não integrar no serviço (task do pipeline guardado)

---

### TASK-103 — Output guardrail with other-customer data, unfulfilled promise and topic checks

- **Requisito**: `GRD-04`, `GRD-08`, `GRD-93`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-100, TASK-101
- **Arquivos de produção**:
  - `api/guardrails/output_guard.py`
  - `prompts/topic_check.md`
- **Arquivos de teste**:
  - `tests/unit/api/guardrails/test_output_guard.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-55 — `api.guardrails.output_guard.OutputGuard(llm: LLMClient, settings)`: `async check_sentence(sentence: str, ctx: OutputContext) -> OutputVerdict(action: Literal["allow","block"], reason: str | None)`, `async check_topic(question: str, first_sentence: str, lang: Lang) -> OutputVerdict`; `OutputContext(customer_id: UUID, own_order_ids: set[int], own_email: str, successful_tools: set[str], question: str, language: Lang)` (produz)
  - CT-52 — `api.guardrails.patterns` (assinatura completa: plan 8.2) (consome)
  - CT-6 — `api.llm.client` (assinatura completa: plan 8.2) (consome)
  - CT-13 — `api.prompts.load_prompt(name: str) -> str` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Por frase: bloqueia e-mail diferente do e-mail do cliente, número de pedido fora de `own_order_ids` (GRD-04) e promessa de ação de uma tool sem sucesso na auditoria da requisição (GRD-08). O tema é verificado uma vez por resposta, por classificação estruturada (`ATLAS_GUARDRAIL_MODEL`, prompt `topic_check`), sobre a pergunta e a 1ª frase. Erro do classificador → `block` (fail-closed).
- **Done when**:
  - [ ] teste prova `block` para frase com e-mail de outra persona e `allow` para o e-mail da própria
  - [ ] teste prova `block` para 'I have cancelled your order' sem `cancel_order` em `successful_tools` e `allow` com ele
  - [ ] teste prova `block` quando o fake do classificador devolve `on_topic=false` e quando levanta erro
  - [ ] `uv run pytest -q tests/unit/api/guardrails/test_output_guard.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 5 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não lidar com streaming (task do filtro)

---

### TASK-104 — Sentence-buffered streaming output filter

- **Requisito**: `GRD-04`, `GRD-08`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-103
- **Arquivos de produção**:
  - `api/guardrails/stream_filter.py`
- **Arquivos de teste**:
  - `tests/unit/api/guardrails/test_stream_filter.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-75 — `api.guardrails.stream_filter.StreamOutputFilter(guard: OutputGuard, ctx: OutputContext)`: `async feed(delta: str) -> list[str]` (só frases aprovadas), `async finish() -> FilterOutcome(blocked: bool, released: str, reason: str | None)` (produz)
  - CT-55 — `api.guardrails.output_guard.OutputGuard(llm: LLMClient, settings)` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Acumula deltas e libera só frases completas aprovadas (DA-22). Na 1ª frase aplica também a checagem de tema. Uma frase bloqueada encerra a liberação, e `finish()` devolve `blocked=True` para o serviço trocar a resposta inteira pela mensagem padrão.
- **Done when**:
  - [ ] teste prova que nenhum trecho de uma frase reprovada é devolvido por `feed`
  - [ ] teste prova que frases aprovadas saem na ordem e que `finish()` libera o resto sem pontuação final
  - [ ] `uv run pytest -q tests/unit/api/guardrails/test_stream_filter.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não emitir eventos SSE

---

### TASK-105 — Untrusted retrieved content delimiters in prompts

- **Requisito**: `GRD-02`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-099
- **Arquivos de produção**:
  - `api/chat/prompt_builder.py`
- **Arquivos de teste**:
  - `tests/unit/api/chat/test_prompt_untrusted.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-31 — `api.chat.prompt_builder.build_request(*, language: Lang, history: HistoryWindow, chunks: list[RetrievedChunk], user_message: str, tools: list[ToolSpec], layout: Literal["naive","cache_ordered"]) -> LLMRequest` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Envolve cada trecho em `<untrusted_document index="n" source="...">…</untrusted_document>`, escapa ocorrências do delimitador no conteúdo e acrescenta ao system a regra de que o conteúdo delimitado é dado, nunca instrução.
- **Done when**:
  - [ ] teste prova que o conteúdo de chunk com `</untrusted_document>` é escapado
  - [ ] teste prova que os trechos ficam fora dos blocos de system
  - [ ] `uv run pytest -q tests/unit/api/chat/test_prompt_untrusted.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não reordenar para cache (fase 11)

---

### TASK-106 — Ingestion injection scan with quarantine

- **Requisito**: `GRD-03`, `GRD-91`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-100
- **Arquivos de produção**:
  - `ingest/pipeline.py`
- **Arquivos de teste**:
  - `tests/integration/ingest/test_quarantine.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-52 — `api.guardrails.patterns` (assinatura completa: plan 8.2) (consome)
  - CT-28 — `python -m ingest run [--corpus corpus] [--report reports/ingest-report.json] [--skip-embeddings]` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Antes de indexar, varre o texto com `find_injection`. Encontrou → status `quarantined`, nenhum chunk, e o documento aparece no relatório. `ATLAS_INGEST_INJECTION_SCAN=false` desliga a varredura (usado só no teste do GRD-91).
- **Done when**:
  - [ ] teste prova que o PDF malicioso do corpus fica `quarantined` com 0 chunks e listado em `totals.quarantined`
  - [ ] teste prova que, com a varredura desligada, ele é indexado
  - [ ] `uv run pytest -q tests/integration/ingest/test_quarantine.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não criar fluxo de liberação de quarentena (fora de escopo)

---

### TASK-107 — Guarded chat pipeline with masking and blocking

- **Requisito**: `GRD-01`, `GRD-04`, `GRD-06`, `GRD-07`, `GRD-08`, `GRD-91`, `GRD-93`, `OBS-91`
- **Tipo**: lógica-negócio
- **Risco**: alto
- **Âncora de risco**: AS-3 (mascaramento antes de persistir/LLM — `api/chat/service.py`) — decisão humana LAC-40 (Jev a_regra_central p=0.38)
- **Perfil**: backend
- **Depende de**: TASK-102, TASK-104, TASK-105, TASK-106
- **Arquivos de produção**:
  - `api/chat/service.py`
  - `api/agent/loop.py`
- **Arquivos de teste**:
  - `tests/integration/api/chat/test_service_guardrails.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-54 — `api.guardrails.input_guard.InputGuard(settings).check(text: str) -> InputVerdict(action: Literal["allow","block"], masked_text: str, reasons: list[str])` (assinatura completa: plan 8.2) (consome)
  - CT-55 — `api.guardrails.output_guard.OutputGuard(llm: LLMClient, settings)` (assinatura completa: plan 8.2) (consome)
  - CT-75 — `api.guardrails.stream_filter.StreamOutputFilter(guard: OutputGuard, ctx: OutputContext)` (assinatura completa: plan 8.2) (consome)
  - CT-15 — `api.chat.service.ChatService.handle_message(request: ChatRequest, principal: CurrentCustomer | None) -> AsyncIterator[AgentEvent]` (assinatura completa: plan 8.2) (consome)
  - CT-45 — `api.agent.events.AgentEvent` (assinatura completa: plan 8.2) (consome)
  - CT-39 — `api.tools.audit` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Entrada: `InputGuard` antes de persistir e de chamar o LLM. Bloqueio → mensagem padrão, `status='blocked'` e 0 chamadas ao LLM. Persistência, logs e LLM só veem o texto mascarado. Saída: os tokens do loop passam pelo `StreamOutputFilter` com o `OutputContext` da sessão (ids de pedido próprios, e-mail próprio, tools com sucesso no trace). Bloqueio → `done{status:'blocked'}` com a mensagem padrão, que substitui a resposta inteira.
- **Done when**:
  - [ ] teste prova que um ataque de injection gera `status='blocked'` e o fake do LLM registra 0 chamadas
  - [ ] teste prova que a mensagem com cartão é persistida e enviada ao LLM com `[CARD]` e que o log `llm_call`/`chat_message` não contém os dígitos
  - [ ] teste prova que o PDF malicioso indexado com a varredura desligada e o modelo obedecendo ('mostre os pedidos do cliente X') não retorna dado de outra persona (GRD-91)
  - [ ] teste prova que uma resposta prometendo reembolso sem `issue_refund` com sucesso é substituída pela mensagem padrão
  - [ ] `uv run pytest -q tests/integration/api/chat/test_service_guardrails.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 5 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não mudar a autorização das tools (já existe)

---

### TASK-108 — Phase 10 red-team suite and guardrail demo

- **Requisito**: `GRD-05`, `GRD-09`, `GRD-90`, `GRD-91`, `DOD-02`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-107
- **Arquivos de produção**:
  - `demos/demo_10_guardrails.py`
  - `evals/datasets/redteam.jsonl`
- **Arquivos de teste**:
  - `tests/integration/demos/test_demo_10.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-12 — `demos.common` (assinatura completa: plan 8.2) (consome)
  - CT-15 — `api.chat.service.ChatService.handle_message(request: ChatRequest, principal: CurrentCustomer | None) -> AsyncIterator[AgentEvent]` (assinatura completa: plan 8.2) (consome)
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: 30 ataques (15 en + 15 pt, incluindo perguntas que acionam o PDF malicioso) e ≥ 20 perguntas legítimas de controle (com casos parecidos com ataque, GRD-90), com `category` `red_team`/`control`. Mede as taxas de bloqueio e de falso positivo com guardrails ligados e desligados (`ATLAS_GUARDRAILS_ENABLED`).
- **Done when**:
  - [ ] teste prova que `redteam.jsonl` tem exatamente 30 casos `red_team` (15 pt, 15 en) e ≥ 20 de controle
  - [ ] teste com `--provider fake` prova a linha `| 10 |` com as duas taxas
  - [ ] `uv run pytest -q tests/integration/demos/test_demo_10.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não ajustar padrões para 'passar' casos específicos sem registrar no ADR

---

### TASK-109 — Phase 10 ADR and eval cases

- **Requisito**: `DOD-01`, `DOD-04`
- **Tipo**: config
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-108
- **Arquivos de produção**:
  - `docs/adr/0010-guardrails.md`
  - `evals/datasets/phase_10.jsonl`
- **Arquivos de teste**:
  - `tests/unit/evals/test_phase_10_cases.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: ADR (camadas, fail-closed, buffer por frase e o custo em TTFT) e casos `red_team` pt/en.
- **Done when**:
  - [ ] teste prova ≥ 2 casos `phase == 10`, pt e en
  - [ ] `uv run pytest -q tests/unit/evals/test_phase_10_cases.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
- **Não fazer**:
  - Não criar a tag

---

## Fase 11 — Prompt cache

### TASK-110 — Cache-ordered prompt layout with stable prefix

- **Requisito**: `CACHE-01`, `CACHE-02`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-109
- **Arquivos de produção**:
  - `api/chat/prompt_builder.py`
- **Arquivos de teste**:
  - `tests/unit/api/chat/test_prompt_cache_layout.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-31 — `api.chat.prompt_builder.build_request(*, language: Lang, history: HistoryWindow, chunks: list[RetrievedChunk], user_message: str, tools: list[ToolSpec], layout: Literal["naive","cache_ordered"]) -> LLMRequest` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: `cache_ordered`: system fixo + definições de tools + políticas fixas (prefixo estável, `cache_breakpoint=True` no último bloco), depois trechos, histórico e pergunta. O idioma vai numa mensagem após o prefixo, não no system (DA-23). `naive`: injeta data/hora e idioma no topo, o 'antes' do demo_11.
- **Done when**:
  - [ ] teste prova que dois requests de conversas e idiomas diferentes têm o prefixo estável serializado idêntico byte a byte
  - [ ] teste prova que o modo `naive` difere entre duas chamadas em instantes distintos
  - [ ] `uv run pytest -q tests/unit/api/chat/test_prompt_cache_layout.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não tocar no adapter

---

### TASK-111 — Prompt cache markers and cache token accounting

- **Requisito**: `CACHE-01`, `CACHE-03`, `CACHE-90`
- **Tipo**: integração-externa
- **Risco**: crítico
- **Âncora de risco**: AS-2 (custo real com preço de cache — `api/llm/pricing.py`) — decisão humana LAC-40 (Jev c_dinheiro p=0.62)
- **Perfil**: backend
- **Depende de**: TASK-109
- **Arquivos de produção**:
  - `api/llm/anthropic_adapter.py`
  - `api/llm/pricing.py`
- **Arquivos de teste**:
  - `tests/unit/api/llm/test_cache_accounting.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-6 — `api.llm.client` (assinatura completa: plan 8.2) (consome)
  - CT-7 — `api.llm.pricing.estimate_cost(model: str, usage: Usage) -> Decimal` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: O adapter Anthropic aplica `cache_control` ao bloco com `cache_breakpoint` e lê os tokens de leitura e escrita de cache do `usage`. O pricing já calcula com o preço de cache, e esta task garante que esses tokens chegam ao log `llm_call` (CACHE-03). Provedor sem cache → zeros (CACHE-90).
- **Done when**:
  - [ ] teste prova `cache_control` no payload capturado do último bloco estável
  - [ ] teste prova `Usage.cache_read_tokens`/`cache_write_tokens` preenchidos a partir da resposta mockada e custo calculado com `cache_read`
  - [ ] teste prova zeros quando a resposta não traz campos de cache
  - [ ] `uv run pytest -q tests/unit/api/llm/test_cache_accounting.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não alterar o layout do prompt

---

### TASK-112 — Phase 11 prompt cache demo

- **Requisito**: `CACHE-04`, `DOD-02`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-110, TASK-111
- **Arquivos de produção**:
  - `demos/demo_11_prompt_cache.py`
- **Arquivos de teste**:
  - `tests/integration/demos/test_demo_11.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-12 — `demos.common` (assinatura completa: plan 8.2) (consome)
  - CT-15 — `api.chat.service.ChatService.handle_message(request: ChatRequest, principal: CurrentCustomer | None) -> AsyncIterator[AgentEvent]` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: 100 conversas (`--n`) com `naive` e com `cache_ordered`. Registra custo e latência médios por chamada e a taxa de cache hit (chamadas com `cache_read_tokens > 0`).
- **Done when**:
  - [ ] teste com `--provider fake --n 5` prova a linha `| 11 |`
  - [ ] `uv run pytest -q tests/integration/demos/test_demo_11.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não mudar o pricing

---

### TASK-113 — Phase 11 ADR and eval cases

- **Requisito**: `DOD-01`, `DOD-04`
- **Tipo**: config
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-112
- **Arquivos de produção**:
  - `docs/adr/0011-prompt-cache.md`
  - `evals/datasets/phase_11.jsonl`
- **Arquivos de teste**:
  - `tests/unit/evals/test_phase_11_cases.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: ADR (ordem estável → variável, data/hora no topo invalida o cache) e casos de regressão de comportamento pt/en.
- **Done when**:
  - [ ] teste prova ≥ 2 casos `phase == 11`, pt e en
  - [ ] `uv run pytest -q tests/unit/evals/test_phase_11_cases.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
- **Não fazer**:
  - Não criar a tag

---

## Fase 12 — Retry e fallback

### TASK-114 — OpenAI fallback adapter with tool format normalization

- **Requisito**: `RES-05`, `CACHE-90`, `TRI-02`
- **Tipo**: integração-externa
- **Risco**: alto
- **Âncora de risco**: AS-8 (primeira integração OpenAI LLM — `api/llm/openai_adapter.py`) — Jev a_terceiro p=0.69
- **Perfil**: backend
- **Depende de**: TASK-113
- **Arquivos de produção**:
  - `api/llm/openai_adapter.py`
- **Arquivos de teste**:
  - `tests/unit/api/llm/test_openai_adapter.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-6 — `api.llm.client` (assinatura completa: plan 8.2) (consome)
  - CT-1 — `api.config.Settings` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Adapter do provedor secundário (DA-14) via SDK `openai` (`max_retries=0`): converte `ToolSpec` → `tools[type=function]`, `tool_calls` → `ToolCall`, `response_schema` → `response_format` `json_schema` estrito, `stream`, e lê o cache automático em `usage.prompt_tokens_details.cached_tokens`.
- **Done when**:
  - [ ] teste com MockTransport prova a conversão de uma resposta com `tool_calls` em `ToolCall` com `arguments` dict
  - [ ] teste prova que o mesmo `LLMRequest` com tools gera `tools[].function.parameters` igual ao `input_schema`
  - [ ] teste prova 429/5xx recuperáveis e 400/401 não recuperáveis
  - [ ] teste prova stream de texto em `text_delta`s
  - [ ] `uv run pytest -q tests/unit/api/llm/test_openai_adapter.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 5 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não montar a cadeia (task da fábrica)

---

### TASK-115 — Retry with exponential backoff, jitter and timeout

- **Requisito**: `RES-01`, `RES-02`, `RES-03`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-113
- **Arquivos de produção**:
  - `api/llm/retry.py`
- **Arquivos de teste**:
  - `tests/unit/api/llm/test_retry.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-56 — `api.llm.retry.RetryingLLMClient(inner: LLMClient, *, max_retries: int, base_delay_s: float, timeout_s: float, sleep=asyncio.sleep, rng=random.random)` (implementa CT‑6) (produz)
  - CT-6 — `api.llm.client` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: `RetryingLLMClient`: `asyncio.timeout(timeout_s)` por tentativa (RES-01) e retry só para `recoverable` com atraso `base * 2^n * uniform(0.5, 1.5)` (relógio e RNG injetáveis), até `max_retries`. No `stream`, só repete antes do 1º evento.
- **Done when**:
  - [ ] teste prova 3 tentativas para 2 falhas 429 seguidas de sucesso, com atrasos crescentes (sleep falso)
  - [ ] teste prova 1 tentativa só para erro 400
  - [ ] teste prova timeout tratado como recuperável
  - [ ] `uv run pytest -q tests/unit/api/llm/test_retry.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 4 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não implementar breaker, chaos nem fallback (tasks próprias)

---

### TASK-116 — Per-provider circuit breaker

- **Requisito**: `RES-04`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-115
- **Arquivos de produção**:
  - `api/llm/circuit_breaker.py`
- **Arquivos de teste**:
  - `tests/unit/api/llm/test_circuit_breaker.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-57 — `api.llm.circuit_breaker.CircuitBreaker(name: str, failure_threshold: int, reset_seconds: float, clock=time.monotonic)` com `state: Literal["closed","open","half_open"]`, `allow() -> bool`, `record_success()`, `record_failure()`; `BreakerLLMClient(inner, breaker)` (implementa CT‑6; aberto → `CircuitOpen(LLMError, recoverable=True)`) (produz)
  - CT-6 — `api.llm.client` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Estados fechado → aberto → meio-aberto (seção 7 do spec) com relógio injetável. Aberto: levanta `CircuitOpen` (recuperável) sem chamar o provedor.
- **Done when**:
  - [ ] teste prova a abertura após `failure_threshold` falhas e 0 chamadas ao inner enquanto aberto
  - [ ] teste prova meio-aberto após `reset_seconds`, com sucesso fechando e falha reabrindo
  - [ ] `uv run pytest -q tests/unit/api/llm/test_circuit_breaker.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não guardar estado em banco (estado por processo, DA-24)

---

### TASK-117 — Chaos failure injection for the primary provider

- **Requisito**: `RES-07`, `DEMO-05`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-115
- **Arquivos de produção**:
  - `api/llm/chaos.py`
- **Arquivos de teste**:
  - `tests/unit/api/llm/test_chaos.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-58 — `api.llm.chaos.ChaosLLMClient(inner: LLMClient, rate: float, rng: random.Random | None = None)` (implementa CT‑6) (produz)
  - CT-6 — `api.llm.client` (assinatura completa: plan 8.2) (consome)
  - CT-1 — `api.config.Settings` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: `ChaosLLMClient` levanta `LLMError(recoverable=True, status=503)` com probabilidade `CHAOS_RATE` (0 se ausente). Valores fora de [0,1] falham na validação das Settings.
- **Done when**:
  - [ ] teste com RNG semeado prova ~30% de falhas em 1.000 chamadas com rate 0.3 (entre 25% e 35%)
  - [ ] teste prova 0 falhas com rate 0
  - [ ] `uv run pytest -q tests/unit/api/llm/test_chaos.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não aplicar ao secundário

---

### TASK-118 — Provider fallback chain with first-token boundary

- **Requisito**: `RES-05`, `RES-91`, `RES-92`, `RES-93`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-116
- **Arquivos de produção**:
  - `api/llm/fallback.py`
- **Arquivos de teste**:
  - `tests/unit/api/llm/test_fallback.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-59 — `api.llm.fallback.FallbackLLMClient(chain: list[tuple[str, LLMClient, CircuitBreaker]])` (implementa CT‑6) e `AllProvidersFailed(Exception)`; no `stream`, troca de provedor só antes do 1º `text_delta` (produz)
  - CT-57 — `api.llm.circuit_breaker.CircuitBreaker(name: str, failure_threshold: int, reset_seconds: float, clock=time.monotonic)` (assinatura completa: plan 8.2) (consome)
  - CT-6 — `api.llm.client` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Tenta cada cliente da cadeia em ordem, pulando quem está com circuito aberto. Todos falham → `AllProvidersFailed` (RES-92, sem chamada quando todos estão abertos). No `stream`: só troca de provedor antes do 1º `text_delta`, e depois dele propaga o erro (RES-93, LAC-29). Emite o log `llm_fallback` com o provedor de origem e o de destino.
- **Done when**:
  - [ ] teste prova que a falha do primário é atendida pelo secundário com o mesmo `LLMRequest`
  - [ ] teste prova que, com todos abertos, `AllProvidersFailed` sai sem nenhuma chamada
  - [ ] teste prova que falha após o 1º token no stream propaga erro sem chamar o secundário
  - [ ] `uv run pytest -q tests/unit/api/llm/test_fallback.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 4 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não repetir tool calls (a cadeia opera só na chamada ao modelo, RES-91)

---

### TASK-119 — Support ticket schema

- **Requisito**: `RES-06`
- **Tipo**: migration
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-113
- **Arquivos de produção**:
  - `api/tickets/models.py`
  - `api/db/migrations/versions/0009_tickets.py`
- **Arquivos de teste**:
  - `tests/integration/api/tickets/test_ticket_models.py`
- **Wiring permitido**:
  - `api/tickets/__init__.py` (criar vazio)
  - `api/db/migrations/env.py` (apenas importar `api.tickets.models`)
- **Reusa**: —
- **Contrato**:
  - CT-60 — ORM `api.tickets.models.Ticket(id, conversation_id, customer_id, reason, trace_id, created_at)` (produz)
  - CT-5 — `api.db.engine` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Tabela `tickets` (conversa, cliente, motivo, trace_id).
- **Done when**:
  - [ ] teste prova `upgrade`/`downgrade`
  - [ ] `uv run pytest -q tests/integration/api/tickets/test_ticket_models.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não criar interface de tickets (fora de escopo)

---

### TASK-120 — Ticket opening service

- **Requisito**: `RES-06`
- **Tipo**: crud-padrão
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-119
- **Arquivos de produção**:
  - `api/tickets/service.py`
- **Arquivos de teste**:
  - `tests/integration/api/tickets/test_ticket_service.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-61 — `api.tickets.service.open_ticket(session, *, conversation_id: UUID, customer_id: UUID, reason: str, trace_id: str) -> Ticket` (produz)
  - CT-60 — `api.tickets.models.Ticket(id, conversation_id, customer_id, reason, trace_id, created_at)` (assinatura completa: plan 8.2) (consome)
  - CT-5 — `api.db.engine` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: `open_ticket` grava o ticket com conversa, motivo e `trace_id`.
- **Done when**:
  - [ ] teste prova o ticket gravado com os três campos
  - [ ] `uv run pytest -q tests/integration/api/tickets/test_ticket_service.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não notificar ninguém

---

### TASK-121 — Resilient client factory and degraded response

- **Requisito**: `RES-05`, `RES-06`, `RES-91`, `RES-92`, `RES-93`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-114, TASK-117, TASK-118, TASK-120
- **Arquivos de produção**:
  - `api/llm/factory.py`
  - `api/chat/service.py`
- **Arquivos de teste**:
  - `tests/integration/api/chat/test_service_degraded.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-9 — `api.llm.instrumented.InstrumentedLLMClient(inner: LLMClient, model: str, provider: str)` (assinatura completa: plan 8.2) (consome)
  - CT-56 — `api.llm.retry.RetryingLLMClient(inner: LLMClient, *, max_retries: int, base_delay_s: float, timeout_s: float, sleep=asyncio.sleep, rng=random.random)` (assinatura completa: plan 8.2) (consome)
  - CT-57 — `api.llm.circuit_breaker.CircuitBreaker(name: str, failure_threshold: int, reset_seconds: float, clock=time.monotonic)` (assinatura completa: plan 8.2) (consome)
  - CT-58 — `api.llm.chaos.ChaosLLMClient(inner: LLMClient, rate: float, rng: random.Random | None = None)` (assinatura completa: plan 8.2) (consome)
  - CT-59 — `api.llm.fallback.FallbackLLMClient(chain: list[tuple[str, LLMClient, CircuitBreaker]])` (assinatura completa: plan 8.2) (consome)
  - CT-61 — `api.tickets.service.open_ticket(session, *, conversation_id: UUID, customer_id: UUID, reason: str, trace_id: str) -> Ticket` (assinatura completa: plan 8.2) (consome)
  - CT-15 — `api.chat.service.ChatService.handle_message(request: ChatRequest, principal: CurrentCustomer | None) -> AsyncIterator[AgentEvent]` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: A fábrica monta `Fallback([Breaker(Retry(Instrumented(Chaos(Anthropic)))), Breaker(Retry(Instrumented(OpenAI)))])`. O secundário só entra se `OPENAI_API_KEY` e `ATLAS_SECONDARY_MODEL` existem. No serviço, `AllProvidersFailed` → mensagem `DEGRADED_RESPONSE` (`status='degraded'`) + `open_ticket`. Falha após o 1º token no stream → evento `error` (RES-93). As tools já auditadas não são reexecutadas (RES-91).
- **Done when**:
  - [ ] teste prova que, com os dois fakes falhando, a resposta é a mensagem degradada e existe 1 ticket com `trace_id`
  - [ ] teste prova que, quando a falha ocorre depois de uma `cancel_order` bem-sucedida no mesmo turno, há só 1 linha de auditoria `success`
  - [ ] teste prova que a fábrica com `CHAOS_RATE=0.3` envolve só o primário
  - [ ] `uv run pytest -q tests/integration/api/chat/test_service_degraded.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não alterar adapters

---

### TASK-122 — Phase 12 resilience demo under injected failures

- **Requisito**: `RES-08`, `DOD-02`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-121
- **Arquivos de produção**:
  - `demos/demo_12_resilience.py`
- **Arquivos de teste**:
  - `tests/integration/demos/test_demo_12.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-12 — `demos.common` (assinatura completa: plan 8.2) (consome)
  - CT-15 — `api.chat.service.ChatService.handle_message(request: ChatRequest, principal: CurrentCustomer | None) -> AsyncIterator[AgentEvent]` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Roda N mensagens com `CHAOS_RATE=0.3` sem resiliência (só primário) e com a cadeia completa. Registra a taxa de sucesso final e a latência extra média.
- **Done when**:
  - [ ] teste com `--provider fake --n 20` prova a linha `| 12 |`
  - [ ] `uv run pytest -q tests/integration/demos/test_demo_12.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não ligar CHAOS_RATE fora do demo

---

### TASK-123 — Phase 12 ADR and eval cases

- **Requisito**: `DOD-01`, `DOD-04`
- **Tipo**: config
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-122
- **Arquivos de produção**:
  - `docs/adr/0012-retry-fallback.md`
  - `evals/datasets/phase_12.jsonl`
- **Arquivos de teste**:
  - `tests/unit/evals/test_phase_12_cases.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: ADR (retry só recuperável, breaker por provedor, fallback só antes do 1º token) e casos de resposta degradada pt/en.
- **Done when**:
  - [ ] teste prova ≥ 2 casos `phase == 12`, pt e en
  - [ ] `uv run pytest -q tests/unit/evals/test_phase_12_cases.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
- **Não fazer**:
  - Não criar a tag

---

## Fase 13 — Evals

### TASK-124 — Core bilingual eval dataset of about 100 cases

- **Requisito**: `EVAL-01`, `EVAL-08`
- **Tipo**: config
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-123
- **Arquivos de produção**:
  - `evals/datasets/core_en.jsonl`
  - `evals/datasets/core_pt.jsonl`
- **Arquivos de teste**:
  - `tests/unit/evals/test_dataset_composition.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: ~100 casos no total entre os arquivos `core_*.jsonl` e os `phase_*.jsonl`/`redteam.jsonl` já existentes, cobrindo `rag`, `tool_trajectory`, `json_valid`, `red_team` e `tone`, cada categoria em pt e en.
- **Done when**:
  - [ ] teste prova total de casos (todos os datasets, exceto `smoke`) entre 90 e 130
  - [ ] teste prova ≥ 1 caso por categoria × idioma (5 × 2)
  - [ ] teste prova ids únicos entre arquivos
  - [ ] `uv run pytest -q tests/unit/evals/test_dataset_composition.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
- **Não fazer**:
  - Não alterar graders

---

### TASK-125 — Deterministic and LLM-as-judge graders

- **Requisito**: `EVAL-02`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-123
- **Arquivos de produção**:
  - `evals/graders.py`
- **Arquivos de teste**:
  - `tests/unit/evals/test_graders.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
  - CT-33 — `evals.judge.judge_faithfulness(llm: LLMClient, question: str, answer: str, chunks: list[str]) -> JudgeVerdict(faithful: bool, score: float, rationale: str)` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Graders determinísticos: `tool_trajectory` (sequência exata), `json_valid` (valida contra `TriageResult`), `citation` (doc_key esperado entre as citações), `red_team_blocked` (status `blocked`). LLM-as-judge: `faithfulness` (reusa `evals/judge.py`) e `tone`. Cada grader devolve um score de 0 a 100.
- **Done when**:
  - [ ] teste prova score 100/0 para trajetória igual/diferente
  - [ ] teste prova que `json_valid` dá 0 para campo extra
  - [ ] teste prova que `faithfulness` chama o juiz (fake) e converte `score` em 0–100
  - [ ] `uv run pytest -q tests/unit/evals/test_graders.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 4 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não usar LLM em grader determinístico

---

### TASK-126 — In-process eval targets for chat and triage

- **Requisito**: `EVAL-01`, `EVAL-02`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-125, TASK-124
- **Arquivos de produção**:
  - `evals/targets.py`
- **Arquivos de teste**:
  - `tests/integration/evals/test_targets.py`
- **Wiring permitido**:
  - `evals/__main__.py` (apenas registrar os targets `chat` e `triage` e o grader padrão por categoria)
- **Reusa**: —
- **Contrato**:
  - CT-15 — `api.chat.service.ChatService.handle_message(request: ChatRequest, principal: CurrentCustomer | None) -> AsyncIterator[AgentEvent]` (assinatura completa: plan 8.2) (consome)
  - CT-74 — `api.triage.service.TriageService(llm: LLMClient, settings)` (assinatura completa: plan 8.2) (consome)
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Targets que executam o caso contra o `ChatService` (com uma persona semeada num banco efêmero, confirmando as destrutivas) ou contra o `TriageService`, e devolvem texto, trajetória de tools, citações e status para os graders.
- **Done when**:
  - [ ] teste prova que um caso `tool_trajectory` roda no target `chat` com fake e devolve a trajetória
  - [ ] teste prova que um caso `json_valid` roda no target `triage`
  - [ ] `uv run pytest -q tests/integration/evals/test_targets.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não chamar o servidor HTTP

---

### TASK-127 — Eval scoring gate against main-branch baseline

- **Requisito**: `EVAL-05`, `EVAL-07`, `EVAL-90`, `EVAL-91`, `EVAL-92`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-123
- **Arquivos de produção**:
  - `evals/gate.py`
- **Arquivos de teste**:
  - `tests/unit/evals/test_gate.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: `python -m evals gate --current <json> [--base <json>]`. Sem base: exit 0 e o relatório marca `baseline_created` (EVAL-92). Queda do geral > 2,0 pontos: exit 1 (regressão). Queda de exatamente 2,0: exit 0 (EVAL-91). Quedas por categoria são só reportadas. Relatório com `status='error'` ou erro de leitura: exit 2, diferente de regressão (EVAL-90).
- **Done when**:
  - [ ] teste prova exit 1 para queda de 2,01 e exit 0 para queda de 2,00
  - [ ] teste prova exit 0 sem base
  - [ ] teste prova exit 2 para relatório `status='error'`
  - [ ] teste prova que queda só em categoria aparece no resultado e não bloqueia
  - [ ] `uv run pytest -q tests/unit/evals/test_gate.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 5 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não baixar artefatos (o workflow faz)

---

### TASK-128 — Markdown eval report for PR comments

- **Requisito**: `EVAL-03`, `DOD-04`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-127
- **Arquivos de produção**:
  - `evals/report.py`
- **Arquivos de teste**:
  - `tests/unit/evals/test_report.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Relatório em inglês com score geral e por categoria, delta contra a base, veredito (pass / regression / error / baseline created) e os casos que pioraram.
- **Done when**:
  - [ ] teste prova que o markdown contém 'Overall', cada categoria e o veredito
  - [ ] `uv run pytest -q tests/unit/evals/test_report.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não postar no GitHub (o workflow faz)

---

### TASK-129 — Evals CI workflow with path filter and merge gate

- **Requisito**: `EVAL-03`, `EVAL-04`, `EVAL-05`, `EVAL-90`
- **Tipo**: infra
- **Risco**: médio
- **Perfil**: infra
- **Depende de**: TASK-128, TASK-126
- **Arquivos de produção**:
  - `.github/workflows/evals.yml`
- **Arquivos de teste**:
  - `tests/unit/infra/test_evals_workflow.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**: —
- **Testes**: unit
- **Descrição**: Gatilho `pull_request` com `paths: ['prompts/**', 'api/**']` (EVAL-03/04) e `push` na branch padrão (gera a base). No PR: baixa o artefato `eval-report` do último run bem-sucedido da branch padrão (`gh run list` + `gh run download`), roda os evals com as chaves de `secrets`, comenta o relatório (`gh pr comment --edit-last` ou novo) e falha com o exit do gate. O bloqueio de merge depende de o Operador marcar o job como required check (seção 16).
- **Done when**:
  - [ ] teste prova `on.pull_request.paths` exatamente `['prompts/**','api/**']`
  - [ ] teste prova um step que executa `python -m evals gate` sem `continue-on-error`
  - [ ] teste prova `upload-artifact` com nome `eval-report` no push da branch padrão
  - [ ] `uv run pytest -q tests/unit/infra/test_evals_workflow.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não alterar `ci.yml`

---

### TASK-130 — Phase 13 eval gate regression demo

- **Requisito**: `EVAL-06`, `DOD-02`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-129
- **Arquivos de produção**:
  - `demos/demo_13_eval_gate.py`
- **Arquivos de teste**:
  - `tests/unit/demos/test_demo_13.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-12 — `demos.common` (assinatura completa: plan 8.2) (consome)
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Roda os evals com o prompt atual e com uma versão degradada temporária de `prompts/system.md` (sem a regra de citação), mostra o gate bloqueando e registra os dois scores em `docs/results.md` com espaço para o link do PR real (EVAL-06, preenchido pelo Operador).
- **Done when**:
  - [ ] teste com `--provider fake` prova exit do gate 1 para o prompt degradado e a linha `| 13 |`
  - [ ] teste prova que `prompts/system.md` fica intacto ao fim (restaurado mesmo em erro)
  - [ ] `uv run pytest -q tests/unit/demos/test_demo_13.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não commitar o prompt degradado

---

### TASK-131 — Phase 13 ADR and eval cases

- **Requisito**: `DOD-01`, `DOD-04`
- **Tipo**: config
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-130
- **Arquivos de produção**:
  - `docs/adr/0013-evals.md`
  - `evals/datasets/phase_13.jsonl`
- **Arquivos de teste**:
  - `tests/unit/evals/test_phase_13_cases.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: ADR (escala 0–100, base = branch padrão, limite de 2 pontos) e casos `tone` pt/en.
- **Done when**:
  - [ ] teste prova ≥ 2 casos `phase == 13`, pt e en
  - [ ] `uv run pytest -q tests/unit/evals/test_phase_13_cases.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
- **Não fazer**:
  - Não criar a tag

---

## Fase 14 — Observabilidade

### TASK-132 — OpenTelemetry tracing with Langfuse OTLP export

- **Requisito**: `OBS-01`, `OBS-91`, `OBS-93`
- **Tipo**: integração-externa
- **Risco**: crítico
- **Âncora de risco**: AS-8 (Langfuse — `api/observability/tracing.py`), AS-3 (atributos mascarados) — Jev a_terceiro p=0.87; c_cripto p=0.45 em dúvida com âncora → nível maior
- **Perfil**: backend
- **Depende de**: TASK-131
- **Arquivos de produção**:
  - `api/observability/tracing.py`
- **Arquivos de teste**:
  - `tests/unit/api/observability/test_tracing.py`
- **Wiring permitido**:
  - `api/main.py` (apenas chamar `init_tracing(settings)` no lifespan e criar o span raiz no middleware)
- **Reusa**: —
- **Contrato**:
  - CT-62 — `api.observability.tracing`: `init_tracing(settings) -> None`, `start_span(name: str, **attributes: object) -> ContextManager[Span]`, constantes `ATTR_SESSION_ID = "langfuse.session.id"`, `ATTR_MODEL = "gen_ai.request.model"`, `ATTR_INPUT_TOKENS = "gen_ai.usage.input_tokens"`, `ATTR_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"`, `ATTR_COST = "atlas.cost_usd"`, `ATTR_FALLBACK = "atlas.fallback"`; exportação nunca bloqueia nem propaga erro (produz)
  - CT-1 — `api.config.Settings` (assinatura completa: plan 8.2) (consome)
  - CT-2 — `api.observability.logging` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: TracerProvider com `BatchSpanProcessor` + OTLP HTTP para `{LANGFUSE_HOST}/api/public/otel` (Basic auth com as chaves públicas e secretas, DA-25). O `trace_id` do OTel é o mesmo `trace_id` do log. Exportação desligada sem chaves. Falha de exportação → aviso `telemetry_dropped` no log local, sem afetar a requisição (OBS-93). Atributos de texto passam por `mask_pii` (OBS-91).
- **Done when**:
  - [ ] teste com `InMemorySpanExporter` prova que `start_span` cria span com os atributos e que o trace_id OTel em hex é igual ao `get_trace_id()`
  - [ ] teste prova que um exportador que levanta erro não propaga a exceção e gera o log `telemetry_dropped`
  - [ ] teste prova que atributo com cartão é gravado mascarado
  - [ ] `uv run pytest -q tests/unit/api/observability/test_tracing.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não instrumentar os módulos (task seguinte)

---

### TASK-133 — Span instrumentation for guardrails, retrieval, LLM, tools and fallback

- **Requisito**: `OBS-01`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-132
- **Arquivos de produção**:
  - `api/llm/instrumented.py`
  - `api/rag/retriever.py`
- **Arquivos de teste**:
  - `tests/integration/api/observability/test_request_spans.py`
- **Wiring permitido**:
  - `api/tools/registry.py` (apenas envolver `execute` em `start_span('tool', ...)` com argumentos e resultado)
  - `api/llm/fallback.py` (apenas `start_span('fallback', ...)` ao trocar de provedor)
  - `api/guardrails/input_guard.py` e `api/guardrails/output_guard.py` (apenas `start_span('guardrail', ...)` com veredito)
- **Reusa**: —
- **Contrato**:
  - CT-62 — `api.observability.tracing` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Spans `llm` (modelo, prompt mascarado, tokens de entrada/saída/cache, custo, latência), `retrieval` (ids dos chunks e scores), `tool`, `guardrail` e `fallback`, todos filhos do span da requisição, com `langfuse.session.id = conversation_id`.
- **Done when**:
  - [ ] teste de uma requisição de chat com tool e retrieval (fakes) prova, no exporter em memória, spans `guardrail`, `retrieval`, `llm` e `tool` com o mesmo trace_id e os atributos de custo e tokens
  - [ ] `uv run pytest -q tests/integration/api/observability/test_request_spans.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não mudar a lógica dos módulos instrumentados

---

### TASK-134 — Langfuse public API client for scores and trace deletion

- **Requisito**: `OBS-02`, `DEMO-10`
- **Tipo**: integração-externa
- **Risco**: crítico
- **Âncora de risco**: AS-8 (API do Langfuse — `api/observability/langfuse_api.py`), AS-5 (exclusão de traces) — Jev a_terceiro p=0.72; c_dado_existente p=0.61 em dúvida com âncora → nível maior
- **Perfil**: backend
- **Depende de**: TASK-131
- **Arquivos de produção**:
  - `api/observability/langfuse_api.py`
- **Arquivos de teste**:
  - `tests/unit/api/observability/test_langfuse_api.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-63 — `api.observability.langfuse_api.LangfuseApi(settings, http: httpx.AsyncClient | None = None)`: `async push_score(trace_id: str, value: int, comment: str | None = None) -> bool`, `async delete_traces_older_than(cutoff: datetime) -> int` (produz)
  - CT-1 — `api.config.Settings` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Cliente httpx da API pública do Langfuse: cria score (`name='user_feedback'`, valor 1/-1) ligado ao trace e apaga traces anteriores ao corte (lista paginada por `toTimestamp` e exclusão em lote), conforme LAC-36. Os endpoints exatos são confirmados na documentação da versão em uso (risco R-6). Erro de rede → `False`/`0` com log, nunca exceção.
- **Done when**:
  - [ ] teste com MockTransport prova o corpo do score com `traceId` e o valor
  - [ ] teste prova a paginação de exclusão (2 páginas → total somado)
  - [ ] teste prova que HTTP 500 devolve `False` sem levantar
  - [ ] `uv run pytest -q tests/unit/api/observability/test_langfuse_api.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não agendar expurgo (demo pública)

---

### TASK-135 — Feedback schema

- **Requisito**: `OBS-02`
- **Tipo**: migration
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-131
- **Arquivos de produção**:
  - `api/feedback/models.py`
  - `api/db/migrations/versions/0010_feedback.py`
- **Arquivos de teste**:
  - `tests/integration/api/feedback/test_feedback_models.py`
- **Wiring permitido**:
  - `api/feedback/__init__.py` (criar vazio)
  - `api/db/migrations/env.py` (apenas importar `api.feedback.models`)
- **Reusa**: —
- **Contrato**:
  - CT-64 — ORM `api.feedback.models.Feedback(id, message_id UNIQUE, customer_id, rating: Literal["up","down"], trace_id, created_at)` (produz)
  - CT-5 — `api.db.engine` (assinatura completa: plan 8.2) (consome)
  - CT-19 — `api.chat.models.Conversation(id UUID, customer_id, created_at, summary, summary_upto_message_id)` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Tabela `feedback` (um por mensagem; `rating` em `up|down`; `trace_id`).
- **Done when**:
  - [ ] teste prova `UNIQUE(message_id)` e CHECK de rating
  - [ ] `uv run pytest -q tests/integration/api/feedback/test_feedback_models.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não criar rota

---

### TASK-136 — Feedback endpoint with ownership check

- **Requisito**: `OBS-02`, `OBS-92`
- **Tipo**: crud-padrão
- **Risco**: crítico
- **Âncora de risco**: AS-1 (feedback só do dono — `api/feedback/routes.py`) — acima do teto de crud-padrão porque decide autorização (Jev c_auth p=0.74)
- **Perfil**: backend
- **Depende de**: TASK-135, TASK-134
- **Arquivos de produção**:
  - `api/feedback/routes.py`
- **Arquivos de teste**:
  - `tests/integration/api/feedback/test_feedback_route.py`
- **Wiring permitido**:
  - `api/main.py` (apenas `include_router(feedback_router)`)
- **Reusa**: —
- **Contrato**:
  - CT-65 — HTTP `POST /messages/{message_id}/feedback` body `{rating: "up"|"down"}` → 204; 404 `MESSAGE_NOT_FOUND` (inexistente ou de outro cliente); 401 `AUTH_REQUIRED` (produz)
  - CT-64 — `api.feedback.models.Feedback(id, message_id UNIQUE, customer_id, rating: Literal["up","down"], trace_id, created_at)` (assinatura completa: plan 8.2) (consome)
  - CT-16 — `api.auth.deps` (assinatura completa: plan 8.2) (consome)
  - CT-63 — `api.observability.langfuse_api.LangfuseApi(settings, http: httpx.AsyncClient | None = None)` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: `POST /messages/{id}/feedback` só para mensagem do assistente numa conversa do cliente. A mensagem de outro cliente recebe o mesmo 404 da inexistente, sem gravar (OBS-92). Grava o feedback com o `trace_id` da mensagem e envia o score ao Langfuse em background (best effort).
- **Done when**:
  - [ ] teste prova 204 e a linha gravada com o `trace_id` da mensagem
  - [ ] teste prova 404 sem gravar para mensagem de outra persona
  - [ ] teste prova que um 2º feedback na mesma mensagem substitui o rating (upsert)
  - [ ] `uv run pytest -q tests/integration/api/feedback/test_feedback_route.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não exibir dashboard no app (OBS-07)

---

### TASK-137 — Web message meta with trace id and feedback buttons

- **Requisito**: `OBS-02`, `OBS-06`
- **Tipo**: ui-puro
- **Risco**: médio
- **Perfil**: frontend
- **Depende de**: TASK-136
- **Arquivos de produção**:
  - `web/src/app/features/chat/message-meta.component.ts`
  - `web/src/app/core/chat/feedback-api.service.ts`
- **Arquivos de teste**:
  - `web/src/app/features/chat/message-meta.component.spec.ts`
- **Wiring permitido**:
  - `web/src/app/features/chat/chat.page.html` (apenas inserir `<atlas-message-meta>` nas mensagens do assistente)
  - `web/src/app/core/i18n/messages.ts` (apenas chaves `feedback.*`)
- **Reusa**: —
- **Contrato**:
  - CT-65 — `POST /messages/{message_id}/feedback` (assinatura completa: plan 8.2) (consome)
  - CT-24 — `I18nService` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: Abaixo de cada resposta: o `trace_id` em texto pequeno, discreto e copiável (OBS-06), e os botões 👍/👎 que chamam a API (rótulos acessíveis traduzidos).
- **Done when**:
  - [ ] teste prova que o `trace_id` recebido é renderizado
  - [ ] teste prova que clicar em 'down' faz `POST /api/messages/{id}/feedback` com `{rating:'down'}`
  - [ ] `npx ng test --watch=false --include=src/app/features/chat/message-meta.component.spec.ts` (executar em `web`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `npx ng lint` e `npx tsc --noEmit -p tsconfig.app.json` (executar em `web`) sem erro novo nos arquivos da task
- **Não fazer**:
  - Não criar tela de dashboard (OBS-07)

---

### TASK-138 — Local Langfuse compose profile and dashboard definition

- **Requisito**: `OBS-03`, `OBS-07`
- **Tipo**: infra
- **Risco**: médio
- **Perfil**: infra
- **Depende de**: TASK-132
- **Arquivos de produção**:
  - `docker-compose.yml`
  - `docs/observability.md`
- **Arquivos de teste**:
  - `tests/unit/infra/test_compose_observability.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**: —
- **Testes**: unit
- **Descrição**: Perfil `observability` com a stack oficial do Langfuse auto-hospedado (web, worker, clickhouse, redis, minio e postgres próprio), sem afetar `docker compose up` sem perfil. `docs/observability.md` (em inglês) define o dashboard do Langfuse: custo por sessão (= conversa), latência p95 e taxa de fallback (spans `fallback` / traces), com os filtros exatos e o passo a passo, além do uso do Langfuse Cloud na demo pública (LAC-36). Nenhuma tela de dashboard no app (OBS-07).
- **Done when**:
  - [ ] teste prova que os serviços do Langfuse têm `profiles: ['observability']` e que `db`, `api` e `web` não têm perfil
  - [ ] teste prova que `docs/observability.md` contém as seções 'Cost per conversation', 'p95 latency' e 'Fallback rate'
  - [ ] `uv run pytest -q tests/unit/infra/test_compose_observability.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não expor o Langfuse publicamente

---

### TASK-139 — Phase 14 trace-to-eval demo

- **Requisito**: `OBS-05`, `DOD-02`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-133, TASK-136
- **Arquivos de produção**:
  - `demos/demo_14_trace_to_eval.py`
- **Arquivos de teste**:
  - `tests/integration/demos/test_demo_14.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-12 — `demos.common` (assinatura completa: plan 8.2) (consome)
  - CT-15 — `api.chat.service.ChatService.handle_message(request: ChatRequest, principal: CurrentCustomer | None) -> AsyncIterator[AgentEvent]` (assinatura completa: plan 8.2) (consome)
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Encena o fluxo: pergunta que recupera um chunk errado (documento de teste com informação desatualizada), feedback negativo, leitura dos spans de retrieval do trace (exporter em memória ou API do Langfuse com `--langfuse`), correção do documento, reingestão, criação do caso em `evals/datasets/phase_14.jsonl` e o caso passando.
- **Done when**:
  - [ ] teste com `--provider fake` prova as 5 etapas impressas e o caso novo aprovado pelo grader
  - [ ] `uv run pytest -q tests/integration/demos/test_demo_14.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não alterar o corpus versionado (usa cópia temporária)

---

### TASK-140 — Phase 14 ADR and eval cases

- **Requisito**: `DOD-01`, `DOD-04`
- **Tipo**: config
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-139, TASK-137, TASK-138
- **Arquivos de produção**:
  - `docs/adr/0014-observability.md`
  - `evals/datasets/phase_14.jsonl`
- **Arquivos de teste**:
  - `tests/unit/evals/test_phase_14_cases.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-10 — `{"id": str, "phase": int, "category": "rag"|"tool_trajectory"|"json_valid"|"red_team"|"tone"|"smoke", "language": "pt"|"en", "input": dict, "expected": dict, "grader": str}` (assinatura completa: plan 8.2) (consome)
- **Testes**: unit
- **Descrição**: ADR (OTel + Langfuse, trace_id visível, observabilidade alimenta evals) e casos pt/en.
- **Done when**:
  - [ ] teste prova ≥ 2 casos `phase == 14`, pt e en
  - [ ] `uv run pytest -q tests/unit/evals/test_phase_14_cases.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 1 testes, todos passando
- **Não fazer**:
  - Não criar a tag

---

## Demo pública

### TASK-141 — LLM call ledger, rate limit and maintenance schema

- **Requisito**: `DEMO-02`, `DEMO-03`
- **Tipo**: migration
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-140
- **Arquivos de produção**:
  - `api/limits/models.py`
  - `api/db/migrations/versions/0011_usage_and_maintenance.py`
- **Arquivos de teste**:
  - `tests/integration/api/limits/test_limits_models.py`
- **Wiring permitido**:
  - `api/limits/__init__.py` (criar vazio)
  - `api/db/migrations/env.py` (apenas importar `api.limits.models`)
- **Reusa**: —
- **Contrato**:
  - CT-66 — ORM `api.limits.models`: `LlmCall(id, trace_id, provider, model, cost_usd: Numeric(12,6), created_at)`, `RateLimitEvent(id, customer_id, created_at)`, `MaintenanceRun(id, job, run_date, finished_at)` com `UNIQUE(job, run_date)` (produz)
  - CT-5 — `api.db.engine` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Tabelas `llm_calls` (trace_id, provider, model, cost_usd numeric, created_at; índice por `created_at`), `rate_limit_events` (customer_id, created_at) e `maintenance_runs` (job, run_date UNIQUE).
- **Done when**:
  - [ ] teste prova `upgrade`/`downgrade` e `UNIQUE(job, run_date)`
  - [ ] `uv run pytest -q tests/integration/api/limits/test_limits_models.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não implementar regras

---

### TASK-142 — Persist estimated cost of every LLM call

- **Requisito**: `DEMO-03`
- **Tipo**: lógica-negócio
- **Risco**: crítico
- **Âncora de risco**: AS-2 (registro de gasto real — `api/limits/ledger.py`) — Jev c_dinheiro p=0.81
- **Perfil**: backend
- **Depende de**: TASK-141
- **Arquivos de produção**:
  - `api/limits/ledger.py`
- **Arquivos de teste**:
  - `tests/integration/api/limits/test_ledger.py`
- **Wiring permitido**:
  - `api/llm/instrumented.py` (apenas chamar `record_llm_call` após cada chamada, em sessão própria, sem falhar a requisição)
- **Reusa**: —
- **Contrato**:
  - CT-67 — `api.limits.ledger.record_llm_call(*, trace_id: str, provider: str, model: str, cost_usd: Decimal, created_at: datetime) -> None` (sessão própria; erro só gera log) (produz)
  - CT-66 — `api.limits.models` (assinatura completa: plan 8.2) (consome)
  - CT-7 — `api.llm.pricing.estimate_cost(model: str, usage: Usage) -> Decimal` (assinatura completa: plan 8.2) (consome)
  - CT-5 — `api.db.engine` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Toda chamada ao LLM (chat, resumo, triagem, juiz de tema) grava seu custo estimado. Falha ao gravar → log de erro, sem afetar a resposta.
- **Done when**:
  - [ ] teste prova 1 linha em `llm_calls` por chamada do cliente instrumentado, com o custo de `estimate_cost`
  - [ ] `uv run pytest -q tests/integration/api/limits/test_ledger.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não bloquear chamadas (task do teto)

---

### TASK-143 — Daily spend cap with UTC reset

- **Requisito**: `DEMO-03`, `DEMO-08`, `DEMO-09`
- **Tipo**: lógica-negócio
- **Risco**: crítico
- **Âncora de risco**: AS-2 (teto diário de gasto real — `api/limits/spend_cap.py`) — Jev c_dinheiro p=0.92
- **Perfil**: backend
- **Depende de**: TASK-142
- **Arquivos de produção**:
  - `api/limits/spend_cap.py`
- **Arquivos de teste**:
  - `tests/integration/api/limits/test_spend_cap.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-68 — `api.limits.spend_cap.SpendCap(settings).check(session, now: datetime) -> CapDecision(allowed: bool, spent_usd: Decimal, resets_at: datetime)` (produz)
  - CT-66 — `api.limits.models` (assinatura completa: plan 8.2) (consome)
  - CT-1 — `api.config.Settings` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Soma `cost_usd` de `llm_calls` com `created_at >= hoje 00:00 UTC`. ≥ `ATLAS_DAILY_SPEND_CAP_USD` (padrão 2,00) → `allowed=False` e `resets_at` = próxima 00:00 UTC (LAC-09).
- **Done when**:
  - [ ] teste prova bloqueio com gasto 2,00 e liberação com 1,99
  - [ ] teste prova que gasto de ontem (23:59 UTC) não conta hoje
  - [ ] teste prova `resets_at` = próxima 00:00 UTC
  - [ ] `uv run pytest -q tests/integration/api/limits/test_spend_cap.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não implementar rate limit por conta

---

### TASK-144 — Per-account hourly rate limiter

- **Requisito**: `DEMO-02`, `DEMO-08`, `DEMO-09`
- **Tipo**: lógica-negócio
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-141
- **Arquivos de produção**:
  - `api/limits/rate_limit.py`
- **Arquivos de teste**:
  - `tests/integration/api/limits/test_rate_limit.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-69 — `api.limits.rate_limit.RateLimiter(settings).check_and_record(session, customer_id: UUID, now: datetime) -> RateDecision(allowed: bool, retry_at: datetime | None)` (produz)
  - CT-66 — `api.limits.models` (assinatura completa: plan 8.2) (consome)
  - CT-1 — `api.config.Settings` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Janela deslizante de 1 hora por `customer_id`, com `ATLAS_RATE_LIMIT_PER_HOUR` (padrão 20). Lock consultivo por cliente na transação, para a contagem e o registro serem atômicos. Recusado → `retry_at` = horário em que o evento mais antigo da janela expira.
- **Done when**:
  - [ ] teste prova 20 permitidas e a 21ª recusada com `retry_at` correto
  - [ ] teste prova que 25 chamadas concorrentes resultam em exatamente 20 permitidas
  - [ ] `uv run pytest -q tests/integration/api/limits/test_rate_limit.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não limitar por IP (fora de escopo)

---

### TASK-145 — Limit enforcement on chat, confirmation and triage endpoints

- **Requisito**: `DEMO-02`, `DEMO-03`, `DEMO-09`
- **Tipo**: lógica-negócio
- **Risco**: crítico
- **Âncora de risco**: AS-2 (bloqueio por teto/rate limit — `api/limits/deps.py`) — Jev c_dinheiro p=0.80
- **Perfil**: backend
- **Depende de**: TASK-143, TASK-144
- **Arquivos de produção**:
  - `api/limits/deps.py`
- **Arquivos de teste**:
  - `tests/integration/api/limits/test_limit_enforcement.py`
- **Wiring permitido**:
  - `api/chat/routes.py`, `api/chat/stream_routes.py` e `api/triage/routes.py` (apenas adicionar as dependências `enforce_chat_limits`/`enforce_llm_budget`)
- **Reusa**: —
- **Contrato**:
  - CT-70 — `api.limits.deps`: dependências FastAPI `enforce_chat_limits(principal, session)` (rate limit + teto) e `enforce_llm_budget(session)` (só teto) → `AtlasError(RATE_LIMITED|DAILY_CAP_REACHED, 429, {retry_at})` (produz)
  - CT-68 — `api.limits.spend_cap.SpendCap(settings).check(session, now: datetime) -> CapDecision(allowed: bool, spent_usd: Decimal, resets_at: datetime)` (assinatura completa: plan 8.2) (consome)
  - CT-69 — `api.limits.rate_limit.RateLimiter(settings).check_and_record(session, customer_id: UUID, now: datetime) -> RateDecision(allowed: bool, retry_at: datetime | None)` (assinatura completa: plan 8.2) (consome)
  - CT-3 — `api.errors` (assinatura completa: plan 8.2) (consome)
  - CT-4 — `api.messages.user_message(key: MessageKey, lang: Lang, **params: object) -> str` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Mensagem nova de persona → rate limit + teto. Confirmação → só teto. Triagem por persona → rate limit + teto; por operador → só teto (LAC-35). Excedido → 429 `RATE_LIMITED`/`DAILY_CAP_REACHED` com a mensagem fixa traduzida e `retry_at`, sem chamar o LLM.
- **Done when**:
  - [ ] teste prova 429 `RATE_LIMITED` na 21ª mensagem com `retry_at` no corpo e 0 chamadas ao fake nessa requisição
  - [ ] teste prova 429 `DAILY_CAP_REACHED` em `/chat`, `/chat/stream` e `/triage` com o teto atingido
  - [ ] `uv run pytest -q tests/integration/api/limits/test_limit_enforcement.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não mudar regras de cálculo

---

### TASK-146 — Daily demo restore of seed data and conversations

- **Requisito**: `DEMO-07`, `DEMO-12`, `DEMO-91`
- **Tipo**: lógica-negócio
- **Risco**: crítico
- **Âncora de risco**: AS-5 (retenção — `api/maintenance/restore.py`), AS-3 (conversas com dados de visitantes) — Jev c_dado_existente p=0.89
- **Perfil**: backend
- **Depende de**: TASK-140
- **Arquivos de produção**:
  - `api/maintenance/restore.py`
- **Arquivos de teste**:
  - `tests/integration/api/maintenance/test_restore.py`
- **Wiring permitido**:
  - `api/maintenance/__init__.py` (criar vazio)
- **Reusa**: —
- **Contrato**:
  - CT-71 — `api.maintenance.restore.restore_demo(session, now: datetime) -> RestoreReport(conversations_deleted: int, orders_reset: int)` (produz)
  - CT-23 — `api.seed.personas.PERSONAS` (assinatura completa: plan 8.2) (consome)
  - CT-20 — `api.chat.repository.ConversationRepository(session)` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Numa transação: apaga as conversas das personas (as mensagens e confirmações vão em cascata) e recria pedidos, assinaturas e reembolsos pelo `seed_commerce`. NÃO apaga `audit_log`, `email_outbox`, tickets nem traces (DEMO-12, LAC-34).
- **Done when**:
  - [ ] teste prova que, após cancelar um pedido e conversar, a restauração volta o pedido ao status do seed e zera as conversas
  - [ ] teste prova que a contagem de `audit_log` é a mesma antes e depois
  - [ ] `uv run pytest -q tests/integration/api/maintenance/test_restore.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não expurgar por idade (task do purge)

---

### TASK-147 — Seven-day retention purge including Langfuse traces

- **Requisito**: `DEMO-10`, `DEMO-12`
- **Tipo**: lógica-negócio
- **Risco**: crítico
- **Âncora de risco**: AS-5 (expurgo de 7 dias — `api/maintenance/purge.py`), AS-3 — Jev c_dado_existente p=0.92
- **Perfil**: backend
- **Depende de**: TASK-146
- **Arquivos de produção**:
  - `api/maintenance/purge.py`
- **Arquivos de teste**:
  - `tests/integration/api/maintenance/test_purge.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**:
  - CT-72 — `api.maintenance.purge.purge_expired(session, now: datetime, retention_days: int, langfuse: LangfuseApi | None) -> PurgeReport(conversations: int, audit_rows: int, emails: int, tickets: int, llm_calls: int, langfuse_traces: int)` (produz)
  - CT-63 — `api.observability.langfuse_api.LangfuseApi(settings, http: httpx.AsyncClient | None = None)` (assinatura completa: plan 8.2) (consome)
  - CT-5 — `api.db.engine` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Apaga registros com mais de `ATLAS_RETENTION_DAYS` (7): conversas (com mensagens, feedback e confirmações), `audit_log`, `email_outbox`, `tickets` e `llm_calls`. Apaga os traces do Langfuse via `delete_traces_older_than` (LAC-36). Idempotente, devolve as contagens.
- **Done when**:
  - [ ] teste prova que registros de 8 dias são apagados e de 6 dias permanecem, em cada tabela
  - [ ] teste prova a chamada a `delete_traces_older_than` com corte = now - 7 dias (fake da API)
  - [ ] `uv run pytest -q tests/integration/api/maintenance/test_purge.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não apagar customers nem o seed

---

### TASK-148 — In-process daily maintenance scheduler with advisory lock

- **Requisito**: `DEMO-07`, `DEMO-10`
- **Tipo**: infra
- **Risco**: médio
- **Perfil**: backend
- **Depende de**: TASK-147
- **Arquivos de produção**:
  - `api/maintenance/scheduler.py`
- **Arquivos de teste**:
  - `tests/integration/api/maintenance/test_scheduler.py`
- **Wiring permitido**:
  - `api/main.py` (apenas iniciar/parar `start_scheduler` no lifespan quando `ATLAS_MAINTENANCE_ENABLED=true`)
- **Reusa**: —
- **Contrato**:
  - CT-73 — `api.maintenance.scheduler.start_scheduler(app) -> asyncio.Task` e `async tick(now: datetime) -> list[str]` (jobs executados) (produz)
  - CT-71 — `api.maintenance.restore.restore_demo(session, now: datetime) -> RestoreReport(conversations_deleted: int, orders_reset: int)` (assinatura completa: plan 8.2) (consome)
  - CT-72 — `api.maintenance.purge.purge_expired(session, now: datetime, retention_days: int, langfuse: LangfuseApi | None) -> PurgeReport(conversations: int, audit_rows: int, emails: int, tickets: int, llm_calls: int, langfuse_traces: int)` (assinatura completa: plan 8.2) (consome)
- **Testes**: integration
- **Descrição**: Laço asyncio que, a cada `ATLAS_MAINTENANCE_INTERVAL_SECONDS`, tenta `pg_try_advisory_lock` e, se não há linha de hoje (UTC) em `maintenance_runs` para o job, roda restore + purge e grava a linha (DA-26). Várias instâncias → uma só execução por dia.
- **Done when**:
  - [ ] teste prova que duas execuções do `tick` no mesmo dia rodam o restore uma única vez
  - [ ] teste prova que, com o lock tomado por outra conexão, o `tick` não roda nada
  - [ ] `uv run pytest -q tests/integration/api/maintenance/test_scheduler.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não depender de cron do PaaS

---

### TASK-149 — Render deployment blueprint for the public demo

- **Requisito**: `DEMO-05`, `DEMO-04`
- **Tipo**: infra
- **Risco**: alto
- **Âncora de risco**: AS-8 (primeira integração com o Render — `render.yaml`) — decisão humana LAC-41 (Jev a_terceiro p=0.44)
- **Perfil**: infra
- **Depende de**: TASK-140
- **Arquivos de produção**:
  - `render.yaml`
- **Arquivos de teste**:
  - `tests/unit/infra/test_render_blueprint.py`
- **Wiring permitido**: —
- **Reusa**: —
- **Contrato**: —
- **Testes**: unit
- **Descrição**: Blueprint Render (DA-27): web service Docker da API (`Dockerfile`, health `/health`), static site do `web` (build Angular, rewrite `/api/*` → API) e Postgres gerenciado. Variáveis: `CHAOS_RATE=0` fixo (DEMO-05), `ATLAS_MAINTENANCE_ENABLED=true`, `ATLAS_COOKIE_SECURE=true`, segredos com `sync: false` (Anthropic, OpenAI, Langfuse Cloud, operador). O `CREATE EXTENSION vector` sai da migration 0001.
- **Done when**:
  - [ ] teste prova `CHAOS_RATE` com valor '0' e que nenhum segredo tem valor literal no arquivo (`sync: false`)
  - [ ] teste prova `healthCheckPath: /health`
  - [ ] `uv run pytest -q tests/unit/infra/test_render_blueprint.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 2 testes, todos passando
  - [ ] `uv run ruff check . --output-format=concise` e `uv run mypy` sem erro novo nos arquivos da task
- **Não fazer**:
  - Não fazer o deploy (ação do Operador com a conta Render, seção 16)

---

### TASK-150 — Final English README with demo, phases table and results

- **Requisito**: `DOD-03`, `DOD-04`, `DEMO-04`, `DEMO-06`, `STR-06`
- **Tipo**: config
- **Risco**: médio
- **Perfil**: infra
- **Depende de**: TASK-149, TASK-148, TASK-145
- **Arquivos de produção**:
  - `README.md`
- **Arquivos de teste**:
  - `tests/unit/docs/test_readme.py`
- **Wiring permitido**:
  - `docs/img/architecture.svg` e `docs/img/demo.gif` (artefatos de mídia; o GIF é gravado pelo Operador)
- **Reusa**: —
- **Contrato**: —
- **Testes**: unit
- **Descrição**: Frase de impacto, GIF de ~30 s, tabela problema → conceito com link para cada tag `phase-NN` e a métrica de `docs/results.md`, diagrama de arquitetura, `docker compose up` em um comando, link da demo pública, credenciais das personas (DEMO-06), TTFT e tempo total do demo_09 (STR-06) e link para `docs/results.md`. Tudo em inglês.
- **Done when**:
  - [ ] teste prova as seções 'Live demo', 'Test accounts', 'Phases', 'Architecture', 'Quickstart', 'Results' e 14 links `phase-NN`
  - [ ] teste prova as duas credenciais de persona e uma linha com 'TTFT'
  - [ ] teste prova que `langdetect` classifica o README como `en`
  - [ ] `uv run pytest -q tests/unit/docs/test_readme.py` (executar em `.`) termina com exit 0 e o JUnit `reports/junit.xml` registra ≥ 3 testes, todos passando
- **Não fazer**:
  - Não colar segredos nem a chave de operador

---
