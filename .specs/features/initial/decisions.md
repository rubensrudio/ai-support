# Decisões — initial (Agente de Suporte com IA)

Etapa: gate de clarificação do `/generate-plan` (após spec-writer, 1ª passada).

## Decisões do humano

### LAC-01 — Idioma do produto (corpus, UI, respostas, evals)
- **Data**: 2026-11-07
- **Opções apresentadas**: A) inglês em tudo B) português em tudo C) bilíngue
- **Recomendação do spec-writer**: A
- **Escolha**: C
- **Observações do humano**: escolheu bilíngue contra a recomendação. Corpus, UI, respostas e datasets de eval cobrem inglês e português; o full-text precisa suportar os dois idiomas.

### LAC-02 — Modelo de login de teste e isolamento entre visitantes na demo
- **Data**: 2026-11-07
- **Opções apresentadas**: A) uma conta compartilhada B) conta efêmera por visitante (24 h) C) algumas personas fixas com restauração diária
- **Recomendação do spec-writer**: C
- **Escolha**: C
- **Observações do humano**: —

### LAC-03 — Quais tools exigem confirmação na UI
- **Data**: 2026-11-07
- **Opções apresentadas**: A) cancel_order + issue_refund B) A + send_email
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-04 — Comportamento do loop quando o cliente recusa a confirmação
- **Data**: 2026-11-07
- **Opções apresentadas**: A) a recusa volta ao modelo como resultado e ele não pode repetir a chamada B) o loop encerra com mensagem fixa
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-05 — issue_refund valida a política no servidor?
- **Data**: 2026-11-07
- **Opções apresentadas**: A) validação obrigatória no servidor B) só o modelo consulta check_refund_policy
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-06 — Regra de elegibilidade de reembolso
- **Data**: 2026-11-07
- **Opções apresentadas**: A) integral se cancelado antes do envio ou até 7 dias após a entrega B) até 30 dias C) prazo configurável, padrão 7
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-07 — Estados do Pedido e quando é cancelável
- **Data**: 2026-11-07
- **Opções apresentadas**: A) cancelável antes de "enviado" B) antes de "entregue" C) qualquer estado não terminal
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-08 — Ação ao atingir o limite de passos ou de custo
- **Data**: 2026-11-07
- **Opções apresentadas**: A) encerra e lista as ações já executadas B) última chamada ao modelo sem tools
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-09 — Valores de rate limit e teto diário
- **Data**: 2026-11-07
- **Opções apresentadas**: A) 20 msg/h por conta e US$ 2/dia B) 50 msg/dia e US$ 5/dia C) configurável com os padrões de A
- **Recomendação do spec-writer**: C
- **Escolha**: C
- **Observações do humano**: —

### LAC-10 — Entrada do endpoint de triagem
- **Data**: 2026-11-07
- **Opções apresentadas**: A) texto livre B) id de conversa C) ambos
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-11 — Valores permitidos de categoria, prioridade e sentimento
- **Data**: 2026-11-07
- **Opções apresentadas**: A) enums fechados (lista no spec) B) texto livre em categoria e prioridade
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-12 — Triagem com JSON ainda inválido após 2 novas tentativas
- **Data**: 2026-11-07
- **Opções apresentadas**: A) erro tipado B) objeto padrão com precisa_humano=true
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-13 — Ação ao detectar injection na entrada
- **Data**: 2026-11-07
- **Opções apresentadas**: A) bloqueia sem chamar o modelo B) segue com flag C) só registra
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-14 — Ação ao detectar dado pessoal na entrada
- **Data**: 2026-11-07
- **Opções apresentadas**: A) mascara antes do LLM e antes de persistir B) bloqueia C) só registra
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-15 — O que é "fora da política" no guardrail de saída
- **Data**: 2026-11-07
- **Opções apresentadas**: A) + promessa de ação não executada + assunto fora do suporte B) + só assunto fora do suporte
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-16 — Cancelamento no meio do stream: o que persiste
- **Data**: 2026-11-07
- **Opções apresentadas**: A) resposta parcial salva como cancelada; tools executadas permanecem B) descarta a parcial
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-17 — Onde ficam o trace_id e o dashboard
- **Data**: 2026-11-07
- **Opções apresentadas**: A) trace_id na UI do chat, dashboard só na ferramenta de observabilidade B) tela de Operador no app C) tudo só na ferramenta
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-20 — Orçamento de contexto e gatilho do resumo
- **Data**: 2026-11-07
- **Opções apresentadas**: A) configurável, padrão 8.000 tokens B) últimas 20 mensagens + resumo
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-21 — Critério e ação para chamada repetida
- **Data**: 2026-11-07
- **Opções apresentadas**: A) 2 seguidas → erro ao modelo; na 3ª encerra B) qualquer repetição encerra
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-22 — Escala, base e limite do gate de evals
- **Data**: 2026-11-07
- **Opções apresentadas**: A) 0–100, base = branch principal, bloqueia se o geral cair > 2 B) idem, por categoria
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-23 — Retenção de conversas, traces e auditoria na demo
- **Data**: 2026-11-07
- **Opções apresentadas**: A) 7 dias B) 30 dias C) indefinida
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-24 — Método de medir a taxa de alucinação (fase 3)
- **Data**: 2026-11-07
- **Opções apresentadas**: A) LLM-as-judge + revisão manual por amostra B) anotação manual integral
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-25 — Ferramenta de observabilidade indisponível
- **Data**: 2026-11-07
- **Opções apresentadas**: A) atendimento segue e descarta a telemetria, mantendo o log local B) a requisição falha
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-26 — Tamanho máximo da mensagem
- **Data**: 2026-11-07
- **Opções apresentadas**: A) 4.000 fixo B) 2.000 fixo C) configurável, padrão 4.000
- **Recomendação do spec-writer**: C
- **Escolha**: C
- **Observações do humano**: —

### LAC-27 — Destinatário de send_email
- **Data**: 2026-11-07
- **Opções apresentadas**: A) sempre o e-mail do cliente da sessão B) o modelo informa o destinatário
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-28 — Verificador de guardrail falha
- **Data**: 2026-11-07
- **Opções apresentadas**: A) fail-closed B) fail-open com alerta
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-29 — Primário falha depois que o streaming já começou
- **Data**: 2026-11-07
- **Opções apresentadas**: A) descarta e refaz no secundário (evento novo) B) erro + resposta degradada C) fallback só antes do 1º token, depois erro
- **Recomendação do spec-writer**: C
- **Escolha**: C
- **Observações do humano**: —

### LAC-30 — Moeda dos valores fictícios no produto bilíngue
- **Data**: 2026-11-07 (etapa: spec-writer, 2ª passada)
- **Opções apresentadas**: A) USD única, formatada pelo idioma B) BRL única C) moeda por persona (EN→USD, PT→BRL)
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-31 — Idioma da resposta a mensagem que não está em PT nem em EN
- **Data**: 2026-11-07 (etapa: spec-writer, 2ª passada)
- **Opções apresentadas**: A) em inglês, avisando que atende só PT/EN B) no idioma da UI C) tenta o idioma da mensagem
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-32 — Idioma inicial da UI
- **Data**: 2026-11-07 (etapa: spec-writer, 2ª passada)
- **Opções apresentadas**: A) o do navegador se for PT/EN, senão EN B) sempre EN
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-33 — Gate de evals sem run anterior na branch principal
- **Data**: 2026-11-07 (etapa: spec-writer, 2ª passada)
- **Opções apresentadas**: A) o primeiro run vira a base e não bloqueia B) bloqueia até o Operador registrar uma base manualmente
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-34 — Conflito de retenção entre LAC-02 C e LAC-23 A
- **Data**: 2026-11-07 (etapa: spec-writer, 2ª passada; levantada pelo orquestrador)
- **Opções apresentadas**: A) conversas apagadas na restauração diária (DEMO-07); traces e auditoria expurgados após 7 dias (DEMO-10) B) conversas também retidas por 7 dias; restauração diária só reseta o seed
- **Recomendação do spec-writer**: A (interpretação aplicada no spec.md)
- **Escolha**: A
- **Observações do humano**: —

### LAC-35 — Quem pode chamar o endpoint de triagem (TRI-01..08)
- **Data**: 2026-11-07 (etapa: plan-architect)
- **Opções apresentadas**: A) só o Operador, com chave no cabeçalho `X-Operator-Key` (variável `ATLAS_OPERATOR_API_KEY`) B) qualquer persona autenticada, com a sessão do chat C) as duas: sessão de persona OU chave de operador
- **Recomendação do plan-architect**: C
- **Escolha**: C
- **Observações do humano**: — (persona sujeita a rate limit DEMO-02/08 e teto diário DEMO-03; chave de operador para uso programático, CI e evals)

### LAC-36 — Onde fica o Langfuse da demo pública
- **Data**: 2026-11-07 (etapa: plan-architect)
- **Opções apresentadas**: A) auto-hospedado só no ambiente local (perfil `observability` do docker-compose); demo pública exporta para o Langfuse Cloud (conta do Operador) e o job de expurgo apaga lá traces com mais de 7 dias via API B) auto-hospedado também na demo pública C) demo pública sem Langfuse, só log local estruturado
- **Recomendação do plan-architect**: A
- **Escolha**: A
- **Observações do humano**: — (conteúdo dos traces já mascarado por GRD-07; expurgo de 7 dias conforme DEMO-10)

### LAC-37 — Tamanho do corpus
- **Data**: 2026-11-07 (etapa: plan-architect)
- **Opções apresentadas**: A) ~20 documentos distintos × 2 idiomas = ~40 arquivos B) ~40 documentos distintos × 2 idiomas = ~80 arquivos
- **Recomendação do plan-architect**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-38 — Risco de TASK-019, TASK-031 e TASK-094 (orquestração do ChatService/loop)
- **Data**: 2026-11-07 (etapa: plan-architect, classificação de risco)
- **Opções apresentadas**: A) médio nas três B) alto nas três (âncora AS-3) C) TASK-031 alto (âncora AS-3); TASK-019 e TASK-094 médio
- **Recomendação do plan-architect**: C
- **Escolha**: C
- **Observações do humano**: —

### LAC-39 — Risco de TASK-068 (registro de tools) e TASK-072 (get_subscription + send_email)
- **Data**: 2026-11-07 (etapa: plan-architect, classificação de risco)
- **Opções apresentadas**: A) crítico nas duas (âncora AS-1) B) médio nas duas C) TASK-068 crítico, TASK-072 médio
- **Recomendação do plan-architect**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-40 — Risco de TASK-107 (pipeline guardado) e TASK-111 (prompt cache + custo)
- **Data**: 2026-11-07 (etapa: plan-architect, classificação de risco)
- **Opções apresentadas**: A) TASK-107 alto (AS-3); TASK-111 médio B) TASK-107 alto (AS-3); TASK-111 crítico (AS-2) C) médio nas duas
- **Recomendação do plan-architect**: B
- **Escolha**: B
- **Observações do humano**: —

### LAC-41 — Risco de TASK-001 (Settings com SecretStr) e TASK-149 (render.yaml)
- **Data**: 2026-11-07 (etapa: plan-architect, classificação de risco)
- **Opções apresentadas**: A) TASK-001 médio; TASK-149 alto (AS-8) B) médio nas duas C) TASK-001 crítico; TASK-149 alto
- **Recomendação do plan-architect**: A
- **Escolha**: A
- **Observações do humano**: —

### LAC-42 — Códigos de erro para rota e método desconhecidos
- **Data**: 2026-11-08 (etapa: orchestrate, fase 00)
- **Opções apresentadas**: A) acrescentar `NOT_FOUND` (404) e `METHOD_NOT_ALLOWED` (405) ao catálogo 8.3 B) usar `INTERNAL_ERROR` para 404/405 de framework
- **Recomendação do orquestrador**: A
- **Escolha**: A
- **Observações do humano**: —

## Premissas assumidas (lacunas não bloqueantes)

### LAC-18 — Bônus da fase 5 no escopo?
- **Premissa**: só a busca híbrida com RRF entra (P3, RAG-12/13); o reranker fica fora do escopo.
- **Reversibilidade**: alta
- **Onde impacta**: spec.md — RAG-12, RAG-13; plan.md — componente de retrieval

### LAC-19 — Comparação de provedores de embeddings é obrigatória?
- **Premissa**: não; usa-se um provedor escolhido no plan.md e a comparação é conteúdo opcional.
- **Reversibilidade**: alta
- **Onde impacta**: plan.md — escolha de provedor de embeddings
