# Especificação — Atlas: Agente de Suporte com IA (projeto completo, fases 0–14)

> Fonte: `/Users/rubensrudio/Projects/ai-support/Projeto de Portfólio — Agente de Suporte com IA.md`
> (doravante "documento-fonte"). Repositório greenfield: não há código, então
> todo termo do glossário vem do documento-fonte e é marcado `(novo)`.
> Revisão 2 (2026-11-07): decisões de `decisions.md` aplicadas (LAC-01..29).
> Revisão 3 (2026-11-07): decisões LAC-30..34 aplicadas.
> As escolhas de stack e de estrutura de repositório do documento-fonte
> (seção "Arquitetura e stack") são **insumo do `plan.md`**, não requisitos
> deste spec.

## 1. Problema

Rubens precisa de um projeto de portfólio, público e em inglês, que convença
recrutadores e entrevistadores nos EUA de que ele domina engenharia de
aplicações com LLM. "Ter RAG" ou "ter agente" não diferencia ninguém. O que
diferencia é mostrar a **cadeia de decisões de engenharia**: cada capacidade
nasce de um problema concreto da etapa anterior, reproduzível, com métrica
de antes e depois.

O produto é o **Atlas**, assistente de suporte da **Assinaflow**, um
e-commerce fictício por assinatura (planos, pedidos, reembolsos, entregas).
Ele evolui em 14 fases, da chamada simples ao LLM até observabilidade
alimentando evals. Cada fase precisa de um script que reproduz o problema,
uma métrica registrada, uma ADR e uma tag Git, para que qualquer visitante
faça checkout da fase e veja a falha acontecer.

O prazo é apertado: 8–10 h por semana, início em 9 nov 2026, código fechado
em 31 jan 2027 e divulgação até meados de fevereiro de 2027. Sem medição
desde o início, nenhuma fase tem "prova". Por isso há uma Fase 0 com um
embrião de evals e de observabilidade.

## 2. Objetivos

- [ ] Um visitante sobe o sistema inteiro com um único comando e conversa com o Atlas localmente.
- [ ] Cada uma das fases 1–14 tem tag `phase-NN`, script `demo_NN` que reproduz o problema e a solução, métrica de antes/depois em `docs/results.md` e ADR em inglês.
- [ ] O Atlas responde a perguntas sobre o corpus com citação e diz "não sei" fora dele.
- [ ] O Atlas executa ações reais sobre pedidos fictícios do cliente autenticado (cancelar, estornar, enviar comprovante), com confirmação e auditoria, sem acessar dados de outro cliente.
- [ ] Uma alteração de prompt que piora a qualidade é barrada no PR pelo gate de evals.
- [ ] Toda resposta pode ser investigada por um trace que mostra guardrails, trechos recuperados, chamadas ao LLM, tools, custo e fallback.
- [ ] Uma demo pública com login de teste fica no ar, protegida por rate limit e teto de gasto diário.

## 3. Fora de Escopo

| Item | Motivo da exclusão |
|---|---|
| Vídeo de 3–5 min, posts no LinkedIn, vídeos no @halotechlabs, artigo técnico consolidado | São conteúdo de divulgação, não software. O documento-fonte os lista em "Como apresentar". |
| Framework de agente (orquestração pronta de loop/tools/RAG) | Decisão central do documento-fonte: as primitivas são escritas à mão. Frameworks só entram como utilitário (ex.: loader de PDF). |
| Envio real de e-mail, cobrança ou estorno real, integração com gateway de pagamento | Domínio fictício. `send_email` é fake e reembolso é só registro. |
| Tools além das sete listadas (ex.: cancelar/alterar assinatura, alterar endereço) | O documento-fonte define exatamente sete tools. Nesses assuntos, o Atlas responde só pelo corpus. |
| Console de atendente humano / fila de tickets com interface | O documento-fonte exige só que o ticket seja aberto (registro) na resposta degradada. |
| Interface de revisão e liberação de documentos em quarentena | O documento-fonte exige quarentena na ingestão, não fluxo de revisão. |
| Cadastro de usuários reais, recuperação de senha, login social | Acesso só pelas personas fixas de teste (`DEMO-06`). |
| Múltiplas lojas / multi-tenant de lojistas | Domínio único: Assinaflow. |
| Dados reais de clientes | Corpus e dados são fictícios, escritos pelo autor. |
| Reranker na busca | Premissa LAC-18: o "bônus" da fase 5 entra só como busca híbrida. |
| Comparação de custo e qualidade entre provedores de embeddings como entregável | Premissa LAC-19. |
| Conformidade formal de acessibilidade (nível WCAG) | Não há exigência no documento-fonte. Ver NFR. |
| Aplicativo mobile | O frontend previsto é web. |
| Idiomas além de português e inglês | Decisão LAC-01: produto bilíngue PT/EN. O comportamento diante de outro idioma está em `CHAT-95`. |

## 4. Glossário de Domínio

Repositório greenfield: nenhum termo existe ainda no código. A coluna "Onde
aparece" cita a seção do documento-fonte. Os nomes entre crases são
vocabulário **obrigatório**: o código deve usá-los exatamente assim.

| Termo | Significado | Onde aparece no código |
|---|---|---|
| Atlas (novo) | O assistente de suporte (o produto). | — (doc-fonte: Visão geral) |
| Assinaflow (novo) | A loja fictícia por assinatura que o Atlas atende. | — (doc-fonte: Visão geral) |
| Cliente (novo) | Usuário final autenticado por login de teste, dono de pedidos, assinatura e reembolsos fictícios. | — (doc-fonte: Fase 6, Demo pública) |
| Operador (novo) | O mantenedor do projeto (Rubens). Opera ingestão, evals, observabilidade e deploy. | — |
| Conversa / `conversations` (novo) | Sequência de mensagens entre um Cliente e o Atlas. | — (doc-fonte: Fase 2) |
| Mensagem / `messages` (novo) | Turno do usuário ou do assistente dentro de uma Conversa. | — (doc-fonte: Fase 2) |
| Orçamento de contexto (novo) | Limite de tokens de histórico enviado ao LLM. Acima dele, as mensagens antigas viram resumo. | — (doc-fonte: Fase 2) |
| Corpus (novo) | ~40 documentos (manual do cliente, políticas internas, FAQ) em Markdown e PDF. | — (doc-fonte: Visão geral) |
| Trecho / chunk (novo) | Pedaço de ~500 tokens de um documento do corpus, com sobreposição e metadados de fonte. | — (doc-fonte: Fase 3) |
| Citação (novo) | Referência à fonte (documento/seção) de um trecho usado na resposta. | — (doc-fonte: Fase 3, Fase 9) |
| Ingestão (novo) | Pipeline que lê o corpus, gera trechos, embeddings e índices. | — (doc-fonte: Fase 3) |
| Quarentena (novo) | Estado de um documento cuja ingestão detectou padrão de injection. Não é indexado. | — (doc-fonte: Fase 10) |
| Busca full-text (novo) | Recuperação por palavra-chave (`tsvector`). | — (doc-fonte: Fase 3) |
| Busca vetorial / embeddings (novo) | Recuperação por similaridade de cosseno entre embeddings. | — (doc-fonte: Fases 4–5) |
| Busca híbrida / RRF (novo) | Fusão de full-text e vetorial por Reciprocal Rank Fusion. | — (doc-fonte: Fase 5, bônus) |
| Pedido / `orders` (novo) | Pedido fictício de um Cliente. | — (doc-fonte: Fase 6) |
| Assinatura / `subscriptions` (novo) | Plano de assinatura fictício de um Cliente. | — (doc-fonte: Fase 6) |
| Reembolso / `refunds` (novo) | Registro de estorno fictício ligado a um Pedido. | — (doc-fonte: Fase 6) |
| Tool (novo) | Função que o modelo pode solicitar: `list_orders`, `get_order`, `cancel_order`, `get_subscription`, `check_refund_policy`, `issue_refund`, `send_email`. | — (doc-fonte: Fase 6) |
| Tool destrutiva (novo) | Tool que muda estado ou dinheiro e exige confirmação do Cliente na UI. São `cancel_order` e `issue_refund` (`TOOL-12`). | — (doc-fonte: Fase 6) |
| Registro de auditoria (novo) | Registro persistente de cada execução de tool com efeito. | — (doc-fonte: Fase 6) |
| Agente / loop / passo (novo) | Ciclo modelo → tool → resultado → modelo. Cada iteração é um passo. | — (doc-fonte: Fase 7) |
| Triagem (novo) | Classificação estruturada `{categoria, prioridade, sentimento, precisa_humano, resumo}`. | — (doc-fonte: Fase 8) |
| Evento SSE (novo) | Unidade do streaming: `token`, `tool_start`, `tool_end`, `citation`, `done`, `error`. | — (doc-fonte: Fase 9) |
| Guardrail (novo) | Controle em camada: entrada, conteúdo recuperado, tools e saída. | — (doc-fonte: Fase 10) |
| Prefixo estável (novo) | Parte inicial do prompt que não muda entre chamadas (system, definições de tools, políticas fixas), onde fica o ponto de cache. | — (doc-fonte: Fase 11) |
| Provedor primário / secundário (novo) | LLM principal (Claude) e o de outro provedor, usado como fallback. | — (doc-fonte: Arquitetura, Fase 12) |
| Resposta degradada (novo) | Resposta "tente em instantes" com ticket aberto, quando todos os provedores falham. | — (doc-fonte: Fase 12) |
| Ticket (novo) | Registro de atendimento pendente aberto na resposta degradada. | — (doc-fonte: Fase 12) |
| Circuit breaker (novo) | Estado por provedor que, após falhas, para de chamá-lo por um período. | — (doc-fonte: Fase 12) |
| `CHAOS_RATE` (novo) | Configuração que injeta falhas no provedor primário com a taxa informada. | — (doc-fonte: Fase 12) |
| Eval / dataset de evals (novo) | Casos em JSONL por categoria, avaliados por graders determinísticos ou LLM-as-judge. | — (doc-fonte: Fase 13) |
| Trace / `trace_id` / span (novo) | Registro de uma requisição e de seus passos, identificado por `trace_id`. | — (doc-fonte: Fase 0, Fase 14) |
| Feedback (novo) | Avaliação positiva/negativa de uma resposta, ligada ao trace. | — (doc-fonte: Fase 14) |
| Fase / tag `phase-NN` (novo) | Etapa do roadmap. Cada uma termina numa tag Git. | — (doc-fonte: Ordem de construção) |
| `demo_NN` (novo) | Script da fase NN que reproduz o problema e mostra a solução. | — (doc-fonte: Definição de pronto) |
| `docs/results.md` (novo) | Registro das métricas de antes/depois de cada fase. | — (doc-fonte: Definição de pronto) |
| ADR (novo) | Registro curto em inglês: contexto, decisão, alternativas descartadas. Uma por fase. | — (doc-fonte: Definição de pronto) |
| Persona de teste (novo) | Conta fixa e compartilhada de login de teste, com dados fictícios próprios restaurados diariamente (`DEMO-06`, `DEMO-07`). | — (doc-fonte: Demo pública) |
| Idioma da conversa (novo) | Português ou inglês: o idioma em que o Cliente escreve. O Atlas responde nele (`CHAT-10`). | — (`decisions.md`, LAC-01) |

## 5. Atores e Permissões

| Ator | Ação | Condição / restrição |
|---|---|---|
| Visitante não autenticado | Ver o frontend e a tela de login de teste | NÃO conversa com o Atlas, NÃO acessa dados. |
| Cliente (persona de teste autenticada) | Conversar com o Atlas, confirmar ou recusar tools destrutivas, cancelar uma resposta em andamento, dar feedback | Só acessa as próprias Conversas, Pedidos, Assinatura e Reembolsos. Sujeito a rate limit e ao teto de gasto diário (`DEMO-08`, `DEMO-09`). Usa uma das personas fixas compartilhadas (`DEMO-06`). |
| Modelo (LLM) | Solicitar tools com argumentos | NUNCA escolhe a identidade do cliente: o `user_id` vem da sessão no servidor. NÃO executa tool destrutiva sem confirmação do Cliente. NÃO escolhe o destinatário de `send_email` (`TOOL-18`). |
| Operador | Rodar a ingestão, ver traces e dashboard, editar o dataset de evals, configurar limites e `CHAOS_RATE`, fazer deploy | Acesso à ferramenta de observabilidade restrito ao Operador. O dashboard existe só na ferramenta de observabilidade (`OBS-07`). O Cliente vê só o `trace_id` de cada resposta (`OBS-06`). |
| CI (pipeline de PR) | Rodar lint, testes e evals, comentar o relatório no PR, bloquear o merge | Só roda evals em PR que toca `prompts/` ou `api/`. |

Repositório greenfield: não há controle de permissão existente. Toda a matriz
acima é nova.

## 6. Histórias de Usuário

Prioridade segue a ordem de construção do documento-fonte: P1 = Fundação e
fases 1–5 (+ definição de pronto), P2 = fases 6–9, P3 = fases 10–14 e demo
pública. Todas estão no escopo do projeto. A prioridade define a ordem e o
que forma o MVP demonstrável.

Prefixos de ID por área: `FND` (Fase 0), `CHAT` (fases 1–2), `RAG` (fases
3–5), `DOD` (definição de pronto), `TOOL` (6), `AGT` (7), `TRI` (8), `STR`
(9), `GRD` (10), `CACHE` (11), `RES` (12), `EVAL` (13), `OBS` (14), `DEMO`
(demo pública). Casos de borda usam a faixa `-90+` de cada prefixo (seção 8).

### P1: Fundação mensurável (Fase 0) ⭐ MVP

**História**: Como Operador, quero subir o projeto com um comando e ter, desde o primeiro dia, log de custo por chamada e um runner mínimo de evals, para que toda fase nasça com prova medida.

**Por que P1**: Sem medição, nenhuma fase tem métrica de antes/depois. O documento-fonte manda construir este embrião antes da fase 1.

**Critérios de aceite**:

| ID | Critério |
|---|---|
| `FND-01` | WHEN o Operador executa `docker compose up` num clone limpo, com as variáveis de ambiente documentadas preenchidas, THEN o sistema SHALL subir API, banco e frontend e responder com sucesso a uma verificação de saúde da API |
| `FND-02` | WHEN um PR é aberto ou atualizado THEN o CI SHALL executar lint, testes automatizados e o runner mínimo de evals, e SHALL marcar o PR como falho se qualquer um falhar |
| `FND-03` | WHEN uma requisição HTTP chega à API THEN o sistema SHALL atribuir a ela um `trace_id` único e SHALL incluí-lo em todos os logs estruturados gerados por essa requisição |
| `FND-04` | WHEN uma chamada ao LLM termina (com sucesso ou erro) THEN o sistema SHALL emitir um log estruturado com `trace_id`, modelo, tokens de entrada, tokens de saída e custo estimado da chamada |
| `FND-05` | WHEN o runner de evals é executado sobre um dataset JSONL THEN ele SHALL produzir o resultado de cada caso e um score agregado num relatório legível por máquina |

**Teste independente**: num clone limpo, rodar `docker compose up`, fazer uma chamada ao LLM, conferir o log com `trace_id`/tokens/custo e rodar o runner de evals sobre um dataset de 1 caso.

### P1: Conversa com o Atlas e memória de contexto (Fases 1–2) ⭐ MVP

**História**: Como Cliente, quero conversar com o Atlas e que ele lembre o que eu disse antes, sem que conversas longas estourem o limite ou o custo.

**Por que P1**: É o caminho básico do produto e a base das fases seguintes. Sem ele não existe atendimento.

**Critérios de aceite**:

| ID | Critério |
|---|---|
| `CHAT-01` | WHEN um Cliente envia uma mensagem para `POST /chat` THEN o sistema SHALL devolver a resposta gerada pelo provedor primário |
| `CHAT-02` | WHEN `demo_01` é executado na tag `phase-01`, enviando "meu nome é Rubens" e depois "qual é o meu nome?" em chamadas independentes, THEN a segunda resposta SHALL não conter o nome informado (problema reproduzido) |
| `CHAT-03` | WHEN uma mensagem é enviada numa Conversa THEN o sistema SHALL persistir a mensagem do usuário e a resposta do assistente em `messages`, vinculadas à `conversations` correspondente, na ordem em que ocorreram |
| `CHAT-04` | WHEN uma mensagem é enviada numa Conversa existente THEN o sistema SHALL incluir o histórico dessa Conversa na chamada ao LLM, de modo que o cenário de `CHAT-02` (a partir de `phase-02`) responda o nome corretamente |
| `CHAT-05` | WHEN uma chamada ao LLM é montada THEN o sistema SHALL contar os tokens do contexto enviado e registrá-los no log da chamada |
| `CHAT-06` | WHEN o histórico de uma Conversa excede o orçamento de contexto THEN o sistema SHALL substituir as mensagens mais antigas por um resumo delas, manter íntegras as mais recentes e SHALL NOT enviar ao LLM um histórico acima do orçamento (valor padrão: `CHAT-08`) |
| `CHAT-07` | WHEN `demo_02` executa uma conversa de 50 turnos nos modos "histórico completo" e "com resumo" THEN ele SHALL gerar o gráfico de tokens por chamada dos dois modos e registrar o resultado em `docs/results.md` |
| `CHAT-08` | WHEN o orçamento de contexto não está configurado THEN o sistema SHALL usar 8.000 tokens de histórico como padrão, e o valor SHALL ser alterável por configuração sem mudar código (LAC-20) |
| `CHAT-09` | WHEN o tamanho máximo de mensagem não está configurado THEN o sistema SHALL usar 4.000 caracteres como padrão, e o valor SHALL ser alterável por configuração sem mudar código (LAC-26) |
| `CHAT-10` | WHEN o Cliente escreve em português ou em inglês THEN o Atlas SHALL responder no mesmo idioma da mensagem (LAC-01) |

> Decisões aplicadas: LAC-20 → `CHAT-08`; LAC-26 → `CHAT-09`; LAC-01 → `CHAT-10`. LAC-31 → `CHAT-95`.

**Teste independente**: na tag `phase-02`, rodar `demo_01` (agora lembra o nome) e `demo_02` (gráfico gerado).

### P1: Respostas fundamentadas no corpus (Fases 3–5) ⭐ MVP

**História**: Como Cliente, quero respostas sobre a Assinaflow baseadas no manual e nas políticas, com a fonte citada, mesmo quando uso palavras diferentes das do documento, para confiar no que o Atlas diz.

**Por que P1**: Resolve a alucinação, o principal risco de um assistente de suporte, e cobre as fases 3–5 do roadmap.

**Critérios de aceite**:

| ID | Critério |
|---|---|
| `RAG-01` | WHEN a ingestão é executada sobre o corpus (Markdown e PDF) THEN ela SHALL dividir cada documento em trechos de ~500 tokens com sobreposição, cada um com metadados de fonte (documento e seção ou página) |
| `RAG-02` | WHEN a ingestão é executada sobre o corpus versionado THEN o relatório de ingestão SHALL mostrar documentos dos três tipos (manual do cliente, política interna, FAQ), nos dois formatos (Markdown e PDF), ao menos um par de sinônimos documentado (ex.: "cancelar plano" / "encerrar assinatura") e o PDF malicioso da fase 10 |
| `RAG-03` | WHEN o Cliente faz uma pergunta coberta pelo corpus THEN o sistema SHALL recuperar os top-k trechos, incluí-los no contexto do LLM e a resposta SHALL citar a fonte de cada trecho usado |
| `RAG-04` | WHEN nenhum trecho recuperado sustenta a resposta THEN o Atlas SHALL responder que não sabe e SHALL NOT apresentar informação sobre a Assinaflow que não esteja nos trechos |
| `RAG-05` | WHEN `demo_03` é executado na tag `phase-03` com a pergunta "cancelar plano" THEN a busca full-text SHALL não retornar o trecho que fala em "encerrar assinatura" (problema reproduzido) |
| `RAG-06` | WHEN `demo_03` executa as 30 perguntas de referência sem RAG e com RAG THEN ele SHALL registrar a taxa de alucinação de cada modo em `docs/results.md` (método: `RAG-18`) |
| `RAG-07` | WHEN a ingestão é executada a partir da fase 4 THEN ela SHALL gerar e persistir um embedding para cada trecho indexado |
| `RAG-08` | WHEN a busca vetorial da fase 4 recebe "cancelar plano" THEN ela SHALL ordenar os trechos por similaridade de cosseno (força bruta) e o trecho de "encerrar assinatura" SHALL estar entre os 5 primeiros |
| `RAG-09` | WHEN `demo_04` é executado THEN ele SHALL registrar em `docs/results.md` o recall@5 de palavra-chave vs embedding no conjunto de perguntas com sinônimos e a latência da força bruta com 10k, 100k e 1M vetores |
| `RAG-10` | WHEN a busca usa o índice HNSW (fase 5) THEN os parâmetros `m` e `ef_search` SHALL ser alteráveis por configuração, sem mudar código |
| `RAG-11` | WHEN `demo_05` é executado THEN ele SHALL registrar em `docs/results.md` uma tabela com latência p50/p95 e recall@5 de força bruta vs HNSW sobre 1M vetores |
| `RAG-14` | WHEN a ingestão é executada sobre o corpus THEN todo documento do corpus SHALL existir em português e em inglês, e cada trecho SHALL ter o idioma nos metadados (LAC-01) |
| `RAG-15` | WHEN a busca full-text indexa ou consulta trechos THEN ela SHALL aplicar a análise linguística (radicais e palavras vazias) do idioma do trecho e do idioma da pergunta, e SHALL recuperar trechos do idioma da pergunta (LAC-01) |
| `RAG-16` | WHEN `demo_03` é executado na tag `phase-03` com a pergunta "cancel plan" THEN a busca full-text SHALL não retornar o trecho que fala em "end subscription" (problema reproduzido também em inglês) (LAC-01) |
| `RAG-17` | WHEN a busca vetorial da fase 4 recebe "cancel plan" THEN o trecho em inglês sobre "end subscription" SHALL estar entre os 5 primeiros (LAC-01) |
| `RAG-18` | WHEN `demo_03` mede a taxa de alucinação THEN ele SHALL usar LLM-as-judge com rubrica de fidelidade aos trechos e SHALL registrar em `docs/results.md` o resultado da revisão manual de uma amostra das respostas, com o tamanho da amostra (LAC-24) |
| `RAG-19` | WHEN as 30 perguntas de referência de `demo_03` são carregadas THEN elas SHALL incluir perguntas em português e em inglês, e a taxa de alucinação SHALL ser registrada por idioma (LAC-01) |

> Decisões aplicadas: LAC-01 → `RAG-14`, `RAG-15`, `RAG-16`, `RAG-17`, `RAG-19`; LAC-24 → `RAG-18`.

**Teste independente**: nas tags `phase-03`, `phase-04` e `phase-05`, rodar o `demo_NN` correspondente e conferir a métrica em `docs/results.md`.

### P1: Definição de pronto por fase e vitrine do repositório ⭐ MVP

**História**: Como recrutador ou entrevistador, quero fazer checkout de qualquer fase e ver o problema e a solução acontecerem, com a métrica e a decisão documentadas, para avaliar o raciocínio de engenharia do autor.

**Por que P1**: É o diferencial do portfólio. Sem esses artefatos, as fases viram só código.

**Critérios de aceite**:

| ID | Critério |
|---|---|
| `DOD-01` | WHEN uma fase NN (1–14) é declarada pronta THEN o repositório SHALL conter casos dessa fase no dataset de evals, o script `demo_NN`, a métrica de antes/depois em `docs/results.md`, uma ADR em inglês (contexto, decisão, alternativas descartadas) e a tag `phase-NN` |
| `DOD-02` | WHEN um visitante faz checkout da tag `phase-NN` e executa `demo_NN` THEN o script SHALL reproduzir o problema da fase e mostrar a solução (ex.: em `phase-03`, a falha de "cancelar plano") |
| `DOD-03` | WHEN o README é publicado THEN ele SHALL estar em inglês e conter: frase de impacto, GIF de ~30 s, tabela problema → conceito com link para cada tag e sua métrica, diagrama de arquitetura, instrução de `docker compose up` em um comando e link para `docs/results.md` |
| `DOD-04` | WHEN qualquer artefato público é publicado (README, ADRs, `docs/results.md`, comentários de PR do CI) THEN ele SHALL estar em inglês |

**Teste independente**: escolher uma tag `phase-NN` ao acaso, fazer checkout, rodar `demo_NN` e conferir ADR, métrica e casos de eval da fase.

### P2: Ações sobre pedidos com autorização e confirmação (Fase 6)

**História**: Como Cliente, quero pedir ao Atlas que consulte e cancele meus pedidos e solicite reembolso, confirmando antes qualquer ação que mude algo, para resolver o problema sem falar com um humano.

**Por que P2**: Transforma o Atlas de "responde" em "age". Depende do chat (P1) e não bloqueia o MVP de respostas.

**Critérios de aceite**:

| ID | Critério |
|---|---|
| `TOOL-01` | WHEN o ambiente é inicializado THEN o serviço fake SHALL conter dados fictícios de `orders`, `subscriptions` e `refunds` para cada persona de teste |
| `TOOL-02` | WHEN o modelo recebe as definições de tools THEN ele SHALL receber exatamente `list_orders`, `get_order`, `cancel_order`, `get_subscription`, `check_refund_policy`, `issue_refund` e `send_email`, com schemas gerados das mesmas definições que validam os argumentos |
| `TOOL-03` | WHEN qualquer tool é executada THEN a identidade do cliente SHALL vir da sessão autenticada no servidor, e nenhum schema de tool SHALL aceitar identificador de cliente como argumento |
| `TOOL-04` | WHEN uma tool lê ou altera Pedido, Assinatura ou Reembolso THEN ela SHALL operar apenas sobre registros do cliente da sessão |
| `TOOL-05` | WHEN o modelo solicita uma tool destrutiva THEN o sistema SHALL suspender a execução, a UI SHALL mostrar ao Cliente a ação e o alvo (ex.: "cancelar pedido #123") e a tool SHALL ser executada somente após confirmação explícita do Cliente (conjunto: `TOOL-12`) |
| `TOOL-06` | WHEN `cancel_order` é executada com sucesso THEN o Pedido SHALL passar ao estado cancelado e o sistema SHALL gravar um registro de auditoria com cliente, tool, argumentos, resultado, data/hora e `trace_id` |
| `TOOL-07` | WHEN `issue_refund` é executada com sucesso THEN o sistema SHALL criar um Reembolso vinculado ao Pedido e gravar registro de auditoria com os mesmos campos de `TOOL-06` |
| `TOOL-08` | WHEN `send_email` é executada THEN o sistema SHALL registrar o e-mail (destinatário, assunto, corpo) sem envio real e gravar registro de auditoria |
| `TOOL-09` | WHEN `check_refund_policy` é executada para um Pedido do cliente THEN ela SHALL devolver se o Pedido é elegível a reembolso e o motivo |
| `TOOL-10` | WHEN `get_subscription` ou `list_orders` é executada THEN ela SHALL devolver somente a Assinatura ou os Pedidos do cliente da sessão, sem efeito colateral |
| `TOOL-11` | WHEN `demo_06` envia "cancela meu pedido" e o Cliente confirma THEN o Pedido SHALL estar cancelado no banco e o registro de auditoria correspondente SHALL existir |
| `TOOL-12` | WHEN o modelo solicita `cancel_order` ou `issue_refund` THEN o sistema SHALL tratá-las como tools destrutivas (`TOOL-05`); WHEN solicita qualquer outra tool THEN SHALL executá-la sem pedir confirmação (LAC-03) |
| `TOOL-13` | WHEN o Cliente recusa a confirmação de uma tool destrutiva THEN o sistema SHALL NOT executá-la, SHALL devolver ao modelo, como resultado da tool, que o cliente recusou, e o loop SHALL continuar (LAC-04) |
| `TOOL-14` | WHEN, depois de uma recusa, o modelo solicita de novo a mesma tool com os mesmos argumentos na mesma requisição THEN o sistema SHALL NOT executá-la nem pedir nova confirmação, e SHALL devolver erro ao modelo (LAC-04) |
| `TOOL-15` | WHEN `issue_refund` é chamada para um Pedido não elegível pela regra de `TOOL-16` THEN o servidor SHALL recusar, devolver erro ao modelo e SHALL NOT criar Reembolso, tenha ou não `check_refund_policy` sido chamada antes (LAC-05) |
| `TOOL-16` | WHEN a elegibilidade de reembolso é avaliada (por `check_refund_policy` ou por `issue_refund`) THEN o Pedido SHALL ser elegível somente se foi cancelado antes do envio ou se foi entregue há até 7 dias (inclusive); o reembolso SHALL ser do valor integral do Pedido; e a mesma regra SHALL constar do corpus nos dois idiomas (LAC-06) |
| `TOOL-17` | WHEN `cancel_order` é avaliada THEN os estados do Pedido SHALL ser pendente, pago, enviado, entregue e cancelado, e o cancelamento SHALL ser permitido somente em pendente ou pago (LAC-07) |
| `TOOL-18` | WHEN `send_email` é executada THEN o destinatário SHALL ser sempre o e-mail do cliente da sessão, e o schema da tool SHALL NOT ter campo de destinatário (LAC-27) |
| `TOOL-19` | WHEN valores monetários de Pedidos, Reembolsos ou Assinaturas são gravados, devolvidos por tool, citados no corpus ou exibidos THEN a moeda SHALL ser sempre USD, e o valor exibido ao Cliente SHALL seguir a formatação do idioma da conversa (LAC-30) |

> Decisões aplicadas: LAC-03 → `TOOL-12`; LAC-04 → `TOOL-13`, `TOOL-14`, `TOOL-97`; LAC-05 → `TOOL-15`; LAC-06 → `TOOL-16`, `TOOL-98`; LAC-07 → `TOOL-17`, `TOOL-99`; LAC-27 → `TOOL-18`. LAC-30 → `TOOL-19`.

**Teste independente**: na tag `phase-06`, autenticar como persona de teste, rodar `demo_06` e conferir no banco o Pedido cancelado e a auditoria.

### P2: Agente multi-step (Fase 7)

**História**: Como Cliente, quero pedir algo que exige várias ações em sequência ("cancela meu último pedido, estorna no cartão e me manda o comprovante") e ver o Atlas concluir tudo.

**Por que P2**: Mostra o loop do agente, mas depende das tools (P2) e não bloqueia o MVP.

**Critérios de aceite**:

| ID | Critério |
|---|---|
| `AGT-01` | WHEN o modelo solicita uma tool THEN o loop SHALL executá-la, devolver o resultado ao modelo e chamá-lo de novo, repetindo até o modelo produzir a resposta final |
| `AGT-02` | WHEN o número de passos de uma requisição atinge o limite configurado (padrão 8, conforme documento-fonte) THEN o sistema SHALL NOT executar nenhuma tool adicional naquela requisição (em seguida: `AGT-08`) |
| `AGT-03` | WHEN o custo acumulado de uma requisição atinge o limite de custo configurado THEN o sistema SHALL NOT executar nenhuma tool adicional nem nova chamada de passo ao modelo com tools (em seguida: `AGT-08`) |
| `AGT-04` | WHEN uma tool falha THEN o erro SHALL ser devolvido ao modelo como resultado da tool, e o loop SHALL continuar |
| `AGT-05` | WHEN o loop detecta chamada repetida (critério e ação: `AGT-09`, `AGT-10`) THEN o sistema SHALL registrar a detecção no log da requisição |
| `AGT-06` | WHEN o Cliente pede "cancela meu último pedido, estorna no cartão e me manda o comprovante" e confirma as ações THEN a trajetória de tools SHALL ser `list_orders` → `get_order` → `check_refund_policy` → `cancel_order` → `issue_refund` → `send_email` |
| `AGT-07` | WHEN `demo_07` executa as 15 tarefas multi-step de referência THEN ele SHALL registrar em `docs/results.md` a taxa de sucesso e o número médio de passos |
| `AGT-08` | WHEN o limite de passos ou de custo é atingido THEN o sistema SHALL encerrar o loop sem nova chamada ao modelo, responder ao Cliente que não conseguiu concluir, listar as ações já executadas segundo o registro de auditoria e registrar o motivo no log e no trace (LAC-08) |
| `AGT-09` | WHEN o modelo solicita a mesma tool com os mesmos argumentos 2 vezes seguidas THEN a segunda SHALL NOT ser executada e o sistema SHALL devolver ao modelo o erro "chamada repetida" (LAC-21) |
| `AGT-10` | WHEN o modelo solicita a mesma tool com os mesmos argumentos pela 3ª vez seguida THEN o sistema SHALL encerrar o loop como em `AGT-08` (LAC-21) |

> Decisões aplicadas: LAC-08 → `AGT-08`; LAC-21 → `AGT-09`, `AGT-10`.

**Teste independente**: na tag `phase-07`, rodar `demo_07` e conferir a trajetória do cenário de `AGT-06` e as métricas.

### P2: Triagem com saída estruturada (Fase 8)

**História**: Como Operador, quero um endpoint de triagem que devolva sempre um JSON válido no schema combinado, para que o código consumidor nunca quebre com texto em volta do JSON.

**Por que P2**: Resolve a confiabilidade de integração. Não faz parte do fluxo de chat do MVP.

**Critérios de aceite**:

| ID | Critério |
|---|---|
| `TRI-01` | WHEN o endpoint de triagem recebe uma entrada válida THEN ele SHALL devolver um objeto com exatamente os campos `categoria`, `prioridade`, `sentimento`, `precisa_humano` (booleano) e `resumo` |
| `TRI-02` | WHEN a triagem chama o LLM THEN ela SHALL exigir saída no schema pelo recurso nativo de schema obrigatório do provedor e SHALL validar a resposta contra o schema |
| `TRI-03` | WHEN a resposta do LLM falha na validação THEN o sistema SHALL repetir a chamada incluindo o erro de validação no prompt, no máximo 2 vezes |
| `TRI-04` | WHEN `demo_08` executa 200 chamadas nos modos "só instrução no prompt" e "schema obrigatório" THEN ele SHALL registrar em `docs/results.md` o percentual de JSON válido de cada modo |
| `TRI-05` | WHEN o endpoint de triagem é chamado THEN a entrada SHALL ser um texto livre (mensagem ou ticket), e identificador de Conversa SHALL NOT ser aceito como entrada (LAC-10) |
| `TRI-06` | WHEN a triagem devolve um objeto THEN `categoria` SHALL ser um de {pedido, assinatura, reembolso, entrega, conta, outro}, `prioridade` SHALL ser um de {baixa, media, alta, urgente} e `sentimento` SHALL ser um de {positivo, neutro, negativo} (LAC-11) |
| `TRI-07` | WHEN a resposta continua inválida depois das 2 novas tentativas THEN o endpoint SHALL devolver erro tipado "triagem indisponível", sem objeto de triagem, e SHALL registrar a falha no log e no trace (LAC-12) |
| `TRI-08` | WHEN a entrada da triagem está em português ou em inglês THEN `categoria`, `prioridade` e `sentimento` SHALL usar os mesmos valores de `TRI-06`, e o `resumo` SHALL estar no idioma da entrada (LAC-01) |

> Decisões aplicadas: LAC-10 → `TRI-05`; LAC-11 → `TRI-06`; LAC-12 → `TRI-07`; LAC-01 → `TRI-08`.

**Teste independente**: na tag `phase-08`, chamar o endpoint de triagem com uma entrada de exemplo e rodar `demo_08`.

### P2: Streaming com passos visíveis e cancelamento (Fase 9)

**História**: Como Cliente, quero ver a resposta surgindo em tempo real e o que o Atlas está fazendo ("consultando pedido…"), e poder cancelar no meio, para não achar que travou.

**Por que P2**: Melhora a experiência percebida. O MVP funciona sem streaming.

**Critérios de aceite**:

| ID | Critério |
|---|---|
| `STR-01` | WHEN o Cliente envia uma mensagem pela UI THEN a resposta SHALL chegar via SSE como eventos tipados `token`, `tool_start`, `tool_end`, `citation`, `done` e `error` |
| `STR-02` | WHEN o servidor emite `tool_start` THEN a UI SHALL mostrar o passo atual em linguagem do usuário (ex.: "consultando pedido…") até o `tool_end` correspondente |
| `STR-03` | WHEN a resposta usa um trecho do corpus THEN o servidor SHALL emitir `citation` com a fonte e a UI SHALL exibi-la junto da resposta |
| `STR-04` | WHEN o Cliente aciona "cancelar" durante a resposta THEN o servidor SHALL interromper a geração em andamento e a UI SHALL parar de renderizar novos tokens (o que persiste: `STR-07`) |
| `STR-05` | WHEN uma resposta termina com sucesso THEN o servidor SHALL emitir `done` exatamente uma vez, como último evento |
| `STR-06` | WHEN `demo_09` é executado THEN ele SHALL medir o tempo até o primeiro token (TTFT) e o tempo total, e os valores SHALL constar no README e em `docs/results.md` |
| `STR-07` | WHEN o Cliente cancela uma resposta em andamento THEN as tools já executadas SHALL permanecer com seus registros de auditoria, e a resposta parcial SHALL ser salva em `messages` marcada como cancelada e incluída no histórico da Conversa (LAC-16) |
| `STR-08` | WHEN a UI do chat é exibida THEN todos os seus textos fixos SHALL estar disponíveis em português e em inglês, e o Cliente SHALL conseguir alternar entre os dois idiomas (LAC-01; idioma inicial: `STR-09`) |
| `STR-09` | WHEN a UI (incluindo a tela de login da demo) é aberta antes de o Cliente escolher um idioma THEN o idioma inicial SHALL ser o do navegador, se for português ou inglês, e inglês nos demais casos (LAC-32) |

> Decisões aplicadas: LAC-16 → `STR-07`; LAC-01 → `STR-08`. LAC-32 → `STR-09`.

**Teste independente**: na tag `phase-09`, abrir a UI, enviar o cenário de `AGT-06`, observar os passos e cancelar uma segunda resposta no meio.

### P3: Guardrails em camadas (Fase 10)

**História**: Como Operador, quero que entradas maliciosas, documentos envenenados e respostas indevidas sejam barrados em camadas, para que nem um PDF malicioso faça o Atlas vazar dados de outro cliente.

**Por que P3**: É endurecimento para produção. A autorização essencial já existe desde a fase 6 (`TOOL-03`, `TOOL-04`).

**Critérios de aceite**:

| ID | Critério |
|---|---|
| `GRD-01` | WHEN uma mensagem do Cliente chega THEN o guardrail de entrada SHALL verificá-la quanto a prompt injection e a dados pessoais antes de qualquer chamada ao LLM (ação ao detectar: `GRD-06`, `GRD-07`) |
| `GRD-02` | WHEN trechos recuperados são incluídos no prompt THEN eles SHALL estar delimitados e marcados como dados não confiáveis, separados das instruções |
| `GRD-03` | WHEN a ingestão encontra padrão de injection num documento THEN o documento SHALL ir para quarentena: nenhum trecho dele indexado e o documento listado como quarentenado no relatório de ingestão |
| `GRD-04` | WHEN a resposta final contém dado de outro cliente THEN o guardrail de saída SHALL bloqueá-la e substituí-la pela mensagem padrão de bloqueio (demais critérios de "fora da política": `GRD-08`) |
| `GRD-05` | WHEN `demo_10` executa a suíte red-team de 30 ataques (incluindo o PDF malicioso) e o conjunto de perguntas legítimas de controle THEN ele SHALL registrar em `docs/results.md` a taxa de bloqueio dos ataques e a taxa de falso positivo nas perguntas legítimas |
| `GRD-06` | WHEN o guardrail de entrada detecta prompt injection THEN o sistema SHALL bloquear a mensagem, responder com a mensagem padrão de bloqueio sem chamar o modelo e registrar a detecção no trace (LAC-13) |
| `GRD-07` | WHEN o guardrail de entrada detecta dado pessoal (número de cartão, documento de identificação, e-mail ou telefone) THEN o sistema SHALL mascarar o dado antes de enviá-lo ao LLM e antes de persisti-lo em `messages`, logs e traces (LAC-14) |
| `GRD-08` | WHEN a resposta final promete uma ação sem execução bem-sucedida da tool correspondente no registro de auditoria daquela requisição, ou trata de assunto fora do suporte da Assinaflow, THEN o guardrail de saída SHALL bloqueá-la e substituí-la pela mensagem padrão de bloqueio (LAC-15) |
| `GRD-09` | WHEN os guardrails de entrada e de saída avaliam conteúdo THEN eles SHALL cobrir português e inglês, e a suíte red-team de `GRD-05` SHALL conter ataques nos dois idiomas (LAC-01) |

> Decisões aplicadas: LAC-13 → `GRD-06`; LAC-14 → `GRD-07`; LAC-15 → `GRD-08`; LAC-28 → `GRD-93`; LAC-01 → `GRD-09`.

**Teste independente**: na tag `phase-10`, rodar `demo_10` e conferir o PDF malicioso em quarentena e as taxas registradas.

### P3: Prompt cache (Fase 11)

**História**: Como Operador, quero que o prompt seja montado do estável para o variável, com ponto de cache, para reduzir custo e latência por chamada.

**Por que P3**: Otimização de custo. Não muda o comportamento funcional.

**Critérios de aceite**:

| ID | Critério |
|---|---|
| `CACHE-01` | WHEN um prompt é montado THEN a ordem SHALL ser: system, definições de tools e políticas fixas (prefixo estável); depois trechos do RAG, histórico e pergunta; com o ponto de cache no fim do prefixo estável |
| `CACHE-02` | WHEN duas requisições de Conversas diferentes são montadas com a mesma configuração THEN o prefixo estável das duas SHALL ser idêntico byte a byte, sem nenhum dado dinâmico (ex.: data/hora) |
| `CACHE-03` | WHEN uma chamada ao LLM usa cache THEN o log da chamada SHALL registrar tokens de leitura e de escrita de cache e o custo calculado com o preço de cache |
| `CACHE-04` | WHEN `demo_11` executa 100 conversas antes e depois da reordenação THEN ele SHALL registrar em `docs/results.md` custo e latência por chamada e a taxa de cache hit |

**Teste independente**: na tag `phase-11`, rodar `demo_11` e comparar as duas linhas da métrica.

### P3: Retry, circuit breaker e fallback (Fase 12)

**História**: Como Cliente, quero continuar sendo atendido mesmo quando o provedor de LLM cai no pico, ou pelo menos receber um aviso claro e um ticket aberto.

**Por que P3**: É resiliência de produção. O MVP funciona com o provedor primário.

**Critérios de aceite**:

| ID | Critério |
|---|---|
| `RES-01` | WHEN uma chamada ao LLM excede o timeout configurado THEN o sistema SHALL abortá-la e tratá-la como erro recuperável |
| `RES-02` | WHEN uma chamada falha com erro recuperável (429, 5xx ou timeout) THEN o sistema SHALL repetir com backoff exponencial e jitter até o máximo de tentativas configurado |
| `RES-03` | WHEN uma chamada falha com erro não recuperável (ex.: 400, 401) THEN o sistema SHALL NOT repeti-la no mesmo provedor |
| `RES-04` | WHEN as falhas de um provedor atingem o limiar configurado THEN o circuit breaker desse provedor SHALL abrir e as chamadas SHALL ir direto ao próximo da cadeia até o período de reavaliação |
| `RES-05` | WHEN o provedor primário esgota as tentativas ou está com circuito aberto THEN o sistema SHALL atender pelo modelo secundário de outro provedor, com o mesmo contexto e as tools no formato desse provedor |
| `RES-06` | WHEN o provedor secundário também falha THEN o sistema SHALL devolver a resposta degradada ("tente em instantes") e abrir um Ticket com Conversa, motivo e `trace_id` |
| `RES-07` | WHEN `CHAOS_RATE` está definido com valor p entre 0 e 1 THEN o sistema SHALL injetar falha recuperável no provedor primário com probabilidade p; WHEN não está definido THEN SHALL injetar zero falhas |
| `RES-08` | WHEN `demo_12` executa com 30% de falhas injetadas THEN ele SHALL registrar em `docs/results.md` a taxa de sucesso final e a latência extra |

> Decisões aplicadas: LAC-29 → `RES-93`.

**Teste independente**: na tag `phase-12`, rodar `demo_12` com `CHAOS_RATE=0.3` e conferir as métricas. Com os dois provedores indisponíveis, conferir a resposta degradada e o Ticket.

### P3: Evals com gate no CI (Fase 13)

**História**: Como Operador, quero que todo PR que mexe em prompt ou API rode um dataset de evals e seja bloqueado se a qualidade cair, para que uma linha no prompt não quebre respostas sem ninguém ver.

**Por que P3**: O embrião (FND-05) já existe no P1. Aqui entra o dataset completo e o gate.

**Critérios de aceite**:

| ID | Critério |
|---|---|
| `EVAL-01` | WHEN o dataset de evals é carregado THEN ele SHALL conter ~100 casos em JSONL com categoria em {RAG (resposta + citação correta), trajetória de tools, JSON válido, red-team, tom}, e cada categoria SHALL ter ao menos um caso |
| `EVAL-02` | WHEN um caso de trajetória de tools ou de JSON válido é avaliado THEN o grader SHALL ser determinístico; WHEN um caso de fidelidade ao contexto é avaliado THEN o grader SHALL ser LLM-as-judge |
| `EVAL-03` | WHEN um PR altera arquivos em `prompts/` ou `api/` THEN o CI SHALL rodar os evals e comentar o relatório no PR |
| `EVAL-04` | WHEN um PR não altera arquivos em `prompts/` nem em `api/` THEN o CI SHALL NOT rodar os evals completos |
| `EVAL-05` | WHEN o score do PR cai além do limite definido em relação à base de comparação THEN o CI SHALL bloquear o merge (escala, base e limite: `EVAL-07`; sem base anterior: `EVAL-92`) |
| `EVAL-06` | WHEN a fase 13 é declarada pronta THEN `docs/results.md` SHALL documentar um caso real de regressão capturada pelo gate, com link para o PR |
| `EVAL-07` | WHEN o gate de evals avalia um PR THEN o score SHALL ir de 0 a 100, por categoria e geral, a base SHALL ser o último run da branch principal e o merge SHALL ser bloqueado só se o score geral cair mais de 2 pontos; quedas só por categoria SHALL aparecer no relatório sem bloquear (LAC-22) |
| `EVAL-08` | WHEN o dataset de evals é carregado THEN cada categoria SHALL ter casos em português e em inglês (LAC-01) |

> Decisões aplicadas: LAC-22 → `EVAL-07`, `EVAL-91`; LAC-01 → `EVAL-08`. LAC-33 → `EVAL-92`.

**Teste independente**: abrir um PR que piora deliberadamente um prompt e conferir o comentário do relatório e o merge bloqueado.

### P3: Observabilidade ponta a ponta (Fase 14)

**História**: Como Operador, quero abrir o trace de qualquer resposta e ver cada passo com custo e latência, para descobrir por que uma resposta saiu errada e transformar o caso num teste.

**Por que P3**: O embrião (FND-03, FND-04) existe no P1. Aqui entra o trace completo, o feedback e o dashboard.

**Critérios de aceite**:

| ID | Critério |
|---|---|
| `OBS-01` | WHEN uma requisição de chat é processada THEN o sistema SHALL gerar um trace com spans de: guardrail, retrieval (trechos e scores), cada chamada ao LLM (modelo, prompt, tokens de entrada/saída/cache, custo, latência), cada tool (argumentos e resultado) e fallback acionado, se houver |
| `OBS-02` | WHEN o Cliente dá feedback positivo ou negativo numa resposta THEN o feedback SHALL ser gravado e ligado ao trace daquela resposta |
| `OBS-03` | WHEN o Operador consulta o dashboard THEN ele SHALL mostrar custo por Conversa, latência p95 e taxa de fallback (onde fica: `OBS-07`) |
| `OBS-04` | ~~disponibilidade do `trace_id`~~ Aposentado — substituído por `OBS-06` (LAC-17) |
| `OBS-05` | WHEN `demo_14` é executado THEN ele SHALL mostrar o fluxo: reclamação → trace aberto → trecho errado identificado → correção → caso adicionado ao dataset de evals e passando |
| `OBS-06` | WHEN uma resposta do assistente é exibida na UI do chat THEN o `trace_id` dela SHALL aparecer junto da resposta, de forma discreta (LAC-17) |
| `OBS-07` | WHEN o dashboard de `OBS-03` é acessado THEN ele SHALL existir só na ferramenta de observabilidade, com acesso restrito ao Operador, e o app SHALL NOT ter tela de dashboard (LAC-17) |

> Decisões aplicadas: LAC-17 → `OBS-06`, `OBS-07`; LAC-25 → `OBS-93`.

**Teste independente**: na tag `phase-14`, enviar uma pergunta, dar feedback negativo, abrir o trace pelo `trace_id` e conferir todos os spans.

### P3: Demo pública protegida

**História**: Como recrutador, quero acessar uma demo pública com login de teste e conversar com o Atlas sem instalar nada. Como Operador, quero que rate limit e teto de gasto diário protejam minha conta do provedor.

**Por que P3**: É a vitrine final. Depende de quase todas as fases.

**Critérios de aceite**:

| ID | Critério |
|---|---|
| `DEMO-01` | WHEN um visitante não autenticado tenta usar o chat THEN o sistema SHALL exigir o login de teste e SHALL NOT chamar o LLM |
| `DEMO-02` | WHEN um Cliente excede o rate limit configurado THEN o sistema SHALL recusar a mensagem sem chamar o LLM e informar quando poderá tentar de novo (valores e janela: `DEMO-08`, `DEMO-09`) |
| `DEMO-03` | WHEN o gasto acumulado do dia atinge o teto configurado THEN o sistema SHALL NOT fazer novas chamadas ao LLM até o reinício da janela diária (comportamento visível: `DEMO-09`) |
| `DEMO-04` | WHEN o README é publicado THEN ele SHALL conter o link da demo pública e as instruções do login de teste |
| `DEMO-05` | WHEN a demo pública está no ar THEN `CHAOS_RATE` SHALL estar desligado (zero falhas injetadas) |
| `DEMO-06` | WHEN a demo pública está no ar THEN SHALL existir ao menos 2 personas de teste fixas e compartilhadas, cada uma com dados fictícios próprios, e as credenciais SHALL estar no README (LAC-02) |
| `DEMO-07` | WHEN a restauração diária da demo ocorre THEN o sistema SHALL restaurar `orders`, `subscriptions` e `refunds` de todas as personas ao estado inicial (seed) e SHALL apagar as Conversas delas (LAC-02) |
| `DEMO-08` | WHEN o rate limit ou o teto de gasto não está configurado THEN o sistema SHALL usar 20 mensagens por hora por conta e US$ 2 por dia no total, e os dois valores SHALL ser alteráveis por variável de ambiente (LAC-09) |
| `DEMO-09` | WHEN o rate limit ou o teto diário é atingido THEN o sistema SHALL responder com mensagem fixa, sem chamar o LLM, até a virada da janela; o teto diário SHALL reiniciar às 00:00 UTC (LAC-09) |
| `DEMO-10` | WHEN um registro de Conversa, trace ou auditoria da demo pública passa de 7 dias THEN o sistema SHALL expurgá-lo automaticamente (LAC-23) |
| `DEMO-11` | WHEN um visitante acessa a demo pública THEN a tela de login e as instruções das personas SHALL estar disponíveis em português e em inglês (LAC-01) |
| `DEMO-12` | WHEN a restauração diária da demo ocorre THEN ela SHALL apagar só o seed e as Conversas (`DEMO-07`), e os traces e os registros de auditoria SHALL NOT ser apagados por ela, só pelo expurgo de 7 dias (`DEMO-10`) (LAC-34) |

> Decisões aplicadas: LAC-02 → `DEMO-06`, `DEMO-07`, `DEMO-91`; LAC-09 → `DEMO-08`, `DEMO-09`; LAC-23 → `DEMO-10`; LAC-01 → `DEMO-11`; LAC-34 → `DEMO-12`.

**Teste independente**: acessar a URL pública, fazer login de teste, conversar, e forçar o rate limit com um script.

### P3: Busca híbrida (bônus da Fase 5)

**História**: Como Cliente, quero que perguntas com termos exatos (ex.: número de plano) e perguntas com sinônimos sejam bem atendidas pela mesma busca.

**Por que P3**: É o "bônus que impressiona" do documento-fonte. Entra por premissa (LAC-18), sem reranker.

**Critérios de aceite**:

| ID | Critério |
|---|---|
| `RAG-12` | WHEN a busca híbrida está habilitada THEN o sistema SHALL combinar os rankings full-text e vetorial por Reciprocal Rank Fusion e devolver o top-k fundido (premissa LAC-18) |
| `RAG-13` | WHEN `demo_05` é executado com a busca híbrida habilitada THEN ele SHALL acrescentar à tabela de `docs/results.md` o recall@5 da busca híbrida no mesmo conjunto de perguntas (premissa LAC-18) |

**Teste independente**: na tag `phase-05`, rodar `demo_05` com a busca híbrida ligada e conferir a linha adicional.

## 7. Estados e Transições

### Pedido (`orders`)

Estados: pendente, pago, enviado, entregue, cancelado (`TOOL-17`). As
transições pendente → pago → enviado → entregue existem só nos dados
fictícios (seed); nenhuma tool as dispara.

| De | Evento | Para | Quem pode disparar | Efeito colateral |
|---|---|---|---|---|
| pendente ou pago | `cancel_order` confirmada | cancelado | Cliente dono, via Atlas, após confirmação | Registro de auditoria |
| enviado ou entregue | `cancel_order` | (sem mudança) | — | Erro devolvido ao modelo (`TOOL-99`) |
| cancelado | `cancel_order` | (sem mudança) | — | Erro devolvido ao modelo (`TOOL-91`) |
| qualquer | qualquer tool por cliente que não é o dono | (sem mudança) | — | "não encontrado" (`TOOL-90`) |

Transição proibida: sair de `cancelado` para qualquer outro estado por tool.

### Reembolso (`refunds`)

| De | Evento | Para | Quem pode disparar | Efeito colateral |
|---|---|---|---|---|
| (inexistente) | `issue_refund` confirmada e elegível (`TOOL-16`) | registrado | Cliente dono, via Atlas | Registro de auditoria |
| registrado | novo `issue_refund` do mesmo Pedido | (sem mudança) | — | Erro ao modelo (`TOOL-93`) |

### Solicitação de tool destrutiva

| De | Evento | Para | Quem pode disparar | Efeito colateral |
|---|---|---|---|---|
| (inexistente) | modelo solicita tool destrutiva | aguardando confirmação | Modelo | UI mostra ação e alvo |
| aguardando confirmação | Cliente confirma | executada | Cliente | Tool executa e audita |
| aguardando confirmação | Cliente recusa | recusada | Cliente | Recusa volta ao modelo e o loop continua (`TOOL-13`, `TOOL-14`) |
| aguardando confirmação | Cliente envia nova mensagem | recusada | Cliente | Tratada como recusa (`TOOL-97`) |

Estados terminais: executada, recusada. Proibido: executar sem passar por
"aguardando confirmação".

### Documento do corpus na ingestão

| De | Evento | Para | Quem pode disparar | Efeito colateral |
|---|---|---|---|---|
| pendente | ingestão OK | indexado | Operador (ingestão) | Trechos e embeddings criados |
| pendente | padrão de injection detectado | quarentena | Operador (ingestão) | Nada indexado; listado no relatório |
| pendente | leitura ou embedding falha | falho | Operador (ingestão) | Listado no relatório; reprocessável |
| indexado | reingestão | indexado | Operador | Trechos substituídos, sem duplicar |

Saída de quarentena: fora de escopo (seção 3).

### Circuit breaker por provedor

| De | Evento | Para | Quem pode disparar | Efeito colateral |
|---|---|---|---|---|
| fechado | falhas atingem o limiar | aberto | Sistema | Chamadas vão ao próximo da cadeia |
| aberto | período de reavaliação expira | meio-aberto | Sistema | Uma chamada de teste ao provedor |
| meio-aberto | chamada de teste OK | fechado | Sistema | Provedor volta a ser usado |
| meio-aberto | chamada de teste falha | aberto | Sistema | Reinicia o período |

## 8. Casos de Borda e Erros

| ID | Situação | Comportamento esperado |
|---|---|---|
| `FND-90` | WHEN a chave de API do provedor não está configurada | THEN o sistema SHALL reportar erro explícito com o nome da variável ausente e SHALL NOT registrar valor de segredo algum em log |
| `CHAT-90` | WHEN a mensagem é vazia ou só tem espaços | THEN o sistema SHALL rejeitá-la com erro de validação, sem chamar o LLM |
| `CHAT-91` | WHEN a mensagem excede o tamanho máximo configurado (`CHAT-09`) | THEN o sistema SHALL rejeitá-la com erro de validação, sem chamar o LLM |
| `CHAT-92` | WHEN o provedor do LLM retorna erro ou não responde (antes da fase 12) | THEN o sistema SHALL devolver erro tipado ao cliente, sem stack trace nem segredos, e SHALL NOT persistir resposta vazia como mensagem do assistente |
| `CHAT-93` | WHEN duas mensagens chegam ao mesmo tempo para a mesma Conversa | THEN o sistema SHALL persistir as duas, sem perda, na ordem de chegada |
| `CHAT-94` | WHEN o identificador de Conversa informado não existe ou é de outro cliente | THEN o sistema SHALL negar o acesso com a mesma resposta nos dois casos, sem revelar se a Conversa existe |
| `CHAT-95` | WHEN a mensagem do Cliente não está em português nem em inglês | THEN o Atlas SHALL responder em inglês avisando que atende só em português e em inglês, SHALL NOT responder ao conteúdo da mensagem e SHALL NOT acionar tools (LAC-31) |
| `RAG-90` | WHEN o índice está vazio (nenhum documento indexado) | THEN o Atlas SHALL responder que não sabe, sem erro |
| `RAG-91` | WHEN um documento do corpus não pode ser lido (corrompido ou formato não suportado) | THEN a ingestão SHALL marcá-lo como falho no relatório e SHALL continuar os demais |
| `RAG-92` | WHEN um documento já indexado é ingerido de novo | THEN a ingestão SHALL substituir os trechos dele, sem duplicar |
| `RAG-93` | WHEN o serviço de embeddings falha durante a ingestão | THEN os documentos afetados SHALL ficar como falhos e reprocessáveis, e SHALL NOT existir trecho sem embedding no índice vetorial |
| `RAG-94` | WHEN o serviço de embeddings ou a busca falha durante uma pergunta | THEN o Atlas SHALL NOT responder como se tivesse consultado a base; SHALL informar que não consegue consultar a base no momento |
| `TOOL-90` | WHEN uma tool recebe identificador de Pedido inexistente ou de outro cliente | THEN ela SHALL devolver ao modelo o mesmo resultado "não encontrado" nos dois casos |
| `TOOL-91` | WHEN `cancel_order` é chamada para um Pedido já cancelado ou em estado não cancelável | THEN ela SHALL devolver erro ao modelo e SHALL NOT alterar o Pedido |
| `TOOL-92` | WHEN duas execuções de `cancel_order` do mesmo Pedido ocorrem ao mesmo tempo | THEN exatamente uma SHALL ter efeito e a outra SHALL receber o erro de `TOOL-91` |
| `TOOL-93` | WHEN `issue_refund` é chamada de novo, ou em paralelo, para um Pedido já reembolsado | THEN o sistema SHALL manter no máximo um Reembolso por Pedido e devolver erro ao modelo |
| `TOOL-94` | WHEN o modelo chama uma tool com argumentos que não passam no schema | THEN o sistema SHALL devolver o erro de validação ao modelo e SHALL NOT executar a tool |
| `TOOL-95` | WHEN uma requisição de chat chega sem sessão autenticada válida | THEN o sistema SHALL rejeitá-la sem chamar o LLM nem tool alguma |
| `TOOL-96` | WHEN uma tool destrutiva fica aguardando confirmação e a confirmação nunca chega | THEN a tool SHALL NOT ser executada |
| `TOOL-97` | WHEN uma tool destrutiva aguarda confirmação e o Cliente envia uma nova mensagem em vez de confirmar | THEN o sistema SHALL tratar como recusa (`TOOL-13`) e SHALL NOT executar a tool (LAC-04) |
| `TOOL-98` | WHEN o reembolso é avaliado para um Pedido entregue há exatamente 7 dias e para um entregue há mais de 7 dias | THEN o primeiro SHALL ser elegível e o segundo SHALL NOT ser elegível (LAC-06) |
| `TOOL-99` | WHEN `cancel_order` é chamada para um Pedido enviado ou entregue | THEN ela SHALL devolver erro ao modelo e SHALL NOT alterar o Pedido (LAC-07) |
| `AGT-90` | WHEN o modelo responde sem solicitar tool | THEN o loop SHALL terminar com 1 passo e devolver a resposta |
| `AGT-91` | WHEN o modelo solicita uma tool que não existe | THEN o sistema SHALL devolver erro ao modelo como resultado, sem executar nada |
| `TRI-90` | WHEN a entrada da triagem é vazia | THEN o endpoint SHALL rejeitá-la com erro de validação, sem chamar o LLM |
| `STR-90` | WHEN ocorre erro durante o streaming | THEN o servidor SHALL emitir `error` e encerrar o stream, SHALL NOT emitir `done` depois, e a UI SHALL mostrar a mensagem de erro |
| `STR-91` | WHEN o cliente se desconecta no meio do streaming | THEN o servidor SHALL interromper a geração, como no cancelamento (`STR-04`) |
| `STR-92` | WHEN o Cliente aciona "cancelar" depois de `done` | THEN nada SHALL mudar na resposta já concluída |
| `GRD-90` | WHEN uma pergunta legítima do conjunto de controle contém termos parecidos com ataque (ex.: "ignore a cobrança duplicada") | THEN ela SHALL ser contabilizada; se bloqueada, entra na taxa de falso positivo de `GRD-05` |
| `GRD-91` | WHEN o PDF malicioso escapa da quarentena (verificação de ingestão desligada no teste) e o modelo obedece à instrução dele | THEN as tools SHALL continuar negando acesso a dados de outros clientes (`TOOL-04`) |
| `GRD-92` | ~~verificador de guardrail indisponível~~ | Aposentado — substituído por `GRD-93` (LAC-28) |
| `GRD-93` | WHEN o verificador de guardrail (entrada ou saída) falha ou fica indisponível | THEN o sistema SHALL bloquear a mensagem ou a resposta com a mensagem padrão (fail-closed) e registrar a falha no trace (LAC-28) |
| `CACHE-90` | WHEN a chamada vai para um provedor sem suporte a cache | THEN a chamada SHALL funcionar normalmente e registrar tokens de cache iguais a zero |
| `RES-90` | ~~primário falha após início do streaming~~ | Aposentado — substituído por `RES-93` (LAC-29) |
| `RES-91` | WHEN um retry ou fallback acontece no meio do loop depois de uma tool com efeito já executada | THEN essa tool SHALL NOT ser executada de novo |
| `RES-92` | WHEN os circuitos de todos os provedores estão abertos | THEN o sistema SHALL devolver a resposta degradada e abrir o Ticket sem chamar nenhum provedor |
| `RES-93` | WHEN o provedor primário falha depois que o primeiro token já foi enviado ao Cliente | THEN o sistema SHALL emitir `error` e encerrar o stream sem fallback, o Cliente SHALL poder reenviar a mensagem e as tools já executadas SHALL NOT ser repetidas (`RES-91`); o fallback de `RES-05` SHALL ocorrer só antes do primeiro token (LAC-29) |
| `EVAL-90` | WHEN a execução dos evals no CI falha por erro do provedor ou de infraestrutura | THEN o job SHALL falhar com status de erro de execução, diferente de "regressão de score" |
| `EVAL-91` | WHEN o score geral do PR cai exatamente 2 pontos em relação à base | THEN o CI SHALL NOT bloquear o merge (LAC-22) |
| `EVAL-92` | WHEN o gate de evals roda e não existe run anterior na branch principal | THEN o CI SHALL NOT bloquear o merge, e o score desse run SHALL virar a base de comparação (LAC-33) |
| `OBS-90` | ~~observabilidade indisponível~~ | Aposentado — substituído por `OBS-93` (LAC-25) |
| `OBS-91` | WHEN uma mensagem contém dado pessoal detectado pelo guardrail | THEN o trace SHALL conter o valor mascarado conforme `GRD-07`, nunca o original |
| `OBS-92` | WHEN um Cliente tenta dar feedback numa resposta de outro cliente | THEN o sistema SHALL recusar sem gravar |
| `OBS-93` | WHEN a ferramenta de observabilidade está indisponível | THEN o atendimento SHALL seguir normalmente, a telemetria daquela requisição SHALL ser descartada com aviso no log local, e o log local SHALL manter `trace_id`, tokens e custo (LAC-25) |
| `DEMO-90` | ~~visitantes simultâneos~~ | Aposentado — substituído por `DEMO-91` (LAC-02) |
| `DEMO-91` | WHEN dois visitantes usam a mesma persona ao mesmo tempo | THEN os dois SHALL operar sobre os mesmos dados fictícios da persona, com as garantias de concorrência de `TOOL-92` e `TOOL-93`, até a restauração diária (`DEMO-07`) (LAC-02) |

## 9. Mensagens ao Usuário

Todos os textos ao Cliente existem em português e em inglês e seguem o
idioma da conversa (`CHAT-10`) ou da UI (`STR-08`). A tabela define o
conteúdo, não a redação final.

| Situação | Mensagem | Tom / canal |
|---|---|---|
| Fora do corpus (`RAG-04`) | Diz que não encontrou a informação na base da Assinaflow e não especula | Neutro; resposta do chat |
| Base indisponível (`RAG-94`) | Diz que não consegue consultar a base agora e pede para tentar de novo | Neutro; resposta do chat |
| Confirmação de tool destrutiva (`TOOL-05`) | "Confirmar: <ação> <alvo>?" com botões confirmar/recusar | Objetivo; componente de confirmação na UI |
| Passo do agente (`STR-02`) | Rótulo por tool (ex.: "consultando pedido…", "verificando política de reembolso…") | Curto; indicador de progresso |
| Resposta degradada (`RES-06`) | "Tente em instantes" + aviso de que um ticket foi aberto | Empático; resposta do chat |
| Bloqueio por guardrail (`GRD-04`, `GRD-06`, `GRD-08`, `GRD-93`) | Diz que não pode ajudar com aquele pedido, sem explicar a regra de detecção | Neutro; resposta do chat |
| Idioma não suportado (`CHAT-95`) | Em inglês: avisa que o atendimento é só em português e em inglês | Neutro; resposta do chat |
| Limite do agente (`AGT-08`) | Diz que não conseguiu concluir e lista as ações já executadas | Neutro; resposta do chat |
| Rate limit (`DEMO-02`) | Diz que o limite foi atingido e quando tentar de novo | Neutro; aviso no chat |
| Teto diário (`DEMO-03`) | Diz que a demo atingiu o limite diário e quando volta | Neutro; aviso no chat |
| Erro no streaming (`STR-90`) | Diz que houve um erro e oferece tentar de novo | Neutro; inline na mensagem |
| Validação de mensagem (`CHAT-90`, `CHAT-91`) | Diz que a mensagem está vazia ou longa demais | Inline no campo de entrada |

## 10. Requisitos Não-Funcionais

| Eixo | Requisito |
|---|---|
| Performance | O documento-fonte não fixa meta absoluta de latência. A exigência é **medir e publicar**: TTFT e tempo total (`STR-06`), p50/p95 da busca (`RAG-11`), latência extra do fallback (`RES-08`), latência por chamada com cache (`CACHE-04`). |
| Volume | Corpus de ~40 documentos; benchmark com 10k, 100k e 1M vetores sintéticos; conversa de 50 turnos; 30 perguntas de alucinação; 15 tarefas multi-step; 200 chamadas de triagem; 100 conversas de cache; ~100 casos de eval; 30 ataques red-team. |
| Concorrência | Duas mensagens na mesma Conversa não se perdem (`CHAT-93`); `cancel_order` e `issue_refund` têm efeito único sob concorrência (`TOOL-92`, `TOOL-93`); visitantes simultâneos na mesma persona compartilham os dados dela (`DEMO-91`). |
| Segurança | Identidade sempre da sessão no servidor (`TOOL-03`); autorização por cliente em toda tool (`TOOL-04`); segredos só por variável de ambiente e nunca em log (`FND-90`); trechos marcados como não confiáveis (`GRD-02`); confirmação para tools destrutivas (`TOOL-05`); acesso à observabilidade restrito ao Operador. |
| Privacidade / LGPD | Dados de clientes e corpus são fictícios. Visitantes da demo podem digitar dados pessoais reais: são mascarados na entrada (`GRD-07`); as Conversas são apagadas na restauração diária (`DEMO-07`), e Conversas, traces e auditoria são expurgados após 7 dias (`DEMO-10`). |
| Acessibilidade | Não se aplica como requisito formal (sem nível WCAG exigido no documento-fonte; ver seção 3). |
| i18n / formato | Produto bilíngue português/inglês: corpus (`RAG-14`), busca full-text (`RAG-15`), respostas (`CHAT-10`), UI (`STR-08`, `DEMO-11`), triagem (`TRI-08`), guardrails (`GRD-09`) e evals (`EVAL-08`, `RAG-19`). Artefatos públicos em inglês (`DOD-04`). Moeda única USD, formatada conforme o idioma da conversa (`TOOL-19`). Outros idiomas: `CHAT-95`. Idioma inicial da UI: `STR-09`. Janela diária dos limites da demo em UTC (`DEMO-09`). |
| Observabilidade | Log estruturado com `trace_id`, tokens e custo desde a Fase 0 (`FND-03`, `FND-04`); trace completo por requisição (`OBS-01`); auditoria de toda tool com efeito (`TOOL-06`–`TOOL-08`); dashboard de custo por Conversa, p95 e taxa de fallback (`OBS-03`). |
| Compatibilidade | Greenfield: não há sistema existente a preservar. Cada tag `phase-NN` SHALL continuar reproduzível depois de criada (`DOD-02`). |

## 11. Rastreabilidade

| ID | História | Prioridade | Status |
|---|---|---|---|
| `FND-01` | P1: Fundação mensurável | P1 | Pendente |
| `FND-02` | P1: Fundação mensurável | P1 | Pendente |
| `FND-03` | P1: Fundação mensurável | P1 | Pendente |
| `FND-04` | P1: Fundação mensurável | P1 | Pendente |
| `FND-05` | P1: Fundação mensurável | P1 | Pendente |
| `FND-90` | P1: Fundação mensurável | P1 | Pendente |
| `CHAT-01` | P1: Conversa e memória | P1 | Pendente |
| `CHAT-02` | P1: Conversa e memória | P1 | Pendente |
| `CHAT-03` | P1: Conversa e memória | P1 | Pendente |
| `CHAT-04` | P1: Conversa e memória | P1 | Pendente |
| `CHAT-05` | P1: Conversa e memória | P1 | Pendente |
| `CHAT-06` | P1: Conversa e memória | P1 | Pendente |
| `CHAT-07` | P1: Conversa e memória | P1 | Pendente |
| `CHAT-08` | P1: Conversa e memória | P1 | Pendente |
| `CHAT-09` | P1: Conversa e memória | P1 | Pendente |
| `CHAT-10` | P1: Conversa e memória | P1 | Pendente |
| `CHAT-90` | P1: Conversa e memória | P1 | Pendente |
| `CHAT-91` | P1: Conversa e memória | P1 | Pendente |
| `CHAT-92` | P1: Conversa e memória | P1 | Pendente |
| `CHAT-93` | P1: Conversa e memória | P1 | Pendente |
| `CHAT-94` | P1: Conversa e memória | P1 | Pendente |
| `CHAT-95` | P1: Conversa e memória | P1 | Pendente |
| `RAG-01` | P1: Respostas fundamentadas | P1 | Pendente |
| `RAG-02` | P1: Respostas fundamentadas | P1 | Pendente |
| `RAG-03` | P1: Respostas fundamentadas | P1 | Pendente |
| `RAG-04` | P1: Respostas fundamentadas | P1 | Pendente |
| `RAG-05` | P1: Respostas fundamentadas | P1 | Pendente |
| `RAG-06` | P1: Respostas fundamentadas | P1 | Pendente |
| `RAG-07` | P1: Respostas fundamentadas | P1 | Pendente |
| `RAG-08` | P1: Respostas fundamentadas | P1 | Pendente |
| `RAG-09` | P1: Respostas fundamentadas | P1 | Pendente |
| `RAG-10` | P1: Respostas fundamentadas | P1 | Pendente |
| `RAG-11` | P1: Respostas fundamentadas | P1 | Pendente |
| `RAG-14` | P1: Respostas fundamentadas | P1 | Pendente |
| `RAG-15` | P1: Respostas fundamentadas | P1 | Pendente |
| `RAG-16` | P1: Respostas fundamentadas | P1 | Pendente |
| `RAG-17` | P1: Respostas fundamentadas | P1 | Pendente |
| `RAG-18` | P1: Respostas fundamentadas | P1 | Pendente |
| `RAG-19` | P1: Respostas fundamentadas | P1 | Pendente |
| `RAG-90` | P1: Respostas fundamentadas | P1 | Pendente |
| `RAG-91` | P1: Respostas fundamentadas | P1 | Pendente |
| `RAG-92` | P1: Respostas fundamentadas | P1 | Pendente |
| `RAG-93` | P1: Respostas fundamentadas | P1 | Pendente |
| `RAG-94` | P1: Respostas fundamentadas | P1 | Pendente |
| `DOD-01` | P1: Definição de pronto e vitrine | P1 | Pendente |
| `DOD-02` | P1: Definição de pronto e vitrine | P1 | Pendente |
| `DOD-03` | P1: Definição de pronto e vitrine | P1 | Pendente |
| `DOD-04` | P1: Definição de pronto e vitrine | P1 | Pendente |
| `TOOL-01` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-02` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-03` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-04` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-05` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-06` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-07` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-08` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-09` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-10` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-11` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-12` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-13` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-14` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-15` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-16` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-17` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-18` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-19` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-90` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-91` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-92` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-93` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-94` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-95` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-96` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-97` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-98` | P2: Ações sobre pedidos | P2 | Pendente |
| `TOOL-99` | P2: Ações sobre pedidos | P2 | Pendente |
| `AGT-01` | P2: Agente multi-step | P2 | Pendente |
| `AGT-02` | P2: Agente multi-step | P2 | Pendente |
| `AGT-03` | P2: Agente multi-step | P2 | Pendente |
| `AGT-04` | P2: Agente multi-step | P2 | Pendente |
| `AGT-05` | P2: Agente multi-step | P2 | Pendente |
| `AGT-06` | P2: Agente multi-step | P2 | Pendente |
| `AGT-07` | P2: Agente multi-step | P2 | Pendente |
| `AGT-08` | P2: Agente multi-step | P2 | Pendente |
| `AGT-09` | P2: Agente multi-step | P2 | Pendente |
| `AGT-10` | P2: Agente multi-step | P2 | Pendente |
| `AGT-90` | P2: Agente multi-step | P2 | Pendente |
| `AGT-91` | P2: Agente multi-step | P2 | Pendente |
| `TRI-01` | P2: Triagem estruturada | P2 | Pendente |
| `TRI-02` | P2: Triagem estruturada | P2 | Pendente |
| `TRI-03` | P2: Triagem estruturada | P2 | Pendente |
| `TRI-04` | P2: Triagem estruturada | P2 | Pendente |
| `TRI-05` | P2: Triagem estruturada | P2 | Pendente |
| `TRI-06` | P2: Triagem estruturada | P2 | Pendente |
| `TRI-07` | P2: Triagem estruturada | P2 | Pendente |
| `TRI-08` | P2: Triagem estruturada | P2 | Pendente |
| `TRI-90` | P2: Triagem estruturada | P2 | Pendente |
| `STR-01` | P2: Streaming | P2 | Pendente |
| `STR-02` | P2: Streaming | P2 | Pendente |
| `STR-03` | P2: Streaming | P2 | Pendente |
| `STR-04` | P2: Streaming | P2 | Pendente |
| `STR-05` | P2: Streaming | P2 | Pendente |
| `STR-06` | P2: Streaming | P2 | Pendente |
| `STR-07` | P2: Streaming | P2 | Pendente |
| `STR-08` | P2: Streaming | P2 | Pendente |
| `STR-09` | P2: Streaming | P2 | Pendente |
| `STR-90` | P2: Streaming | P2 | Pendente |
| `STR-91` | P2: Streaming | P2 | Pendente |
| `STR-92` | P2: Streaming | P2 | Pendente |
| `GRD-01` | P3: Guardrails | P3 | Pendente |
| `GRD-02` | P3: Guardrails | P3 | Pendente |
| `GRD-03` | P3: Guardrails | P3 | Pendente |
| `GRD-04` | P3: Guardrails | P3 | Pendente |
| `GRD-05` | P3: Guardrails | P3 | Pendente |
| `GRD-06` | P3: Guardrails | P3 | Pendente |
| `GRD-07` | P3: Guardrails | P3 | Pendente |
| `GRD-08` | P3: Guardrails | P3 | Pendente |
| `GRD-09` | P3: Guardrails | P3 | Pendente |
| `GRD-90` | P3: Guardrails | P3 | Pendente |
| `GRD-91` | P3: Guardrails | P3 | Pendente |
| `GRD-92` | P3: Guardrails | P3 | Aposentado (→ `GRD-93`) |
| `GRD-93` | P3: Guardrails | P3 | Pendente |
| `CACHE-01` | P3: Prompt cache | P3 | Pendente |
| `CACHE-02` | P3: Prompt cache | P3 | Pendente |
| `CACHE-03` | P3: Prompt cache | P3 | Pendente |
| `CACHE-04` | P3: Prompt cache | P3 | Pendente |
| `CACHE-90` | P3: Prompt cache | P3 | Pendente |
| `RES-01` | P3: Retry e fallback | P3 | Pendente |
| `RES-02` | P3: Retry e fallback | P3 | Pendente |
| `RES-03` | P3: Retry e fallback | P3 | Pendente |
| `RES-04` | P3: Retry e fallback | P3 | Pendente |
| `RES-05` | P3: Retry e fallback | P3 | Pendente |
| `RES-06` | P3: Retry e fallback | P3 | Pendente |
| `RES-07` | P3: Retry e fallback | P3 | Pendente |
| `RES-08` | P3: Retry e fallback | P3 | Pendente |
| `RES-90` | P3: Retry e fallback | P3 | Aposentado (→ `RES-93`) |
| `RES-91` | P3: Retry e fallback | P3 | Pendente |
| `RES-92` | P3: Retry e fallback | P3 | Pendente |
| `RES-93` | P3: Retry e fallback | P3 | Pendente |
| `EVAL-01` | P3: Evals com gate | P3 | Pendente |
| `EVAL-02` | P3: Evals com gate | P3 | Pendente |
| `EVAL-03` | P3: Evals com gate | P3 | Pendente |
| `EVAL-04` | P3: Evals com gate | P3 | Pendente |
| `EVAL-05` | P3: Evals com gate | P3 | Pendente |
| `EVAL-06` | P3: Evals com gate | P3 | Pendente |
| `EVAL-07` | P3: Evals com gate | P3 | Pendente |
| `EVAL-08` | P3: Evals com gate | P3 | Pendente |
| `EVAL-90` | P3: Evals com gate | P3 | Pendente |
| `EVAL-91` | P3: Evals com gate | P3 | Pendente |
| `EVAL-92` | P3: Evals com gate | P3 | Pendente |
| `OBS-01` | P3: Observabilidade | P3 | Pendente |
| `OBS-02` | P3: Observabilidade | P3 | Pendente |
| `OBS-03` | P3: Observabilidade | P3 | Pendente |
| `OBS-04` | P3: Observabilidade | P3 | Aposentado (→ `OBS-06`) |
| `OBS-05` | P3: Observabilidade | P3 | Pendente |
| `OBS-06` | P3: Observabilidade | P3 | Pendente |
| `OBS-07` | P3: Observabilidade | P3 | Pendente |
| `OBS-90` | P3: Observabilidade | P3 | Aposentado (→ `OBS-93`) |
| `OBS-91` | P3: Observabilidade | P3 | Pendente |
| `OBS-92` | P3: Observabilidade | P3 | Pendente |
| `OBS-93` | P3: Observabilidade | P3 | Pendente |
| `DEMO-01` | P3: Demo pública | P3 | Pendente |
| `DEMO-02` | P3: Demo pública | P3 | Pendente |
| `DEMO-03` | P3: Demo pública | P3 | Pendente |
| `DEMO-04` | P3: Demo pública | P3 | Pendente |
| `DEMO-05` | P3: Demo pública | P3 | Pendente |
| `DEMO-06` | P3: Demo pública | P3 | Pendente |
| `DEMO-07` | P3: Demo pública | P3 | Pendente |
| `DEMO-08` | P3: Demo pública | P3 | Pendente |
| `DEMO-09` | P3: Demo pública | P3 | Pendente |
| `DEMO-10` | P3: Demo pública | P3 | Pendente |
| `DEMO-11` | P3: Demo pública | P3 | Pendente |
| `DEMO-12` | P3: Demo pública | P3 | Pendente |
| `DEMO-90` | P3: Demo pública | P3 | Aposentado (→ `DEMO-91`) |
| `DEMO-91` | P3: Demo pública | P3 | Pendente |
| `RAG-12` | P3: Busca híbrida | P3 | Pendente |
| `RAG-13` | P3: Busca híbrida | P3 | Pendente |

**Cobertura**: 173 requisitos ativos (P1: 48 | P2: 62 | P3: 63). Outros 5 foram aposentados e substituídos ao aplicar as decisões (`GRD-92`, `RES-90`, `OBS-04`, `OBS-90`, `DEMO-90`).

## 12. Lacunas

Severidade classificada pelos critérios de `lacunas.md`, consultados no Jev
(um `noul` por critério). Legenda: mc = muda_construcao, as = area_sensivel,
pe = permissao, ec = erro_critico, sc = sem_criterio.

### Lacunas bloqueantes resolvidas (decisões do humano em `decisions.md`)

| Lacuna | Escolha | Critérios gerados |
|---|---|---|
| LAC-01 — idioma do produto | C (bilíngue PT/EN; contra a recomendação A) | `CHAT-10`, `RAG-14`, `RAG-15`, `RAG-16`, `RAG-17`, `RAG-19`, `TRI-08`, `STR-08`, `GRD-09`, `EVAL-08`, `DEMO-11` |
| LAC-02 — login de teste e isolamento | C (personas fixas, restauração diária) | `DEMO-06`, `DEMO-07`, `DEMO-91` (aposenta `DEMO-90`) |
| LAC-03 — tools com confirmação | A | `TOOL-12` |
| LAC-04 — recusa da confirmação | A | `TOOL-13`, `TOOL-14`, `TOOL-97` |
| LAC-05 — política validada no servidor | A | `TOOL-15` |
| LAC-06 — regra de elegibilidade | A | `TOOL-16`, `TOOL-98` |
| LAC-07 — estados do Pedido | A | `TOOL-17`, `TOOL-99` |
| LAC-08 — limite de passos/custo | A | `AGT-08` |
| LAC-09 — rate limit e teto | C | `DEMO-08`, `DEMO-09` |
| LAC-10 — entrada da triagem | A | `TRI-05` |
| LAC-11 — enums da triagem | A | `TRI-06` |
| LAC-12 — triagem sem JSON válido | A | `TRI-07` |
| LAC-13 — injection na entrada | A | `GRD-06` |
| LAC-14 — dado pessoal na entrada | A | `GRD-07` (e `OBS-91` ajustado) |
| LAC-15 — fora da política na saída | A | `GRD-08` |
| LAC-16 — cancelamento do stream | A | `STR-07` |
| LAC-17 — `trace_id` e dashboard | A | `OBS-06`, `OBS-07` (aposenta `OBS-04`) |
| LAC-20 — orçamento de contexto | A | `CHAT-08` |
| LAC-21 — chamada repetida | A | `AGT-09`, `AGT-10` |
| LAC-22 — gate de evals | A | `EVAL-07`, `EVAL-91` |
| LAC-23 — retenção na demo | A | `DEMO-10` |
| LAC-24 — método da alucinação | A | `RAG-18` |
| LAC-25 — observabilidade indisponível | A | `OBS-93` (aposenta `OBS-90`) |
| LAC-26 — tamanho máximo da mensagem | C | `CHAT-09` |
| LAC-27 — destinatário de `send_email` | A | `TOOL-18` |
| LAC-28 — falha do verificador de guardrail | A | `GRD-93` (aposenta `GRD-92`) |
| LAC-29 — falha do primário após o 1º token | C | `RES-93` (aposenta `RES-90`) |
| LAC-30 — moeda dos valores fictícios | A (USD única) | `TOOL-19` |
| LAC-31 — mensagem fora de PT/EN | A | `CHAT-95` |
| LAC-32 — idioma inicial da UI | A | `STR-09` |
| LAC-33 — gate sem base anterior | A | `EVAL-92` |
| LAC-34 — retenção: restauração diária vs 7 dias | A (interpretação da revisão 2 mantida) | `DEMO-12` (confirma `DEMO-07` e `DEMO-10`) |

### Premissas assumidas (não bloqueantes)

[LACUNA:LAC-18|NAO_BLOQUEANTE]
Pergunta: O bônus da fase 5 (busca híbrida com RRF e reranker) entra no escopo?
Opções:
  A) Os dois entram.
  B) Só a busca híbrida entra (P3); o reranker fica fora.
  C) Nenhum entra.
Recomendação: B — RRF é barato sobre as buscas que já existem. O reranker exige outro modelo ou API e compete com o cronograma.
Impacto se errado: escopo e cronograma da fase 5.
Premissa assumida: B. Ver `RAG-12`, `RAG-13` e seção 3. Reversibilidade alta.
Jev: todos os critérios ≤ 0.35 exceto sc=0.40 (só sem_criterio em dúvida → premissa)

[LACUNA:LAC-19|NAO_BLOQUEANTE]
Pergunta: A comparação de custo e qualidade entre provedores de embeddings (API vs modelo local) é entregável obrigatório?
Opções:
  A) Sim, com métrica em `docs/results.md`.
  B) Não. Usa-se um provedor, escolhido no `plan.md`, e a comparação vira conteúdo opcional.
Recomendação: B — o documento-fonte a cita como "vira conteúdo", não como prova de fase.
Impacto se errado: escopo de uma fase.
Premissa assumida: B. Ver seção 3. Reversibilidade alta.
Jev: todos os critérios ≤ 0.35 exceto sc=0.46 (só sem_criterio em dúvida → premissa)

## 13. Critérios de Sucesso da Feature

- [ ] As tags `phase-01` a `phase-14` existem e cada `demo_NN` roda com sucesso a partir de um checkout limpo da sua tag.
- [ ] `docs/results.md` tem 14 linhas de métrica de antes/depois, uma por fase.
- [ ] Código fechado até 31 jan 2027. Demo pública no ar até meados de fevereiro de 2027.
- [ ] `docker compose up` num clone limpo deixa o Atlas respondendo em um comando.
- [ ] A suíte red-team de 30 ataques não consegue acessar dados de outro cliente em nenhum caso.
- [ ] Pelo menos um PR real teve o merge bloqueado pelo gate de evals por regressão.
- [ ] O cenário "cancela meu último pedido, estorna e me manda o comprovante" completa a trajetória de 6 tools na demo pública.
- [ ] O Atlas responde perguntas do corpus e executa o cenário multi-step tanto em português quanto em inglês, com as métricas de `docs/results.md` registradas por idioma onde o spec exige (`RAG-19`).

## Autoverificação do spec

- [x] Todo critério de aceite tem ID único e formato WHEN/THEN/SHALL — 173 IDs ativos e 5 aposentados, sem duplicata (conferido por script), todos os ativos com WHEN/THEN/SHALL.
- [x] Toda história P1 é demonstrável isoladamente — cada uma tem "Teste independente" executável na sua tag ou num clone limpo.
- [x] Seção "Fora de Escopo" tem pelo menos um item — 14 itens.
- [x] Glossário usa termos que existem no codebase (verificado com Grep) — repositório greenfield, sem código (conferido). Todos os termos vêm do documento-fonte, marcados `(novo)` e com a seção de origem citada.
- [x] Casos de borda cobrem vazio (`CHAT-90`, `RAG-90`, `TRI-90`), limite (`CHAT-91`, `AGT-02`, `DEMO-02`, `TOOL-98`, `EVAL-91`), inválido (`TOOL-94`, `AGT-91`), concorrência (`CHAT-93`, `TOOL-92`, `TOOL-93`, `DEMO-91`), falha de dependência (`CHAT-92`, `RAG-93`, `RAG-94`, `RES-92`, `RES-93`, `OBS-93`, `GRD-93`) e permissão (`CHAT-94`, `TOOL-90`, `TOOL-95`, `OBS-92`).
- [x] Toda seção de NFR está preenchida ou marcada "não se aplica" — 9/9. Acessibilidade marcada "não se aplica" com justificativa.
- [x] Toda lacuna tem severidade, opções e recomendação — LAC-01..34 tratadas: 32 resolvidas por decisão do humano e 2 premissas mantidas (LAC-18, LAC-19); nenhum marcador BLOQUEANTE restante.
- [x] Nenhuma decisão técnica de implementação vazou para o spec — stack, PaaS, provedores, nomes de classe e caminhos novos ficam para o `plan.md`. Os nomes técnicos citados (`POST /chat`, tabelas, tools, eventos SSE, `CHAOS_RATE`, `demo_NN`, `docs/results.md`, `prompts/`, `api/`) são vocabulário do documento-fonte, não decisões deste spec.
