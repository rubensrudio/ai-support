"""Triage service (CT-74, TRI-02, TRI-03, TRI-07, TRI-08).

`classify(text, mode=None)`: `mode` defaults to `settings.triage_mode`.
- `schema`: the request carries `response_schema = triage_output_schema()` (native structured
  output, TRI-02): the `TriageResult` schema without the keywords providers reject (`minLength`
  ...); the answer is still validated with `TriageResult`, so those rules keep holding.
- `prompt_only`: only the instructions of `prompts/triage.md`, no `response_schema` (the "before"
  of demo 08).
The answer is validated with `TriageResult`; on failure the call is repeated up to
`settings.triage_max_repairs` times (default 2), each new request including the validation error
(TRI-03). Exhausted -> `AtlasError(TRIAGE_UNAVAILABLE, 503)` and the log `triage_failed`
(TRI-07). The input text never goes to the logs.

Details:
- The ticket goes in one `user` message between `<ticket>` and `</ticket>`; the prompt treats it
  as data. Delimiter tags inside the ticket (any spacing, case or attributes) are neutralised so
  it cannot close the block early.
- The JSON object is extracted from the answer before validation: a Markdown code fence or text
  around the object is tolerated (mostly a `prompt_only` concern, applied in both modes).
- A repair request keeps the whole history: the previous assistant answer followed by a `user`
  message with a short summary of the validation errors (location, message, error type). The
  summary never includes the offending input, so the ticket text is not echoed back.
- `LLMError` (any provider failure, timeout included) fails the triage at once with the same
  503: retries and fallback for provider errors are the job of the resilience decorators.
"""

import re
from typing import Literal

from pydantic import ValidationError

from api.config import Settings
from api.errors import AtlasError, ErrorCode
from api.llm.client import ChatMessage, LLMClient, LLMError, LLMRequest, SystemBlock
from api.llm.fallback import AllProvidersFailed
from api.observability.logging import log_event
from api.prompts import load_prompt
from api.triage.schemas import TriageResult, triage_output_schema

TriageMode = Literal["prompt_only", "schema"]

PROMPT_NAME = "triage"
PURPOSE = "triage"
MAX_TOKENS = 512
MAX_REPORTED_ERRORS = 8
EMPTY_ANSWER_PLACEHOLDER = "(empty answer)"

_FENCE = re.compile(r"```[ \t]*(?:json)?[ \t]*\r?\n?(.*?)```", re.DOTALL | re.IGNORECASE)
_TICKET_TAG = re.compile(r"<\s*(/?)\s*ticket\b[^>]*>", re.IGNORECASE)


def wrap_ticket(text: str) -> str:
    """The `user` message: the ticket between delimiters, with inner tags neutralised."""
    safe = _TICKET_TAG.sub(lambda m: f"[{m.group(1)}ticket]", text)
    return f"<ticket>\n{safe}\n</ticket>"


def extract_json(answer: str) -> str:
    """The JSON object inside `answer`: fenced block first, else first `{` to last `}`."""
    fenced = _FENCE.search(answer)
    candidate = fenced.group(1) if fenced else answer
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end > start:
        return candidate[start : end + 1]
    return candidate.strip()


def summarize_validation_error(error: ValidationError) -> str:
    """Short list of the errors without the offending input values."""
    lines: list[str] = []
    details = error.errors(include_url=False, include_input=False, include_context=False)
    for detail in details[:MAX_REPORTED_ERRORS]:
        location = ".".join(str(part) for part in detail["loc"]) or "(root)"
        lines.append(f"- {location}: {detail['msg']} [{detail['type']}]")
    if len(details) > MAX_REPORTED_ERRORS:
        lines.append(f"- ... and {len(details) - MAX_REPORTED_ERRORS} more errors")
    return "\n".join(lines)


def repair_message(error: ValidationError) -> str:
    return (
        "Your previous answer was rejected by the validator with these errors:\n"
        f"{summarize_validation_error(error)}\n"
        "Answer again with only one JSON object with exactly the fields categoria, prioridade, "
        "sentimento, precisa_humano and resumo, using only the allowed values."
    )


class TriageService:
    def __init__(self, llm: LLMClient, settings: Settings) -> None:
        self.llm = llm
        self.settings = settings

    async def classify(self, text: str, mode: TriageMode | None = None) -> TriageResult:
        active: TriageMode = mode or self.settings.triage_mode
        max_calls = 1 + self.settings.triage_max_repairs
        system = [SystemBlock(text=load_prompt(PROMPT_NAME))]
        schema = triage_output_schema() if active == "schema" else None
        messages = [ChatMessage(role="user", content=wrap_ticket(text))]

        for attempt in range(1, max_calls + 1):
            request = LLMRequest(
                system=system,
                messages=list(messages),
                response_schema=schema,
                max_tokens=MAX_TOKENS,
                purpose=PURPOSE,
            )
            try:
                response = await self.llm.complete(request)
            except (LLMError, AllProvidersFailed) as exc:
                # `AllProvidersFailed`: the resilient chain (phase 12) exhausted every provider.
                self._fail(active, attempt, type(exc).__name__)
                raise AtlasError(ErrorCode.TRIAGE_UNAVAILABLE, 503) from exc
            try:
                result = TriageResult.model_validate_json(extract_json(response.text))
            except ValidationError as exc:
                if attempt == max_calls:
                    self._fail(active, attempt, type(exc).__name__)
                    raise AtlasError(ErrorCode.TRIAGE_UNAVAILABLE, 503) from exc
                log_event(
                    "triage_repair",
                    mode=active,
                    attempt=attempt,
                    error_count=exc.error_count(),
                )
                previous = response.text if response.text.strip() else EMPTY_ANSWER_PLACEHOLDER
                messages.append(ChatMessage(role="assistant", content=previous))
                messages.append(ChatMessage(role="user", content=repair_message(exc)))
                continue
            log_event("triage_classified", mode=active, attempts=attempt)
            return result

        raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover

    @staticmethod
    def _fail(mode: TriageMode, attempts: int, error_type: str) -> None:
        log_event("triage_failed", mode=mode, attempts=attempts, error_type=error_type)
