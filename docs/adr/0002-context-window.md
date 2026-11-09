# ADR 0002: Persist conversation history and bound it with a summarised context window

- Status: accepted
- Date: 2026-11-09
- Phase: 02

## Context

Phase 01 left Atlas stateless: `demos/demo_01_stateless.py` shows that "My name is Rubens" followed
by "What is my name?" in two independent calls cannot recall the name (CHAT-02). A support agent
has to remember what the customer said earlier in the same conversation (CHAT-03, CHAT-04).

Sending every earlier message has two costs. Input tokens grow with every turn, so each
conversation gets more expensive and slower as it goes on. Long enough conversations also run
into the model's context window. Phase 02 needs a context budget: a default of 8,000 history tokens
that can be changed without code changes (CHAT-06, CHAT-08, LAC-20). Without that, the cost of a
conversation has no upper bound.

`demos/demo_02_context_window.py` measures it. It repeats the name scenario in one conversation (pt
and en), then runs a 50-turn conversation in each history mode and plots the history tokens of
every chat call in `docs/img/phase02_tokens.png`. The totals go to the phase 02 row of
`docs/results.md`. In the offline run (`--provider fake`), the `full` history peaks at about 11,000
tokens and keeps growing, while the `summarized` history stays below 8,000 and still recalls the
name on turn 50. Only the Operator's run with a real provider records the measured numbers.

## Decision

Conversation history lives in PostgreSQL and the service builds a bounded window from it on every
call.

- **Storage.** `conversations` and `messages` (migration `0003`, `api/chat/models.py`) hold every
  user and assistant message in arrival order (`messages.id` is a bigserial). Each conversation
  belongs to one customer. `api/chat/repository.py` filters every read by `customer_id`, and a
  conversation owned by someone else gets the same 404 as one that does not exist.
- **Conversation id.** `POST /chat` without `conversation_id` starts a new conversation. With
  one, it continues that conversation. `ChatService` (`api/chat/service.py`) stores the user
  message first, sends the window to the LLM and stores the answer.
- **Budget.** `ATLAS_CONTEXT_BUDGET_TOKENS` (default `8000`) caps the history tokens per call: the
  summary block plus the message window. The fixed system prompt is not counted. Tokens are counted
  locally with `tiktoken` (`o200k_base`, `api/llm/tokens.py`, DA-9). That is an estimate with no
  network call per message, and cost still comes from the provider's `usage`.
- **Summarise the oldest messages.** `build_history` in `api/chat/context.py` keeps the most
  recent messages that fit (the last one is always kept intact). It folds the older ones, together
  with the previous summary, into a new summary through an LLM call with `purpose="summarize"` and
  `prompts/summarize.md`. The summary goes to the model as an extra system block. It is stored in
  `conversations.summary` with `summary_upto_message_id`, so each message is summarised once. A
  history that fits the budget never triggers a summarise call.
- **"Before" stays selectable (DA-16).** `ATLAS_HISTORY_MODE=full` sends every message and never
  summarises. `demo_02` uses it as the baseline, and a call without `conversation_id` still
  reproduces the phase 01 problem in `demo_01`.

## Alternatives considered

- **Sliding window: send only the last N messages or the last N tokens.** It is the simplest and
  cheapest option, with no extra LLM call. But it silently drops early facts, and the name stated
  on turn 1 is exactly what goes first. The 50-turn demo asks for that name on the last turn, so a
  pure window fails the scenario this phase exists to fix.
- **Send the full history every time.** Perfect recall until the context limit, but cost and
  latency grow without bound, and one long conversation eventually fails outright. We keep it only
  as the measured baseline (`ATLAS_HISTORY_MODE=full`).
- **Keep history client-side and resend it in each request.** No storage on our side, but the
  client could rewrite what the assistant "said". It also breaks the per-customer ownership checks
  and leaves no record for later phases (traces, evals, retention).
- **Retrieve relevant earlier messages with embeddings.** Good for very long or multi-session
  memory, but it needs the vector machinery of phases 04 and 05 and still loses the order of the
  conversation. Revisit if summaries prove too lossy.
- **Count tokens with the provider's counting API.** Exact, but it adds a network round trip per
  message and ties the budget to one provider. The local estimate is close enough for a budget
  with headroom.

## Consequences

- A conversation's input cost now has an upper bound: once the history passes the budget, each
  call sends at most about 8,000 history tokens plus the system prompt, whatever the length of
  the conversation.
- Summaries are lossy. A detail that the summariser leaves out is gone from the model's context,
  even though it is still in the database. The summarise prompt has to keep facts about the
  customer, such as names, identifiers and open requests. Phase 13 evals and the phase 02 cases in
  `evals/datasets/phase_02.jsonl` check this kind of recall.
- Each summarisation is an extra LLM call that adds latency and cost to the turn that triggers
  it. Because summaries are cached in `conversations.summary`, this happens once every several
  turns, not on every message.
- Conversations are personal data stored on our side, so masking before storage (phase 10) and
  retention and deletion (public demo stage) now apply to them.
- The token count is an estimate (`o200k_base`), not the provider's tokenizer, so the real number
  of input tokens can differ slightly from the budget.
