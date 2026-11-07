# Projeto de Portfólio — Agente de Suporte com IA

Nov 5, 2026 · @Rubens

## Visão geral

Um único app — **Atlas**, o assistente de suporte de uma loja fictícia por assinatura — que evolui em 14 fases, cada uma resolvendo o problema que a anterior criou. O diferencial no portfólio não é "ter RAG" ou "ter agente": é mostrar a cadeia de decisões de engenharia, com uma métrica de antes/depois em cada etapa.

**Domínio:** "Assinaflow", e-commerce por assinatura (planos, pedidos, reembolsos, entregas). Cobre todos os seus exemplos, usa só dados fictícios e qualquer recrutador entende em 10 segundos.

**Corpus:** \~40 documentos (manual do cliente, políticas internas, FAQ) em Markdown e PDF, escritos por você. Inclua de propósito sinônimos ("encerrar assinatura") e um PDF malicioso para a fase 10.

| Fase | Problema | Conceito |
| --- | --- | --- |
| 1 | O app precisa falar com uma LLM | Chamada à API |
| 2 | A LLM esquece a pergunta anterior | Janela de contexto |
| 3 | A LLM inventa respostas sobre o manual | RAG |
| 4 | Busca por palavra não acha sinônimos | Embeddings |
| 5 | Comparar com 1M de trechos é lento | Vector database |
| 6 | A LLM só responde texto, não age | Tool calling |
| 7 | A tarefa exige 5 ferramentas em sequência | Agente (loop) |
| 8 | O código espera JSON, vem "claro, aqui está" | Structured output |
| 9 | Resposta de 20 s parece travada | Streaming |
| 10 | Um PDF manda ignorar as instruções | Guardrails |
| 11 | Paga o mesmo prompt gigante toda vez | Prompt cache |
| 12 | O provedor cai no pico | Retry + fallback |
| 13 | Uma linha no prompt quebra 3 respostas | Evals |
| 14 | Ninguém sabe por que a resposta saiu errada | Observabilidade |

## Arquitetura e stack

Decisão central: escrever as primitivas você mesmo (loop do agente, retriever, fallback, guardrails), sem framework de agente. Isso prova que você entende o mecanismo; frameworks entram só como utilitário (ex.: loader de PDF).

| Camada | Escolha | Por quê |
| --- | --- | --- |
| API | Python 3.12 + FastAPI async + Pydantic v2 | Seu stack atual; Pydantic serve de schema para tools e structured output |
| Frontend | Angular, chat com SSE | Sua força; mostra full stack |
| Banco | PostgreSQL 16 + pgvector | Um só banco para conversas, pedidos fictícios e vetores |
| LLM | Claude como primário, um segundo provedor como fallback | Interface própria `LLMClient` com um adapter por provedor |
| Embeddings | API de embeddings (ex.: Voyage ou OpenAI) ou sentence-transformers local | Comparar custo e qualidade vira conteúdo |
| Observabilidade | OpenTelemetry + Langfuse self-hosted | Traces de prompt, tools, tokens e custo |
| Evals | pytest + dataset JSONL + LLM-as-judge | Roda no CI a cada PR |
| Infra | Docker Compose, GitHub Actions, deploy em PaaS | Demo pública com link no README |

Estrutura do repositório:

```
atlas-support-agent/
  api/          # FastAPI: chat, agent loop, tools, guardrails
    llm/        # LLMClient + adapters + retry/fallback
    rag/        # chunking, embeddings, retriever
    tools/      # pedidos, assinaturas, reembolsos (serviço fake)
  ingest/       # pipeline de ingestão do corpus
  web/          # Angular
  evals/        # datasets + runners
  docs/adr/     # uma ADR por fase
  docker-compose.yml
```

O diagrama abaixo mostra o caminho de uma mensagem com todas as fases prontas.

&#91;embedded content: arquitetura do Atlas · caminho de uma mensagem\]

A pergunta desce pelo guardrail de entrada até o loop do agente, que chama retriever, modelo e tools; a resposta sobe pelo guardrail de saída em stream, e cada passo vira um span no Langfuse.

## Roadmap — Fundamentos e recuperação (fases 1–5)

Cada fase tem três partes: o que construir, como reproduzir o problema que ela resolve e a métrica que prova a melhora.

### Fase 1 — Chamada à API

- **Construir:** `POST /chat` sem estado; interface `LLMClient` com um adapter; chave via variável de ambiente.
- **Problema reproduzido:** "Meu nome é Rubens" → "Qual é o meu nome?" → o modelo não sabe.
- **Prova:** script `demo_01_stateless.py` gravado no README.

### Fase 2 — Janela de contexto

- **Construir:** tabelas `conversations` e `messages`; envio do histórico a cada chamada; contagem de tokens; orçamento de contexto com corte das mensagens antigas e resumo delas.
- **Problema reproduzido:** conversa de 50 turnos estoura o limite ou fica cara.
- **Prova:** gráfico de tokens por chamada em 50 turnos, histórico completo vs resumo.

### Fase 3 — RAG

- **Construir:** pipeline `ingest/` (parse de MD/PDF, chunks de \~500 tokens com overlap, metadados de fonte); busca full-text do Postgres (`tsvector`); top-k injetado no prompt com citação; instrução de responder "não sei" fora dos trechos.
- **Problema reproduzido:** antes do RAG, perguntas sobre o manual geram respostas inventadas. Depois, "cancelar plano" não acha o trecho que diz "encerrar assinatura".
- **Prova:** taxa de alucinação em 30 perguntas, sem vs com RAG.

### Fase 4 — Embeddings

- **Construir:** embedding por chunk; similaridade de cosseno em NumPy, força bruta.
- **Problema reproduzido:** com 1M de vetores sintéticos, cada busca leva tempo demais.
- **Prova:** recall@5 de palavra-chave vs embedding num conjunto de perguntas com sinônimos; latência da força bruta em 10k, 100k e 1M vetores.

### Fase 5 — Vector database

- **Construir:** pgvector com índice HNSW; parâmetros `m` e `ef_search` configuráveis.
- **Bônus que impressiona:** busca híbrida (full-text + vetor com Reciprocal Rank Fusion) e um reranker.
- **Prova:** latência p50/p95 e recall@5 de força bruta vs HNSW em 1M vetores — o trade-off velocidade × precisão em uma tabela.

## Roadmap — Ação e experiência (fases 6–9)

### Fase 6 — Tool calling

- **Construir:** serviço fake de pedidos no Postgres (`orders`, `subscriptions`, `refunds`) e as tools `list_orders`, `get_order`, `cancel_order`, `get_subscription`, `check_refund_policy`, `issue_refund`, `send_email` (fake), com schemas gerados do Pydantic.
- **Decisão de engenharia:** o `user_id` vem da sessão no servidor, nunca de um argumento do modelo. Tools destrutivas exigem confirmação do usuário na UI.
- **Prova:** "cancela meu pedido" executa de verdade, com registro de auditoria.

### Fase 7 — Agente

- **Construir:** loop modelo → tool → resultado → modelo, até a resposta final; limite de passos (ex.: 8) e de custo; erro de tool devolvido como resultado para o modelo decidir; detecção de chamadas repetidas.
- **Cenário:** "Cancela meu último pedido, estorna no cartão e me manda o comprovante" → `list_orders` → `get_order` → `check_refund_policy` → `cancel_order` → `issue_refund` → `send_email`.
- **Prova:** taxa de sucesso e número médio de passos em 15 tarefas multi-step.

### Fase 8 — Structured output

- **Construir:** endpoint de triagem que devolve `{categoria, prioridade, sentimento, precisa_humano, resumo}`; schema obrigatório via recurso nativo do provedor; validação Pydantic; se falhar, nova tentativa com o erro de validação no prompt (máx. 2).
- **Problema reproduzido:** só com instrução no prompt, parte das respostas vem com texto em volta do JSON.
- **Prova:** % de JSON válido em 200 chamadas, só prompt vs schema obrigatório.

### Fase 9 — Streaming

- **Construir:** SSE do FastAPI até o Angular com eventos tipados (`token`, `tool_start`, `tool_end`, `citation`, `done`, `error`); botão de cancelar no meio da resposta.
- **Detalhe que mostra senioridade:** durante o loop do agente, a UI mostra o passo atual ("consultando pedido…"), não só os tokens finais.
- **Prova:** tempo até o primeiro token (TTFT) vs tempo total, medido e exibido no README.

## Roadmap — Produção (fases 10–14)

### Fase 10 — Guardrails

- **Construir em camadas:** (1) entrada: detecção de injection e de dados pessoais; (2) conteúdo recuperado: chunks marcados como dados não confiáveis no prompt e varredura de padrões de injection na ingestão, com quarentena; (3) tools: autorização no servidor (fase 6), então mesmo que o modelo obedeça ao PDF, não acessa dados de outros clientes; (4) saída: bloqueio de dados de outros usuários e de respostas fora da política.
- **Prova:** suíte red-team de 30 ataques, incluindo o PDF malicioso; taxa de bloqueio e taxa de falso positivo em perguntas legítimas.

### Fase 11 — Prompt cache

- **Construir:** prompt ordenado do estável para o variável — system, definições de tools e políticas fixas primeiro; trechos do RAG, histórico e pergunta depois; ponto de cache no fim do prefixo estável.
- **Lição para documentar:** qualquer dado dinâmico no topo (ex.: data/hora) invalida o cache.
- **Prova:** custo e latência por chamada e taxa de cache hit em 100 conversas, antes vs depois.

### Fase 12 — Retry e fallback

- **Construir:** timeout por chamada; retry com backoff exponencial e jitter só para erros recuperáveis (429, 5xx, timeout); circuit breaker por provedor; cadeia primário → modelo secundário → resposta degradada ("tente em instantes" + ticket aberto). Adapters normalizam o formato de tools entre provedores.
- **Teste de caos:** flag `CHAOS_RATE` injeta falhas no provedor primário.
- **Prova:** taxa de sucesso final e latência extra com 30% de falhas injetadas.

### Fase 13 — Evals

- **Construir:** dataset de \~100 casos em JSONL por categoria — RAG (resposta + citação correta), trajetória de tools, JSON válido, red-team, tom. Graders determinísticos onde der (sequência de tools, schema); LLM-as-judge para fidelidade ao contexto.
- **CI:** GitHub Actions roda os evals em todo PR que toca `prompts/` ou `api/`; bloqueia o merge se o score cair além de um limite que você define (ex.: 2 pontos).
- **Prova:** relatório de eval comentado no PR e um caso real de regressão capturada.

### Fase 14 — Observabilidade

- **Construir:** um trace por requisição com spans de guardrail, retrieval (chunks e scores), cada chamada LLM (modelo, prompt, tokens de entrada/saída/cache, custo, latência), cada tool (argumentos e resultado) e fallback acionado. `trace_id` visível no atendimento; feedback positivo/negativo ligado ao trace; dashboard de custo por conversa, p95 e taxa de fallback.
- **Prova (a demo que fecha o projeto):** cliente reclama → você abre o trace → acha o chunk errado → corrige → o caso vira teste no dataset de evals. Observabilidade alimenta evals.

## Ordem de construção vs ordem da história

O README conta a história de 1 a 14, mas construa um embrião das fases 13 e 14 logo no começo: sem medição, nenhuma fase tem "prova".

1. **Fase 0 — Fundação:** repo, Docker Compose, CI, `LLMClient`, log estruturado com `trace_id`, tokens e custo por chamada (embrião da 14) e um runner de eval mínimo em pytest (embrião da 13).
2. **Fases 1–5:** cada fase já entra com seus casos no dataset.
3. **Fases 6–9:** a autorização das tools nasce na fase 6, não na 10.
4. **Fases 10–12:** endurecimento para produção.
5. **Fases 13–14 completas:** Langfuse, gate de evals no CI, dashboard.

Cada fase termina com uma tag `phase-NN` no Git. Quem visita o repo pode fazer checkout da fase 3 e ver a falha do "cancelar plano" acontecendo.

## Cronograma e definição de pronto

Com 8–10 h por semana, o projeto cabe em 14 semanas: começa em 9 nov 2026 e fica pronto para divulgar até meados de fevereiro de 2027, antes da mudança em março. A fase de dezembro tem uma semana a mais por causa das festas.

&#91;embedded content: cronograma · 6 blocos, 14 semanas\]

O código fecha em 31 jan; as duas últimas semanas são só para README, vídeo, deploy e primeiros posts.

Uma fase só está pronta quando tem:

- [ ] Código funcionando e coberto por casos no dataset de evals
- [ ] Script `demo_NN` que reproduz o problema e mostra a solução
- [ ] Métrica de antes/depois registrada em `docs/results.md`
- [ ] ADR curta em inglês: contexto, decisão, alternativas descartadas
- [ ] Tag `phase-NN` no Git

## Como apresentar no portfólio

Tudo público em inglês, pensado para recrutador e entrevistador nos EUA.

- **README:** uma frase de impacto, GIF de 30 s, a tabela problema → conceito com link para cada tag e sua métrica, diagrama de arquitetura, `docker compose up` em um comando, e `docs/results.md`.
- **Demo pública:** login de teste, rate limit e teto de gasto diário (os próprios guardrails protegendo sua conta).
- **Vídeo de 3–5 min:** você narrando a cadeia de problemas em inglês — vira também treino para entrevistas.
- **Série de conteúdo:** um post no LinkedIn e um vídeo no @halotechlabs por fase; 14 peças de um só projeto.
- **Artigo técnico consolidado** ao final, com as métricas de todas as fases.
- **Entrevistas:** cada fase é uma história pronta de decisão técnica (problema, alternativas, trade-off, resultado medido) e cobre as perguntas típicas de system design de apps com LLM.
