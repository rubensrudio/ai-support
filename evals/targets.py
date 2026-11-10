"""In-process eval targets (EVAL-01, EVAL-02, TASK-126). No HTTP server.

- `ChatTarget(settings)`: runs the case message through `ChatService` with a persona seeded in an
  ephemeral database (personas + commerce seed), confirming destructive tools automatically,
  and returns the output dict of `evals.graders` (text, status, language, tool trajectory from
  `tool_start` events, citation doc_keys, error).
- `TriageTarget(settings)`: runs `TriageService.classify` and returns the output dict with
  `triage` set (the parsed result as a dict, or None when it failed).
- `AutoTarget(settings)`: dispatches each case to one of the two (json_valid / triage-shaped
  input -> triage, everything else -> chat), for a single run over every dataset.
All implement `evals.runner.EvalTarget` (`async run(case) -> dict`).

Chat target details:
- Database: the process engine (`api.db.engine`, i.e. `ATLAS_DATABASE_URL`), assumed migrated.
  On the first case the personas are seeded (idempotent); before every case the commerce data
  is reset (`seed_commerce`, as `python -m api.seed` and demos 06/07 do), so a case never sees
  the effects of the previous one. Order numbers change on every reset: `{paid_order_id}` and
  the other `demos.demo_07_agent.ORDER_REFS` placeholders in the message (and in the history)
  are resolved against the persona's fresh orders; other `{...}` text is left as written.
- Persona: `input.persona` (`alex`, `bia`), else the persona of the case language (en -> alex,
  pt -> bia). An unknown persona is a case error.
- Request: `input.message` with `ui_language` = `input.ui_language` or the case language, sent
  as the persona's principal, each request in its own unit of work and trace id, as the HTTP
  route does. `input.history` (a list of texts) is replayed first as earlier user turns of the
  same conversation, each answered by the model; those turns are not part of the trajectory.
- Confirmations: with a truthy `input.confirm` (`true` or `"all"`) every destructive call is
  confirmed (`resolve_confirmation(..., "confirm")`, the method behind
  `POST /chat/confirmations/{id}`) until the answer is no longer `awaiting_confirmation`
  (at most `MAX_CONFIRMATIONS`). Without it the case stops at `awaiting_confirmation`: nothing
  is refused, the status is what the grader sees.
- Output: `text` = the final answer, `status` / `language` of the final response,
  `trajectory` = tools of every `tool_start` of the case message and its confirmations, plus
  the tool of a still pending confirmation (a suspended destructive call has no `tool_start`
  until it is confirmed, and the model did call it: phase 06 expects `["cancel_order"]` with
  `awaiting_confirmation`),
  `citations` = doc_keys of the final response, `chunks` = texts of the chunks retrieved for the
  case message (for the faithfulness judge), `triage` = None. Any exception (an `AtlasError`
  such as `VALIDATION_ERROR`, a database error) becomes `error` = `"<Type>: <message>"` with
  everything collected so far, so the graders score it 0 instead of aborting the run. A
  `degraded` answer (every provider failed, RES-06) keeps its text and status and also sets
  `error` (`DEGRADED_ERROR`): it is a provider outage, not a quality score (EVAL-90).
- Other input fields of the phase datasets (`endpoint`, `layout`, `recall`, `attack`, `set`,
  `fake_reply`, `model_answer`, `document`, `providers`) describe how the phase tests ran
  offline; they do not apply in process and are ignored.
- LLM: `build_llm_client(settings)` built on the first case (or the `llm` given, for tests),
  shared by every case; the guardrail client is `ATLAS_GUARDRAIL_MODEL`'s when set (as
  `get_chat_service` does); retrieval uses the default `Retriever` (with the embedder only in
  a vector mode). `aclose()` closes the embedder and the engine's pooled connections; call it
  in the event loop that ran the cases (the CLI does).
"""

import re
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any, cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from api.agent.events import AgentEvent, ConfirmationPrompt, ToolStartEvent
from api.auth.deps import CurrentCustomer
from api.chat.schemas import ChatRequest, ChatResponse
from api.chat.service import ChatService
from api.config import Settings
from api.db.engine import dispose_engine, get_sessionmaker, session_scope
from api.errors import AtlasError
from api.llm.client import LLMClient
from api.llm.factory import build_llm_client
from api.messages import Lang
from api.observability.logging import bind_trace_id, new_trace_id, reset_trace_id
from api.rag.embeddings import EmbeddingClient, OpenAIEmbeddingClient, build_embedder
from api.rag.retriever import RetrievedChunk, Retriever, is_vector_mode
from api.seed.commerce import seed_commerce
from api.seed.personas import PERSONAS
from api.tools.catalog import build_default_registry
from api.tools.registry import ToolRegistry
from api.triage.service import TriageMode, TriageService
from demos.demo_01_stateless import seed_demo_principals
from demos.demo_07_agent import ORDER_REFS, order_refs
from evals.dataset import EvalCase

# Safety net for real providers: never confirm more than this per case.
MAX_CONFIRMATIONS = 8
TRIAGE_MODES: tuple[TriageMode, ...] = ("prompt_only", "schema")
STATUS_OK = "completed"
STATUS_ERROR = "error"
STATUS_DEGRADED = "degraded"
# A degraded answer means every provider failed (RES-06): an execution error, not a score.
DEGRADED_ERROR = "degraded response: every LLM provider failed (status 'degraded')"
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_CONFIRM_ALL = "all"


def _output(language: str) -> dict[str, Any]:
    """The output dict of `evals.graders` with every key empty."""
    return {
        "text": "",
        "status": "",
        "language": language,
        "trajectory": [],
        "citations": [],
        "chunks": [],
        "triage": None,
        "error": None,
    }


def _error_text(error: Exception) -> str:
    if isinstance(error, AtlasError):
        return f"AtlasError: {error.code.value} ({error.http_status})"
    return f"{type(error).__name__}: {error}"


def resolve_placeholders(text: str, refs: dict[str, int]) -> str:
    """Replace the `{<ref>}` order placeholders with the seeded order numbers; other braces stay."""
    return _PLACEHOLDER.sub(
        lambda match: str(refs[match.group(1)]) if match.group(1) in refs else match.group(0),
        text,
    )


def _needs_refs(texts: list[str]) -> bool:
    return any(
        match.group(1) in ORDER_REFS for text in texts for match in _PLACEHOLDER.finditer(text)
    )


def _wants_confirm(value: object) -> bool:
    return value is True or value == _CONFIRM_ALL


def _chat_input(case: EvalCase) -> tuple[str, list[str], Lang]:
    """`(message, history texts, reply language)` of a chat case; raises without a message."""
    message = case.input.get("message")
    if not isinstance(message, str):
        raise ValueError(f"case {case.id!r} has no input.message")
    raw_history = case.input.get("history") or []
    history = [str(turn) for turn in raw_history] if isinstance(raw_history, list) else []
    ui_language = case.input.get("ui_language")
    language = cast(Lang, ui_language if ui_language in ("pt", "en") else case.language)
    return message, history, language


@contextmanager
def _request_trace() -> Iterator[str]:
    """A fresh trace id for one simulated HTTP request, as the middleware binds it."""
    trace_id = new_trace_id()
    token = bind_trace_id(trace_id)
    try:
        yield trace_id
    finally:
        reset_trace_id(token)


async def _tap(events: AsyncIterator[AgentEvent], started: list[str]) -> AsyncIterator[AgentEvent]:
    """Pass the events through, recording the tool of every `tool_start`."""
    async for event in events:
        if isinstance(event, ToolStartEvent):
            started.append(event.tool)
        yield event


class RecordingRetriever(Retriever):
    """The default retriever, keeping the chunks of the last search for the judge."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.last: list[RetrievedChunk] = []

    async def retrieve(self, *args: Any, **kwargs: Any) -> list[RetrievedChunk]:
        chunks = await super().retrieve(*args, **kwargs)
        self.last = list(chunks)
        return chunks


class ChatTarget:
    def __init__(
        self,
        settings: Settings,
        *,
        llm: LLMClient | None = None,
        guardrail_llm: LLMClient | None = None,
    ) -> None:
        self.settings = settings
        self._llm = llm
        self._guardrail_llm = guardrail_llm
        self._embedder: EmbeddingClient | None = None
        self._retriever: RecordingRetriever | None = None
        self._registry: ToolRegistry | None = None
        self._principals: dict[str, CurrentCustomer] | None = None

    async def run(self, case: EvalCase) -> dict[str, Any]:
        output = _output(case.language)
        try:
            await self._run(case, output)
        except Exception as error:  # one broken case is a 0 score, never an aborted run
            output["error"] = _error_text(error)
        return output

    async def aclose(self) -> None:
        """Close the embedder's HTTP client and the engine's pooled connections."""
        if isinstance(self._embedder, OpenAIEmbeddingClient):
            await self._embedder.aclose()
        self._embedder = None
        self._retriever = None
        await dispose_engine()

    # --- setup -------------------------------------------------------------------------------

    def _chat_llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = build_llm_client(self.settings)
            if self.settings.guardrail_model and self._guardrail_llm is None:
                self._guardrail_llm = build_llm_client(
                    self.settings.model_copy(
                        update={"primary_model": self.settings.guardrail_model}
                    )
                )
        return self._llm

    def _service_parts(self) -> tuple[RecordingRetriever, ToolRegistry]:
        if self._retriever is None:
            if is_vector_mode(self.settings.retrieval_mode):
                self._embedder = build_embedder(self.settings)
            self._retriever = RecordingRetriever(get_sessionmaker(), self._embedder, self.settings)
        if self._registry is None:
            self._registry = build_default_registry()
        return self._retriever, self._registry

    async def _principal(self, case: EvalCase) -> CurrentCustomer:
        if self._principals is None:
            by_language = await seed_demo_principals(self.settings)
            self._principals = {
                persona.username: by_language[persona.language] for persona in PERSONAS
            }
        default = next(p.username for p in PERSONAS if p.language == case.language)
        persona = str(case.input.get("persona") or default)
        principal = self._principals.get(persona)
        if principal is None:
            raise LookupError(f"unknown persona {persona!r} (known: {sorted(self._principals)})")
        return principal

    def _service(self, session: AsyncSession) -> ChatService:
        retriever, registry = self._service_parts()
        return ChatService(
            self._chat_llm(),
            self.settings,
            session,
            retriever=retriever,
            registry=registry,
            guardrail_llm=self._guardrail_llm,
        )

    # --- requests ----------------------------------------------------------------------------

    async def _send(
        self, principal: CurrentCustomer, request: ChatRequest, started: list[str]
    ) -> ChatResponse:
        """`POST /chat`: one message in its own unit of work and trace."""
        with _request_trace():
            async with session_scope() as session:
                service = self._service(session)
                events = service.handle_message(request, principal)
                return await service.collect(_tap(events, started))

    async def _confirm(
        self, principal: CurrentCustomer, confirmation: ConfirmationPrompt, started: list[str]
    ) -> ChatResponse:
        """`POST /chat/confirmations/{id}` with `{"decision": "confirm"}`."""
        with _request_trace():
            async with session_scope() as session:
                service = self._service(session)
                events = service.resolve_confirmation(confirmation.id, "confirm", principal)
                return await service.collect(_tap(events, started))

    async def _replay_history(
        self, principal: CurrentCustomer, history: list[str], refs: dict[str, int], language: Lang
    ) -> UUID | None:
        """Send the history texts as earlier user turns; returns the conversation id."""
        conversation_id: UUID | None = None
        for turn in history:
            previous = await self._send(
                principal,
                ChatRequest(
                    message=resolve_placeholders(turn, refs),
                    conversation_id=conversation_id,
                    ui_language=language,
                ),
                [],
            )
            conversation_id = previous.conversation_id
        return conversation_id

    async def _settle_confirmations(
        self,
        case: EvalCase,
        principal: CurrentCustomer,
        response: ChatResponse,
        started: list[str],
    ) -> ChatResponse:
        """Confirm every destructive call when the case asks for it (at most
        `MAX_CONFIRMATIONS`); a call left pending joins the trajectory."""
        confirmations = 0
        while (
            _wants_confirm(case.input.get("confirm"))
            and response.status == "awaiting_confirmation"
            and response.confirmation is not None
            and confirmations < MAX_CONFIRMATIONS
        ):
            confirmations += 1
            response = await self._confirm(principal, response.confirmation, started)

        if response.status == "awaiting_confirmation" and response.confirmation is not None:
            # A suspended destructive call has no `tool_start` until it is confirmed.
            started.append(response.confirmation.tool)
        return response

    async def _run(self, case: EvalCase, output: dict[str, Any]) -> None:
        message, history, language = _chat_input(case)

        principal = await self._principal(case)
        async with session_scope() as session:
            await seed_commerce(session)
        refs = await order_refs(principal) if _needs_refs([message, *history]) else {}
        conversation_id = await self._replay_history(principal, history, refs, language)

        retriever, _ = self._service_parts()
        retriever.last = []
        started: list[str] = output["trajectory"]
        request = ChatRequest(
            message=resolve_placeholders(message, refs),
            conversation_id=conversation_id,
            ui_language=language,
        )
        response = await self._send(principal, request, started)
        output["chunks"] = [chunk.content for chunk in retriever.last]
        response = await self._settle_confirmations(case, principal, response, started)

        output["text"] = response.content
        output["status"] = response.status
        output["language"] = response.language
        output["citations"] = [c.doc_key for c in response.citations if c.doc_key]
        if response.status == STATUS_DEGRADED:
            output["error"] = DEGRADED_ERROR


class TriageTarget:
    """`TriageService.classify(input.text, input.mode)`; `input.mode` other than `prompt_only` /
    `schema` (or absent) uses `settings.triage_mode`. `status` is `completed` with `triage` set,
    or `error` with `triage=None`. Only an infrastructure failure (provider error, unexpected
    exception) sets `error` (an execution error for the run, EVAL-90); an answer that stayed
    invalid after every repair is a quality result: `triage=None`, no `error`, so `json_valid`
    scores 0. `text` is the result as JSON."""

    def __init__(self, settings: Settings, *, llm: LLMClient | None = None) -> None:
        self.settings = settings
        self._llm = llm

    async def run(self, case: EvalCase) -> dict[str, Any]:
        output = _output(case.language)
        text = case.input.get("text")
        raw_mode = case.input.get("mode")
        mode = cast(TriageMode, raw_mode) if raw_mode in TRIAGE_MODES else None
        try:
            if not isinstance(text, str):
                raise ValueError(f"case {case.id!r} has no input.text")
            if self._llm is None:
                self._llm = build_llm_client(self.settings)
            result = await TriageService(self._llm, self.settings).classify(text, mode)
        except Exception as error:  # one broken case is a 0 score, never an aborted run
            output["status"] = STATUS_ERROR
            if _is_invalid_model_output(error):
                # Quality, not infrastructure (EVAL-90): the model kept answering invalid JSON
                # after every repair. `triage=None` makes `json_valid` score 0 without turning
                # the whole run into an execution error.
                output["triage"] = None
                return output
            output["error"] = _error_text(error)
            return output
        output["status"] = STATUS_OK
        output["triage"] = result.model_dump()
        output["text"] = result.model_dump_json()
        return output


def _is_invalid_model_output(error: BaseException) -> bool:
    """`TRIAGE_UNAVAILABLE` caused by an answer that never validated (not by the provider)."""
    cause = error.__cause__
    return isinstance(error, AtlasError) and isinstance(cause, ValidationError | ValueError)


def is_triage_case(case: EvalCase) -> bool:
    """A triage case: category `json_valid`, or an input with `text` and no `message`."""
    if case.category == "json_valid":
        return True
    return "text" in case.input and "message" not in case.input


class AutoTarget:
    """Dispatches each case: triage cases (`is_triage_case`) to `TriageTarget`, every other
    case to `ChatTarget`, so one run covers every chat and triage dataset. Both targets are
    built once (with `settings`) and shared by the cases; `aclose()` closes the chat target."""

    def __init__(
        self,
        settings: Settings,
        *,
        chat: ChatTarget | None = None,
        triage: TriageTarget | None = None,
    ) -> None:
        self.settings = settings
        self.chat = chat if chat is not None else ChatTarget(settings)
        self.triage = triage if triage is not None else TriageTarget(settings)

    async def run(self, case: EvalCase) -> dict[str, Any]:
        target = self.triage if is_triage_case(case) else self.chat
        return await target.run(case)

    async def aclose(self) -> None:
        await self.chat.aclose()
