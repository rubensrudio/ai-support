# ADR 0007: Own agent loop with step, cost and repeated-call limits

- Status: accepted
- Date: 2026-11-09
- Phase: 07

## Context

Phase 06 gave the model tools, but the requests it handled were one tool deep: "cancel my
order #N" is one `cancel_order` call. Real support requests chain several actions: "cancel my
last order, refund it to my card and email me the receipt" needs
`list_orders → get_order → check_refund_policy → cancel_order → issue_refund → send_email`,
with two confirmations in the middle (AGT-01, AGT-06). Letting the model drive a chain of
calls raises new risks:

- **Runaway loops.** A model that never gives a final answer, or keeps calling the same tool
  with the same arguments, burns requests and money without progress (AGT-02, AGT-05,
  AGT-09, AGT-10, LAC-21).
- **Cost per request.** Every step is a full model call with the growing transcript. The
  project needs a hard ceiling in USD per customer request (AGT-03, AS-2, premise P-3).
- **Unknown cost on subscription providers.** `claude_cli` and `codex_cli`
  (`SUBSCRIPTION_PROVIDERS`, LAC-43) run on the developer's subscription: there is no price per
  token, so `estimate_cost` cannot answer for them.
- **Honest endings.** When a limit stops the agent, some actions may already have happened
  (an order cancelled, a refund issued). The customer must be told exactly what was done, and
  never something that was not (AGT-08).
- **Confirmations.** Destructive steps suspend the loop between HTTP requests (phase 06,
  TOOL-05); the limits have to add up across those suspensions.

`demo_07` measures the change on 15 multi-step tasks in pt and en
(`demos/data/phase07_tasks.jsonl`): the same tasks with `ATLAS_AGENT_MAX_STEPS=1` (the model
gets one call, and the tools it asks for in that last step never run) and with the default
limit, reporting the success rate (exact tool trajectory
and expected outcome in the database) and the mean number of model calls per task. The numbers
are in the `| 07 |` row of `docs/results.md`.

## Decision

1. **Own loop, no framework.** `AgentLoop` (`api/agent/loop.py`, DA-20) keeps the plain
   cycle model → tool calls → tool results → model until a final answer, as in phase 06. Each
   call emits `tool_start`/`tool_end`; a failed tool goes back to the model as a result
   (AGT-04). The loop state (`AgentState`, now version 2) is plain JSON stored in
   `pending_confirmations.agent_state`, so a destructive step still suspends the request and
   `resume` continues it from the next HTTP request. A version 1 state from phase 06 loads with
   an empty cost and repeat history.
2. **Guards as plain data** (`api/agent/guards.py`, CT-47). They live inside `AgentState`, so
   they add up across `run` and every `resume` of the same customer request:
   - `StepBudget(settings.agent_max_steps)`: `consume()` before every model call;
     `ATLAS_AGENT_MAX_STEPS`, default 8 (the AGT-06 chain needs 7). The 9th call is refused
     and the loop ends with `step_limit`. Tool calls returned by the call that used the last
     step never run, nor are they prechecked or suspended: the loop ends with `step_limit`
     right away, since no step would be left to read their results (AGT-02).
   - `CostBudget(settings.agent_max_cost_usd)`: the loop checks the budget **before** every
     model call (a state restored already at the limit ends before calling the model) and,
     after every call, `add(cost)` with `estimate_cost(model, usage)`;
     `ATLAS_AGENT_MAX_COST_USD`, default 0.25 USD (P-3). The call that reaches the limit always
     ends the loop with `cost_limit`, **even when it is a final answer**: no tool of it runs, no
     model call follows and the customer gets the limit message (AGT-03, AGT-08). A model with
     no known price (`UnknownModelPricing`) is treated as reaching the limit, the safe side for
     AS-2 (logged as `agent_cost_unpriced_model`, not counted as an unknown cost).
   - `RepeatDetector.check(call)` compares `(tool, canonical JSON arguments)` with the
     immediately previous call: the 2nd identical call in a row is not executed and goes back
     to the model as `repeat_error` (`{"error": "repeated call", "error_code":
     "REPEATED_CALL"}`), the 3rd ends the loop with `repeat_limit` (LAC-21). Different
     arguments reset the streak. Both cases log `agent_repeat_detected`. A refused destructive
     repeat still answers `ACTION_ALREADY_REFUSED` (TOOL-14 takes precedence).
3. **Unknown cost on subscription providers (user decision).** For `claude_cli` and
   `codex_cli` the cost of a call is `None`: it is **not** added to the budget, it is counted in
   `CostBudget.unknown_calls` and logged as `agent_cost_unknown`. The step limit and the repeat
   detector still bound these runs. We chose this over refusing the providers (they are how the
   project is tested locally without an API key) and over inventing a price (a made-up number
   would make the cost limit look meaningful when it is not).
4. **Limit message from the audit log.** Any limit ends with `done{status: 'limit_reached'}`,
   without a new model call, and the text `AGENT_LIMIT_REACHED` (pt/en, `api/messages.py`)
   listing the successful actions read from `audit_log` (`list_successful_actions`, CT-39)
   for **every** request of the logical turn: the `trace_id` of the `run` and of each `resume`
   (each confirmation is a new HTTP request with its own trace), oldest first, so an action
   confirmed in an earlier request is still listed. Never from what the model said it did. The loop logs
   `agent_limit{reason, steps, cost_usd, unknown_cost_calls}` (AGT-08).

## Alternatives considered

- **An agent framework** (LangGraph, the OpenAI Agents SDK, the Claude Agent SDK). Less code
  for the happy path, but the suspension across HTTP requests, the server-side confirmation and
  the per-request guards would sit on top of the framework's own state and retry model, and
  the project's goal is to learn and show the loop itself. The loop is one module and has
  no dependency beyond the `LLMClient` protocol.
- **Only a step limit.** Simple and enough to stop runaway loops, but it does not bound cost:
  eight steps with a long transcript on an expensive model can cost far more than one support
  request is worth.
- **Only a cost limit.** Does not bound subscription providers (unknown cost) nor a cheap model
  stuck in a loop.
- **A global repeat counter** (same call anywhere in the request). Would punish legitimate
  re-reads, such as `get_order` before and after `cancel_order`; comparing only with the
  previous call catches the stuck-model pattern without false positives.
- **Ending at the first repeat.** A model often recovers when told the call was repeated; one
  `repeat_error` gives it that chance before `repeat_limit`.
- **Letting the model write the limit message.** It would need one more model call (beyond the
  limit) and could claim actions that never happened; the audit log is the source of truth.

## Consequences

- A customer request has a hard ceiling of model calls and, on priced providers, of USD; a
  stuck model ends after at most three identical calls.
- The limit message is always truthful about side effects, because it comes from `audit_log`
  and not from the model, and it covers the actions confirmed in earlier requests of the same
  turn.
- A final answer that crosses the cost limit is not delivered: the customer gets the limit
  message instead, and the tokens of that answer are spent for nothing. We accept this so the
  limit is a hard stop, never "one more answer".
- On `claude_cli`/`codex_cli` the cost limit does not apply; only steps and repeats bound the
  run, and `unknown_calls` in the logs shows how many calls were not priced.
- A legitimate request longer than 8 steps fails with `limit_reached`; raise
  `ATLAS_AGENT_MAX_STEPS` if real traffic needs it. The step count includes the final answer,
  so the AGT-06 chain (6 tools) uses 7 of the 8 steps. Tools asked in the last step are
  dropped, so with `ATLAS_AGENT_MAX_STEPS=1` (the "before" of `demo_07`) no tool ever runs and
  every multi-step task ends `limit_reached` after one model call.
- `demo_07 --provider fake` scripts each task's expected trajectory, so it proves the loop, the
  confirmations and the limits, not the model's judgement. **The real row is pending a run with
  `--provider real`** (`uv run python -m demos.demo_07_agent --provider real`); with a real model
  the demo reports what happened, task by task.
- The demo resets the personas' commerce data before every task; order numbers change on every
  reset, so tasks and eval cases name orders by placeholders (`{paid_order_id}`).
- Phase 08 adds structured output for triage on top of the same client; the loop and its
  guards stay unchanged.
