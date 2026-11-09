"""Phase 09 demo: time to first token (TTFT) and total time of `POST /chat/stream` (SSE) vs
`POST /chat` (JSON) (STR-06, DOD-02).

1. **Questions.** `--n` short support questions (default 10), alternating en (persona `alex`)
   and pt (persona `bia`) and cycling `QUESTIONS` when `--n` is larger. Every question opens a
   new conversation (no `conversation_id`), so the history never grows between measurements.
2. **Run.** The personas log in through `POST /auth/login` (the real session cookie, no
   dependency override of `require_customer`). For every question the demo sends it once to
   `POST /chat` and once to `POST /chat/stream`, back to back, so both modes see the same
   conditions:
   - **Before** = `POST /chat`: the JSON answer arrives whole, so TTFT = total time (the time
     until the response body is read);
   - **After** = `POST /chat/stream`: TTFT is the time until the first `event: token` block is
     parsed from the body, total is the time until the stream ends. The `start` event (the
     turn's conversation and trace ids, always first) carries no answer text, so it does not
     stop the TTFT clock. A stream counts as `ok` (`stream_ok`) when `start` is first and
     unique and the stream ends with exactly one `done{status: 'completed'}` with a non-empty
     `content` equal to the concatenated tokens (with tools: the tokens end with it, since
     text before a tool call is streamed too). The plain chat path (`codex_cli`, which has no
     tools, or `ATLAS_AGENT_TOOLS_ENABLED=false`) makes one `complete()` call and sends no
     `token`: the answer is only in `done.content`, so its TTFT is the arrival of `done`.
   The `| 09 |` row of `docs/results.md` gets the p50/p95 (nearest rank) of each.
3. **Client.** By default the demo runs the API in process: an `httpx.AsyncClient` over
   `StreamingASGITransport` on `create_app()`. httpx's own `ASGITransport` buffers the whole
   body before returning the response, which would make TTFT equal to the total time; this
   transport hands every body chunk to the client as the app sends it and reports
   `http.disconnect` only when the client closes the response. `--base-url` measures a running
   API over the network instead (seed the personas there first: `python -m api.seed`; the
   password comes from `ATLAS_PERSONA_PASSWORD`).

`--provider fake` answers with a scripted `FakeLLMClient` wrapped in `PacedFakeLLM`, which
waits `--delta-delay-ms` before each word-sized delta of `stream()` and the same total time in
`complete()`, as a model generating tokens would; without that pause every answer would be
instant and TTFT meaningless. Retrieval is off in the fake run. `real` uses the configured
provider and retrieval. The subscription CLI providers (`claude_cli`, `codex_cli`) do not stream
(user decision): `stream_or_complete` falls back to `complete()` and sends the whole answer as
one token (`claude_cli`) or the plain path answers in `done` (`codex_cli`), so with them TTFT is
about the total time and streaming only changes the transport.
A fake run writes its results under `<tmp>/atlas-demo09/` unless `--results` is given, so it
never replaces the real `| 09 |` row.

Usage:
    uv run python -m demos.demo_09_streaming --provider fake --n 4 --results /tmp/results.md
    uv run python -m demos.demo_09_streaming --provider real        # writes docs/results.md
    uv run python -m demos.demo_09_streaming --provider real --base-url http://localhost:8000
"""

import argparse
import asyncio
import json
import math
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp, Message

from api.chat.service import ChatService, close_embedder, get_chat_service
from api.config import Settings, get_settings
from api.db.engine import dispose_engine, get_db_session
from api.llm.client import LLMClient, LLMError, LLMRequest, LLMResponse, LLMStreamEvent, Usage
from api.llm.fake import FAKE_MODEL, FAKE_PROVIDER, FakeLLMClient, split_deltas
from api.llm.instrumented import InstrumentedLLMClient
from api.main import create_app
from api.messages import Lang
from api.seed.personas import PERSONAS
from demos.common import DEFAULT_RESULTS_PATH, demo_parser, record_result
from demos.demo_01_stateless import seed_demo_principals

PHASE = 9
DEFAULT_N = 10
DEFAULT_DELTA_DELAY_MS = 30.0
IN_PROCESS_URL = "http://atlas.demo"
HTTP_TIMEOUT_S = 180.0
# Seconds to wait for the app to finish after the client closes a streamed response.
_CLOSE_TIMEOUT_S = 5.0
_NO_STREAMING_PROVIDERS = ("claude_cli", "codex_cli")


@dataclass(frozen=True)
class Question:
    language: Lang
    text: str
    answer: str  # scripted answer of `--provider fake`


QUESTIONS: tuple[Question, ...] = (
    Question(
        "en",
        "How do I change the card used for my subscription?",
        "Open Billing in your account, choose Payment method, add the new card and set it as "
        "default. The next renewal will be charged to it.",
    ),
    Question(
        "pt",
        "Como eu troco o cartão usado na minha assinatura?",
        "Abra Cobrança na sua conta, escolha Forma de pagamento, adicione o novo cartão e "
        "marque-o como padrão. A próxima renovação será cobrada nele.",
    ),
    Question(
        "en",
        "Can I pause my plan instead of cancelling it?",
        "Yes. You can pause the plan for up to three months from the Subscription page; billing "
        "stops during the pause and resumes automatically afterwards.",
    ),
    Question(
        "pt",
        "Posso pausar meu plano em vez de cancelar?",
        "Sim. Você pode pausar o plano por até três meses na página Assinatura; a cobrança "
        "para durante a pausa e volta automaticamente depois.",
    ),
    Question(
        "en",
        "How long does a refund take to show up on my card?",
        "Once approved, a refund usually appears on your card statement within five to ten "
        "business days, depending on your bank.",
    ),
    Question(
        "pt",
        "Quanto tempo o reembolso leva para aparecer no cartão?",
        "Depois de aprovado, o reembolso costuma aparecer na fatura do cartão em cinco a dez "
        "dias úteis, conforme o seu banco.",
    ),
    Question(
        "en",
        "Where can I download my invoices?",
        "Go to Billing, then Invoices. Every invoice has a Download button that saves it as a "
        "PDF with your company details.",
    ),
    Question(
        "pt",
        "Onde eu baixo as minhas notas fiscais?",
        "Vá em Cobrança e depois em Notas fiscais. Cada nota tem um botão Baixar que salva o "
        "PDF com os dados da sua empresa.",
    ),
    Question(
        "en",
        "Does the annual plan have a discount?",
        "Yes. The annual plan costs the same as ten monthly payments, so you get two months "
        "free compared with paying monthly.",
    ),
    Question(
        "pt",
        "O plano anual tem desconto?",
        "Sim. O plano anual custa o mesmo que dez mensalidades, então você ganha dois meses em "
        "relação ao pagamento mensal.",
    ),
)


@dataclass(frozen=True)
class Timing:
    language: Lang
    ttft_ms: float
    total_ms: float
    ok: bool


def questions_for(n: int) -> list[Question]:
    """The first `n` questions, cycling `QUESTIONS` (alternating en and pt)."""
    return [QUESTIONS[index % len(QUESTIONS)] for index in range(n)]


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile: the smallest value with at least `p`% of the values <= it."""
    if not values:
        raise ValueError("percentile of an empty sequence")
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


# --- fake provider ---------------------------------------------------------------------------


def _fake_response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        usage=Usage(input_tokens=0, output_tokens=len(split_deltas(text))),
        model=FAKE_MODEL,
        provider=FAKE_PROVIDER,
        stop_reason="end_turn",
    )


def fake_script(questions: Sequence[Question]) -> list[LLMResponse | LLMError]:
    """Two answers per question, in run order: one for `/chat`, one for `/chat/stream`."""
    return [_fake_response(question.answer) for question in questions for _ in range(2)]


class PacedFakeLLM:
    """Wraps a fake so it takes time like a model: `delay_s` before each text delta of
    `stream()`, and the same total time (one delay per delta) before `complete()` returns."""

    def __init__(self, inner: LLMClient, delay_s: float) -> None:
        self.inner = inner
        self.delay_s = delay_s

    async def complete(self, request: LLMRequest) -> LLMResponse:
        response = await self.inner.complete(request)
        await asyncio.sleep(self.delay_s * max(1, len(split_deltas(response.text))))
        return response

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        async for event in self.inner.stream(request):
            if event.kind == "text_delta":
                await asyncio.sleep(self.delay_s)
            yield event


def _fake_service(llm: LLMClient, settings: Settings) -> Callable[[AsyncSession], ChatService]:
    """`get_chat_service` override: the paced fake, the request session, no retriever."""

    def dependency(session: Annotated[AsyncSession, Depends(get_db_session)]) -> ChatService:
        return ChatService(llm, settings, session)

    return dependency


# --- in-process streaming transport ----------------------------------------------------------


class _QueueStream(httpx.AsyncByteStream):
    def __init__(
        self,
        chunks: "asyncio.Queue[bytes | BaseException | None]",
        task: "asyncio.Task[None]",
        disconnected: asyncio.Event,
    ) -> None:
        self._chunks = chunks
        self._task = task
        self._disconnected = disconnected

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            item = await self._chunks.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    async def aclose(self) -> None:
        # The client is done with the response: the app's next `receive()` is a disconnect.
        self._disconnected.set()
        await asyncio.wait({self._task}, timeout=_CLOSE_TIMEOUT_S)
        if not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


class StreamingASGITransport(httpx.AsyncBaseTransport):
    """An in-process ASGI transport that streams the response body.

    The response is returned as soon as the app sends `http.response.start`; each body chunk
    reaches the client when the app sends it. The request body is delivered in one
    `http.request` message; after it, `receive()` blocks until the client closes the response
    and then answers `http.disconnect` (so `Request.is_disconnected()` is False meanwhile). An
    app exception before the response starts is raised; one in the middle of the body is raised
    while the body is read; one after the body is complete is ignored.
    """

    def __init__(self, app: ASGIApp, client: tuple[str, int] = ("127.0.0.1", 123)) -> None:
        self.app = app
        self.client = client

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": request.method,
            "headers": [(key.lower(), value) for key, value in request.headers.raw],
            "scheme": request.url.scheme,
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?")[0],
            "query_string": request.url.query,
            "server": (request.url.host, request.url.port),
            "client": self.client,
            "root_path": "",
        }
        chunks: asyncio.Queue[bytes | BaseException | None] = asyncio.Queue()
        started = asyncio.Event()
        disconnected = asyncio.Event()
        start: dict[str, Any] = {}
        body_sent = False

        async def receive() -> Message:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            if message["type"] == "http.response.start":
                start.update(message)
                started.set()
            elif message["type"] == "http.response.body":
                data = message.get("body", b"")
                if data:
                    await chunks.put(data)
                if not message.get("more_body", False):
                    await chunks.put(None)

        async def run() -> None:
            try:
                await self.app(scope, receive, send)
            except Exception as exc:
                await chunks.put(exc)
            finally:
                await chunks.put(None)
                started.set()

        task = asyncio.create_task(run())
        await started.wait()
        if not start:
            await task
            item = chunks.get_nowait()
            if isinstance(item, BaseException):
                raise item
            raise RuntimeError("ASGI app ended without starting a response")
        return httpx.Response(
            start["status"],
            headers=start.get("headers", []),
            stream=_QueueStream(chunks, task, disconnected),
            request=request,
        )


# --- SSE parsing -----------------------------------------------------------------------------


class SseReader:
    """Incremental `text/event-stream` parser: `feed(chunk)` returns the `(event, data)` pairs
    of the blocks completed by the chunk; an incomplete tail waits for the next chunk."""

    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, chunk: bytes) -> list[tuple[str, dict[str, Any]]]:
        self._buffer += chunk.replace(b"\r\n", b"\n")
        *blocks, self._buffer = self._buffer.split(b"\n\n")
        events: list[tuple[str, dict[str, Any]]] = []
        for block in blocks:
            name = "message"
            data: list[str] = []
            for line in block.decode("utf-8").split("\n"):
                field, _, value = line.partition(":")
                value = value.removeprefix(" ")
                if field == "event":
                    name = value
                elif field == "data":
                    data.append(value)
            if data:
                events.append((name, json.loads("\n".join(data))))
        return events


# --- measurements ----------------------------------------------------------------------------


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


def _payload(question: Question) -> dict[str, str]:
    return {"message": question.text, "ui_language": question.language}


async def time_chat(client: httpx.AsyncClient, question: Question) -> Timing:
    """`POST /chat`: the whole JSON answer at once, so TTFT = total."""
    started = time.perf_counter()
    response = await client.post("/chat", json=_payload(question))
    total = _elapsed_ms(started)
    ok = response.status_code == 200
    if ok:
        body = response.json()
        ok = body.get("status") == "completed" and bool(body.get("content"))
    print(
        f"[chat] [{question.language}] {question.text!r}: HTTP {response.status_code}, "
        f"total {total:.0f} ms{'' if ok else ' FAIL'}"
    )
    return Timing(language=question.language, ttft_ms=total, total_ms=total, ok=ok)


def stream_ok(events: list[tuple[str, dict[str, Any]]]) -> bool:
    """The stream follows CT-45/CT-50 and answered.

    - `start` is the first event and comes once (CT-45);
    - exactly one terminal event, a final `done{completed}` with a non-empty `content`;
    - without tools the concatenated tokens equal `content` (no tokens at all on the plain chat
      path); with tools they end with it, since text that precedes a tool call is streamed too
      while `done.content` is only the text of the last model call.
    """
    names = [name for name, _ in events]
    if not names or names[0] != "start" or names.count("start") != 1:
        return False
    terminal = [data for name, data in events if name in ("done", "error")]
    if len(terminal) != 1 or names[-1] != "done":
        return False
    done = terminal[0]
    content = str(done.get("content") or "")
    if done.get("status") != "completed" or not content:
        return False
    tokens = "".join(str(data.get("text", "")) for name, data in events if name == "token")
    if not tokens:
        return True
    return tokens.endswith(content) if "tool_start" in names else tokens == content


async def time_stream(client: httpx.AsyncClient, question: Question) -> Timing:
    """`POST /chat/stream`: TTFT = first `token` (or the `done` of a stream without tokens)
    parsed, total = end of the stream."""
    reader = SseReader()
    events: list[tuple[str, dict[str, Any]]] = []
    first_answer: float | None = None
    started = time.perf_counter()
    async with client.stream("POST", "/chat/stream", json=_payload(question)) as response:
        status = response.status_code
        async for chunk in response.aiter_bytes():
            for event in reader.feed(chunk):
                if event[0] in ("token", "done") and first_answer is None:
                    first_answer = _elapsed_ms(started)
                events.append(event)
    total = _elapsed_ms(started)
    ttft = first_answer if first_answer is not None else total
    ok = status == 200 and stream_ok(events)
    tokens = sum(1 for name, _ in events if name == "token")
    print(
        f"[stream] [{question.language}] {question.text!r}: HTTP {status}, "
        f"TTFT {ttft:.0f} ms, total {total:.0f} ms, {tokens} tokens, "
        f"events {events[0][0] if events else '-'}..{events[-1][0] if events else '-'}"
        f"{'' if ok else ' FAIL'}"
    )
    return Timing(language=question.language, ttft_ms=ttft, total_ms=total, ok=ok)


async def login(client: httpx.AsyncClient, username: str, password: str) -> None:
    response = await client.post("/auth/login", json={"username": username, "password": password})
    if response.status_code != 200:
        raise RuntimeError(f"Login of persona {username!r} failed: HTTP {response.status_code}")


async def measure(
    clients: dict[Lang, httpx.AsyncClient], questions: Sequence[Question], password: str
) -> tuple[list[Timing], list[Timing]]:
    usernames = {persona.language: persona.username for persona in PERSONAS}
    for language, client in clients.items():
        await login(client, usernames[language], password)
    before: list[Timing] = []
    after: list[Timing] = []
    for question in questions:
        client = clients[question.language]
        before.append(await time_chat(client, question))
        after.append(await time_stream(client, question))
    return before, after


def _clients(
    base_url: str, transport: httpx.AsyncBaseTransport | None
) -> dict[Lang, httpx.AsyncClient]:
    """One client (cookie jar) per persona language."""
    return {
        persona.language: httpx.AsyncClient(
            base_url=base_url, transport=transport, timeout=HTTP_TIMEOUT_S
        )
        for persona in PERSONAS
    }


async def run_demo(
    provider: str,
    settings: Settings,
    questions: Sequence[Question],
    *,
    base_url: str | None,
    delta_delay_ms: float,
) -> tuple[list[Timing], list[Timing]]:
    if base_url is not None:
        clients = _clients(base_url, None)
        try:
            return await measure(clients, questions, settings.persona_password)
        finally:
            for client in clients.values():
                await client.aclose()

    app = create_app()
    if provider == "fake":
        fake_settings = settings.model_copy(update={"llm_provider": "fake"})
        llm = InstrumentedLLMClient(
            PacedFakeLLM(FakeLLMClient(fake_script(questions)), delta_delay_ms / 1000),
            model=FAKE_MODEL,
            provider=FAKE_PROVIDER,
        )
        app.dependency_overrides[get_chat_service] = _fake_service(llm, fake_settings)
    clients = _clients(IN_PROCESS_URL, StreamingASGITransport(app))
    try:
        await seed_demo_principals(settings)
        return await measure(clients, questions, settings.persona_password)
    finally:
        for client in clients.values():
            await client.aclose()
        # No lifespan runs in process: release what the app would close at shutdown.
        await close_embedder()
        await dispose_engine()


# --- report ----------------------------------------------------------------------------------


def _p50_p95(values: Sequence[float]) -> str:
    return f"p50 {percentile(values, 50):.0f} ms, p95 {percentile(values, 95):.0f} ms"


def _count(timings: Sequence[Timing]) -> str:
    return f"(n={len(timings)}, ok {sum(t.ok for t in timings)}/{len(timings)})"


def before_cell(timings: Sequence[Timing]) -> str:
    totals = [t.total_ms for t in timings]
    return f"POST /chat (JSON): TTFT = total, {_p50_p95(totals)} {_count(timings)}"


def after_cell(timings: Sequence[Timing]) -> str:
    ttft = _p50_p95([t.ttft_ms for t in timings])
    total = _p50_p95([t.total_ms for t in timings])
    return f"POST /chat/stream (SSE): TTFT {ttft}; total {total} {_count(timings)}"


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {number}")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = demo_parser("Phase 09: TTFT and total time, POST /chat vs POST /chat/stream")
    parser.set_defaults(results=None)
    parser.add_argument("--n", type=_positive_int, default=DEFAULT_N)
    parser.add_argument("--base-url", dest="base_url", default=None)
    parser.add_argument(
        "--delta-delay-ms", dest="delta_delay_ms", type=float, default=DEFAULT_DELTA_DELAY_MS
    )
    return parser


def _provider_note(args: argparse.Namespace, settings: Settings) -> str:
    if args.provider == "fake":
        return (
            f"provider=fake (scripted answers paced at {args.delta_delay_ms:g} ms per word "
            "delta, retrieval off)"
        )
    if args.base_url is not None:
        return f"provider of the server at {args.base_url}"
    note = f"provider={settings.llm_provider}"
    if settings.llm_provider in _NO_STREAMING_PROVIDERS:
        note += " (no streaming: the answer arrives whole, TTFT about the total)"
    return note


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.base_url is not None and args.provider == "fake":
        parser.error("--base-url measures a running API; it cannot use --provider fake")
    if args.results is None:
        # A fake run never replaces the real `| 09 |` row (same rule as demos 03 to 08).
        fake_results = Path(tempfile.gettempdir()) / "atlas-demo09" / "results.md"
        args.results = DEFAULT_RESULTS_PATH if args.provider == "real" else fake_results

    settings = get_settings()
    questions = questions_for(args.n)
    before, after = asyncio.run(
        run_demo(
            args.provider,
            settings,
            questions,
            base_url=args.base_url,
            delta_delay_ms=args.delta_delay_ms,
        )
    )
    client = f"httpx over {args.base_url}" if args.base_url else "in-process httpx over the app"
    record_result(
        PHASE,
        f"TTFT and total time, {len(questions)} chat questions (pt/en): "
        "POST /chat vs POST /chat/stream",
        before_cell(before),
        after_cell(after),
        notes=(
            "Before: the JSON answer arrives whole, so TTFT = total; After: TTFT = first "
            "event: token parsed by the client, total = end of the stream; each question sent "
            "to both endpoints back to back, a new conversation each, personas alex (en) and "
            f"bia (pt) logged in via /auth/login; {client}; {_provider_note(args, settings)}; "
            "claude_cli and codex_cli do not stream (fallback to complete()), so with them "
            "TTFT is about the total time"
        ),
        results_path=args.results,
    )
    print(f"Recorded phase {PHASE:02d} in {args.results}")
    # Exit 1 when an answer of either endpoint failed.
    return 0 if all(t.ok for t in [*before, *after]) else 1


if __name__ == "__main__":
    sys.exit(main())
