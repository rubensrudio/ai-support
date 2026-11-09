# ADR 0009: Streaming chat over SSE with typed events, a terminal guard and cancellation

- Status: accepted
- Date: 2026-11-09
- Phase: 09

## Context

Until phase 08 the chat answered with one JSON body (`POST /chat`): the Customer stared at a
spinner until the agent had run every tool and the model had written the last word. On a
multi-step turn that is seconds of silence, and the UI could not say what the agent was doing
or let the Customer stop it. Forces:

- **Perceived latency.** What the Customer feels is the time to the first token (TTFT), not
  the total time. The spec sets no absolute target; it asks to measure and publish both
  (STR-06).
- **More than text.** Besides tokens the UI needs the agent's steps (`tool_start` /
  `tool_end`, STR-02), the citations (STR-03), a final status with ids and the trace id
  (`done`, STR-05) and errors in the Customer's language (STR-90).
- **POST with a body and a cookie.** The request carries the message (CT-14) and the identity
  comes only from the `atlas_session` cookie; the browser's `EventSource` only does `GET`
  without a body.
- **Cancellation and disconnects.** The Customer can press "cancel" (STR-04) or close the tab
  (STR-91). Tools that already ran must keep their effects and audit rows, and the partial
  answer must not vanish from the history (STR-07, LAC-16). Cancelling after `done` changes
  nothing (STR-92).
- **Providers that do not stream.** The subscription CLI adapters (`claude_cli`,
  `codex_cli`) run one CLI process per call and return the whole answer. By user decision they
  stay supported without streaming instead of being dropped.
- **Exactly one ending.** A client must be able to trust that a stream ends with one terminal
  event and that nothing follows it, whatever fails in between.

`demo_09` measures TTFT and the total time over N questions (pt and en, personas `alex` and
`bia`, logged in through `/auth/login`): every question goes to `POST /chat` (Before: the answer
arrives whole, TTFT = total) and to `POST /chat/stream` (After: TTFT = first `event: token`
parsed by the client; the `start` event carries no answer text and does not count). It runs the API in process with an httpx client over a streaming ASGI
transport (httpx's own `ASGITransport` buffers the whole body, which would hide TTFT) or against
a running API with `--base-url`. The numbers are in the `| 09 |` row of `docs/results.md`.

## Decision

1. **SSE over `POST`, consumed with `fetch`.** `POST /chat/stream` (body CT-14) and
   `POST /chat/confirmations/{id}/stream` (body CT-46) answer `200 text/event-stream` with
   `Cache-Control: no-cache` and `X-Accel-Buffering: no` (so a reverse proxy does not buffer
   the body) (`api/chat/stream_routes.py`, CT-76). Authentication and validation run before the
   stream opens: no session is `401 AUTH_REQUIRED` and a blank or too long message is `422`,
   both as the usual JSON error envelope, and the LLM is never called. The web client
   (`web/src/app/core/chat/chat-stream.service.ts`, CT-51) uses `fetch` with
   `credentials: 'include'` and reads `response.body` as a `ReadableStream`, parsing it
   incrementally with `parseSse` (`sse-parser.ts`); `EventSource` is not used because it
   cannot `POST` a body. The JSON `POST /chat` stays for demos, evals and scripts.
2. **Typed events, one per SSE block.** The stream carries the CT-45 events of
   `api/agent/events.py`, mirrored by the `StreamEvent` union of `chat.models.ts`: `start`,
   `token`, `tool_start`, `tool_end`, `citation`, `done` and `error`. `encode_sse(event)`
   (`api/streaming/events.py`, CT-50) writes `event: <type>\ndata: <json>\n\n`, the JSON on
   one line with UTF-8 kept as is. The `start` event (`StartEvent`: the turn's
   `conversation_id` and `trace_id`) is always the first event, sent once before any token, so
   a client that cancels or sees an `error` before `done` still knows which conversation and
   trace the turn belongs to. After it, only the relative order is fixed:
   `start, (token* tool_start tool_end)*, token*, citation*, done`. Text the model writes
   before a tool call in the same response is streamed as `token`s too, each `tool_start` is
   closed by its `tool_end` before the next model call, citations are optional and come after
   the answer is committed, and `done` is last. `done.content` is the text of the last model
   call, so on a turn with tools the concatenated tokens end with it rather than equal it.
   The phase 09 eval cases (`evals/datasets/phase_09.jsonl`) check exactly this: `start`
   first, a required relative order, `citation*` allowed, and tokens equal to `done.content`
   only on turns without tools.
3. **Streaming inside the agent loop.** Every model call goes through
   `stream_or_complete(llm, request)` (`api/llm/streaming.py`): each non-empty `text_delta`
   becomes a `token` at once, and the deltas, tool calls, usage and stop reason are folded back
   into one `LLMResponse`, so the guards, confirmations, savepoints and `AgentState` of phases
   06 and 07 are unchanged. The Anthropic adapter streams natively.
4. **Fallback for providers without streaming.** When `stream()` raises
   `NotImplementedError` before the first event (`claude_cli`, `codex_cli`),
   `stream_or_complete` calls `complete()` and synthesises the same event sequence: one
   `text_delta` with the whole text, the tool calls, `usage`, `stop`. The API and the UI see the
   same contract; only the timing differs (TTFT about the total time). `codex_cli` has no
   tools, so it takes the plain chat path, which answers in `done.content` without `token`
   events.
5. **Terminal guard.** `guard_terminal(events)` forwards events until the first terminal one
   (`done` or `error`), then stops pulling and closes the source, so the stream ends with
   exactly one terminal event, always last (STR-05). The route turns any exception into one
   `event: error` with the catalogue code, the message in the Customer's language and the
   `trace_id`, after rolling back the request's uncommitted work; no `done` follows it
   (STR-90). A source that ends without a terminal event also ends with an `INTERNAL_ERROR`
   event: the guard never invents a `done` it cannot fill.
6. **Cancellation persists the partial answer.** The route checks
   `request.is_disconnected()` before pulling each event; on a disconnect (the web client's
   `AbortController.abort()` when the Customer cancels, or a closed tab) it closes the service
   stream. The chat service catches the cancellation (`CancelledError` or `GeneratorExit`),
   stores the text already streamed as an assistant message with `status='cancelled'` (it is
   part of the history of the next turns; nothing is stored when no token was sent), and
   commits, so the tools that already ran keep their effects and audit rows. The commit runs in
   a shielded cancel scope, because Starlette re-delivers the cancellation at every `await`.
   Once the answer is committed, a cancellation changes nothing (STR-92).

## Alternatives considered

- **WebSocket.** Full duplex would let the client send "cancel" on the same channel, but it
  needs its own authentication handshake, does not go through the plain HTTP middleware
  (trace id, error envelope) and is harder to proxy. The chat is one request, one answer
  stream; closing the HTTP request is a good enough cancel signal.
- **`EventSource` with a `GET`.** Native reconnection and parsing, but no request body: the
  message would have to go in the URL (logs, length limits) or in a separate "create" call
  followed by a `GET` per turn, which doubles the state to keep.
- **Long polling or chunked JSON lines.** Polling adds latency and requests; NDJSON over a
  plain chunked response works but reinvents what SSE already names (event type, framing) and
  proxies are less likely to pass it through unbuffered.
- **Dropping the CLI providers or emulating streaming.** Splitting a finished answer into fake
  deltas would report a TTFT that never happened; refusing the providers would break the user
  decision to keep the subscription CLIs. The honest fallback keeps one contract and lets
  `demo_09` show that TTFT is about the total time with them.
- **Discarding the partial answer on cancel.** Simpler, but the Customer would lose text they
  already read and the next turn would answer as if it never existed (LAC-16).

## Consequences

- The first token reaches the Customer as soon as the model writes it, and the UI shows each
  agent step and the citations as they happen. With a streaming provider TTFT drops from the
  total time to the time of the first delta plus the work done before the model call
  (persisting the user message, retrieval).
- Two chat endpoints to keep in sync: the JSON `POST /chat` folds the same events
  (`ChatService.collect`), so a change of the event contract touches both.
- Errors after the stream opened can no longer change the HTTP status (it is already 200): the
  client must read the `error` event. Errors before it opens keep the JSON envelope.
- The CLI providers give no TTFT gain; only the transport changes.
- Text the model writes before a tool call in the same response is streamed too (the loop
  cannot know a tool call follows); it stays in the model's context, while `done.content` is the
  text of the last model call. Clients and evals must not assume that the tokens of a turn with
  tools equal `done.content`, nor a fixed sequence beyond `start` first and `done` last.
- The phase 10 output guardrail will have to filter a stream: releasing text by approved
  sentence adds to TTFT, and `demo_09` is where that cost will show (ADR 0010).
