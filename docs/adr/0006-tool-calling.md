# ADR 0006: Tool calling with session identity, UI confirmation and server-side policy

- Status: accepted
- Date: 2026-11-09
- Phase: 06

## Context

Up to phase 05 Atlas only answers with text: asked to cancel an order, the model can at best
explain how the customer could do it. Phase 06 lets the model act on the customer's account
through tools: list and read orders, cancel an order, check the refund policy and issue a
refund, read the subscription and send an e-mail (TOOL-01..19). An LLM that can change data
raises questions that text answers never did:

- **Who is the customer?** The model reads untrusted text (the customer's message, retrieved
  documents, tool results). If a tool took a `customer_id` or an e-mail from the model, a
  prompt injection could act on another account (TOOL-03, TOOL-18, AS-1).
- **Who decides?** Cancelling and refunding cannot be undone. The customer must see exactly
  what will happen and say yes before it happens (TOOL-05, TOOL-13, LAC-03, LAC-04).
- **Who applies the rules?** The refund policy (cancelled before shipping, or delivered at most
  7 days ago, full amount; LAC-05, LAC-06) and the cancellable statuses (pending or paid,
  LAC-07) cannot depend on the model reading them correctly.
- **Concurrency.** Two tabs, a retry or a double click must not cancel or refund twice
  (TOOL-92, TOOL-93).
- **Local provider.** `claude_cli` (LAC-43) runs the logged-in Claude Code CLI with every
  built-in tool disabled and has no native tool calling, but it is how the project is tested
  locally without an API key.

`demo_06` measures the change: the same "cancel my order #N" / "cancela meu pedido #N" with
`ATLAS_AGENT_TOOLS_ENABLED=false` (text only, the order stays `paid`) and with tools on (the
order is `cancelled` and `audit_log` has a `success` row with the request's `trace_id`). The
numbers are in the `| 06 |` row of `docs/results.md`.

## Decision

1. **Identity only from the session.** The server builds a `ToolContext` (customer id and
   e-mail, `trace_id`, request session, reply language, now) from the authenticated session
   principal (`require_customer`), never from the model or the stored agent state.
   `ToolRegistry.register` (`api/tools/registry.py`, CT-40) rejects any args model with a field
   in `IDENTITY_FIELDS` (`customer_id`, `user_id`, `email`, `to`, `recipient`) or without
   `extra='forbid'`, so a call cannot even name a customer. Every query filters by
   `ctx.customer_id`; another customer's order answers `ORDER_NOT_FOUND`, like a missing one
   (TOOL-90). `send_email` always writes to the session e-mail (LAC-27).
2. **One schema for model and validation.** The JSON schema sent to the model is
   `args_model.model_json_schema()` of the same Pydantic model that validates the call
   (TOOL-02). An unknown tool or invalid arguments return `UNKNOWN_TOOL` /
   `INVALID_ARGUMENTS` to the model and the handler never runs (TOOL-94).
3. **Confirmation in the UI, with the loop state saved.** Only `cancel_order` and
   `issue_refund` are destructive (`DESTRUCTIVE_TOOLS`, `api/tools/catalog.py`). When the
   model calls one, `AgentLoop` (`api/agent/loop.py`, DA-20) stops, serialises its
   `AgentState` into `pending_confirmations.agent_state` (migration `0008`) and the chat
   answers `awaiting_confirmation` with a `ConfirmationPrompt` (`tool`, `target='#<order_id>'`,
   and for a refund the amount computed by the server, never by the model). The web shows a
   confirmation card; `POST /chat/confirmations/{id}` with `confirm` or `refuse` is a new HTTP
   request that resumes the loop from the stored state, with a `ToolContext` built again from
   the current session. A confirmation runs at most once and expires after
   `ATLAS_CONFIRMATION_TTL_MINUTES` (default 15; 404 for a missing or foreign one, 409 for one
   no longer pending, TOOL-96). A refusal goes back to the model as `CUSTOMER_REFUSED`; the
   same call repeated is answered `ACTION_ALREADY_REFUSED` (TOOL-13, TOOL-14). A new message
   instead of a decision refuses the pending confirmation (TOOL-97).
4. **Refund policy on the server.** `evaluate_refund` (`api/tools/refund_policy.py`, CT-41) is
   a pure function of the order and `now`. `check_refund_policy` reports it; `issue_refund`
   evaluates it again with the order locked by `SELECT ... FOR UPDATE`, whatever the model
   said or checked before, and `UNIQUE(order_id)` (`uq_refunds_order_id`) turns a concurrent
   second refund into `ALREADY_REFUNDED` (DA-21, TOOL-15, TOOL-93). No payment gateway.
5. **Atomic cancellation.** `cancel_order` is one `UPDATE orders ... WHERE id AND customer_id
   AND status IN ('pending', 'paid') RETURNING`: zero rows means not cancellable (or not
   found), so two concurrent cancellations have exactly one effect (DA-21, TOOL-92). No
   check-then-update.
6. **Audit in the same transaction.** Every successful action writes an `audit_log` row
   (`record_audit`, CT-39: tool, arguments, result, outcome, `trace_id`) in the same
   transaction as its effect. The confirmation, the tool effect, the audit row and the answer
   are committed together; if the model fails after the tool ran, everything rolls back and
   the confirmation is still pending.
7. **Emulated tool calling on `claude_cli`.** With tools in the request, the adapter
   (`api/llm/claude_cli_adapter.py`) lists them (name, description, input schema) in the
   system prompt and forces the answer into `{text, tool_calls}` with `--json-schema`; the CLI
   runs nothing. Calls get position-based ids, calls to names outside the request are dropped,
   and tool results are rendered as escaped JSON inside `<tool_result>` blocks so a result can
   never forge a turn. The registry validates and runs the calls exactly as for the native
   providers.
8. **Switchable.** `ATLAS_AGENT_TOOLS_ENABLED` (default `true`) turns the whole agent loop off
   and restores the phase-05 text answers (DA-16); it is the "before" of `demo_06`.

## Alternatives considered

- **Identity as a tool argument checked against the session.** Works only if every handler
  remembers the check, and the model still sees and can try other ids. Removing the field from
  the schema makes the mistake impossible instead of detectable.
- **Confirmation by text ("reply YES to confirm").** No extra endpoint, but the model would
  have to interpret the answer, an injected "yes" could confirm, and nothing binds the "yes"
  to one exact call. A card with an id bound to a stored call is unambiguous.
- **Keeping the loop in memory while waiting.** Simpler, but it dies with a restart, does not
  work with more than one instance and holds a request open for minutes. The serialised state
  survives reconnects (DA-20).
- **Trusting the model with the refund policy** (policy only in the prompt or in
  `check_refund_policy`). The model could skip the check, misread a date or be talked into an
  exception; re-evaluating inside `issue_refund` costs one query.
- **Check then update for cancellation, or application locks.** Racy or heavier; the
  conditional `UPDATE ... RETURNING` is atomic in PostgreSQL without extra locks.
- **No tool calling on `claude_cli`** (text only locally). Then the phase could only be tested
  with an API key; emulation keeps the same registry and loop under test.

## Consequences

- A prompt injection can at most ask for an action on the customer's own account, and a
  destructive one still needs a click on the card; policy and concurrency rules hold whatever
  the model does.
- Every action is traceable: `audit_log.trace_id` joins the action to the logs and the LLM
  calls of the request that confirmed it.
- Destructive actions cost an extra round trip (the confirmation request) and a row in
  `pending_confirmations`; expired rows stay until a cleanup (`expire_stale`).
- `demo_06 --provider fake` scripts the `cancel_order` call, so it proves the plumbing, not
  the model's judgement. **The real row is pending a run with `--provider real`**
  (`uv run python -m demos.demo_06_tools --provider real`); with a real model the demo reports
  what happened, including "no confirmation requested" when the model does not call the tool.
- `claude_cli` tool calling is an emulation for local testing: it depends on the model filling
  the JSON schema and is not the reference for latency or cost.
- The demo resets the personas' commerce data (`seed_commerce`), like the daily demo reset;
  order numbers change on every reset, so nothing hard-codes them (eval cases use
  `{paid_order_id}` placeholders).
- Phase 07 builds on this loop: step, cost and repeated-call limits (AGT-02..10).
