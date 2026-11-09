"""Phase 08 demo: valid JSON rate of the triage, prompt-only vs native schema (TRI-04, DOD-02).

1. **Tickets.** `demos/data/phase08_inputs.jsonl` holds 24 realistic Assinaflow support tickets,
   12 in pt and 12 in en, two per category of `TriageResult` and language, each with the
   category a human would give it (used only for the agreement note, never sent to the model).
2. **Calls.** For each mode, `--n` (default 200) calls of `TriageService.classify(text, mode)`
   cycling through the tickets, straight on the service (no HTTP, no database):
   - `prompt_only` (**Before**): only the instructions of `prompts/triage.md`, no
     `response_schema`;
   - `schema` (**After**): the request carries `TriageResult.model_json_schema()` and the
     provider enforces it natively (Anthropic `output_config.format`, `claude -p --json-schema`,
     `codex exec --output-schema`).
3. **Metric.** Both modes run with `triage_max_repairs=0`, so every call is exactly one model
   answer and "valid" means the **first answer** passed `TriageResult` validation (the extraction
   of `extract_json` still applies: a code fence or text around the object is tolerated, as in
   production). With the default repairs (`ATLAS_TRIAGE_MAX_REPAIRS`, 2) almost every call would
   end valid in both modes and the comparison would show nothing. A provider failure
   (`LLMError`) is counted apart as an error and left out of the rate: rate = valid / answered.
4. **Repair run.** Unless `--no-repair-run`, `prompt_only` runs once more with the default
   repairs (`--repairs`, default `settings.triage_max_repairs`) and the Before cell adds the final
   rate and how many calls the repairs saved. `schema` is not rerun: its first answers are the
   point of the phase.
5. **Results.** The `| 08 |` row of `docs/results.md`: Before = `prompt_only`, After = `schema`,
   Notes = provider, models, tickets, errors and category agreement among valid answers.
   A mode (the repair run included) with no answer at all, or with provider errors on more
   than half of its calls, measures an outage, not the model: the demo prints why on stderr,
   records nothing and exits 3. A rate with no answers behind it is shown as `n/a`.

`--provider fake` runs offline with `FakeTriageLLM`: in `prompt_only` every 4th first answer of
each language is broken (rotating defects: an English enum value, a missing field, truncated
JSON and an extra field), the others are valid (every 3rd wrapped in a code fence, which the
extraction tolerates);
in `schema` and after a repair request it always answers valid JSON. Its rates are a
demonstration of the mechanism (80% / 100% on 10 calls, 75% / 100% on 200), not a measurement.
A fake run writes under `<tmp>/atlas-demo08/` unless `--results` is given, so it never replaces
the real `| 08 |` row. `real` uses the configured provider (`ATLAS_LLM_PROVIDER`: `anthropic`,
`claude_cli` or `codex_cli`) and reports what the model really answered.

Usage:
    uv run python -m demos.demo_08_triage --provider fake --n 10 --results /tmp/results.md
    uv run python -m demos.demo_08_triage --provider real   # writes docs/results.md
"""

import argparse
import asyncio
import json
import sys
import tempfile
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic import ValidationError

from api.config import Settings, get_settings
from api.errors import AtlasError
from api.llm.client import (
    LLMClient,
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    Usage,
)
from api.llm.fake import FAKE_MODEL, FAKE_PROVIDER, split_deltas
from api.llm.instrumented import InstrumentedLLMClient
from api.messages import Lang
from api.triage.schemas import Categoria
from api.triage.service import TriageMode, TriageService, wrap_ticket
from demos.common import DEFAULT_RESULTS_PATH, build_demo_llm, demo_parser, record_result

PHASE = 8
METRIC = "Triage valid JSON rate on the first answer (no repair), prompt-only vs native schema"
INPUTS_PATH = Path(__file__).resolve().parent / "data" / "phase08_inputs.jsonl"
DEFAULT_N = 200
DEFAULT_CONCURRENCY = 4
LANGUAGES: tuple[Lang, ...] = ("en", "pt")
CATEGORIES: tuple[str, ...] = get_args(Categoria)
# `FakeTriageLLM`: every `FAKE_INVALID_EVERY`-th first answer of `prompt_only` is broken.
FAKE_INVALID_EVERY = 4
FAKE_FENCE_EVERY = 3
# A mode with provider errors on more than this share of its calls is not recorded.
MAX_ERROR_SHARE = 0.5

Outcome = Literal["valid", "invalid", "error"]


@dataclass(frozen=True)
class TriageTicket:
    id: str
    language: Lang
    categoria: str
    text: str


def load_tickets(path: Path = INPUTS_PATH) -> list[TriageTicket]:
    """Tickets of the JSONL file, one per non-blank line. Raises `ValueError` on bad data."""
    tickets: list[TriageTicket] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        raw = json.loads(line)
        ticket_id, text, language = raw.get("id"), raw.get("text"), raw.get("language")
        categoria = raw.get("categoria")
        valid = (
            isinstance(ticket_id, str)
            and ticket_id.strip()
            and isinstance(text, str)
            and text.strip()
            and language in LANGUAGES
            and categoria in CATEGORIES
        )
        if not valid:
            raise ValueError(f"{path}:{line_number}: invalid triage ticket {raw!r}")
        tickets.append(
            TriageTicket(
                id=ticket_id,
                language="pt" if language == "pt" else "en",
                categoria=categoria,
                text=text,
            )
        )
    return tickets


# --- Fake provider ----------------------------------------------------------------------------

_FAKE_PRIORITY: dict[str, str] = {
    "pedido": "media",
    "assinatura": "baixa",
    "reembolso": "alta",
    "entrega": "media",
    "conta": "alta",
    "outro": "baixa",
}
_FAKE_SUMMARY: dict[Lang, str] = {
    "en": "Customer asks for help with this {categoria} request.",
    "pt": "Cliente pede ajuda com esta solicitação de {categoria}.",
}


def _fake_answer(ticket: TriageTicket | None) -> dict[str, Any]:
    categoria = ticket.categoria if ticket else "outro"
    language: Lang = ticket.language if ticket else "en"
    return {
        "categoria": categoria,
        "prioridade": _FAKE_PRIORITY[categoria],
        "sentimento": "neutro",
        "precisa_humano": categoria in ("reembolso", "conta"),
        "resumo": _FAKE_SUMMARY[language].format(categoria=categoria),
    }


def _broken_answer(answer: dict[str, Any], defect: int) -> str:
    """One of the defects a model shows without an enforced schema."""
    if defect == 0:  # translated enum value
        return json.dumps({**answer, "categoria": "refund"}, ensure_ascii=False)
    if defect == 1:  # missing field
        return json.dumps({k: v for k, v in answer.items() if k != "resumo"}, ensure_ascii=False)
    if defect == 2:  # truncated answer
        text = json.dumps(answer, ensure_ascii=False)
        return text[: len(text) // 2]
    return json.dumps({**answer, "confidence": 0.9}, ensure_ascii=False)  # extra field


class FakeTriageLLM:
    """Offline triage model for `--provider fake` and the tests (CT-6 `LLMClient`).

    Finds the ticket by the wrapped `user` message. In `prompt_only` (no `response_schema`)
    every `invalid_every`-th first answer **of each language** is broken, with rotating defects
    (counting per language spreads the failures over pt and en, since the tickets alternate);
    a repair request (more than one message) and every `schema` request get valid JSON.
    Records every request.
    """

    def __init__(
        self, tickets: Sequence[TriageTicket], invalid_every: int = FAKE_INVALID_EVERY
    ) -> None:
        self._by_message = {wrap_ticket(ticket.text): ticket for ticket in tickets}
        self._invalid_every = invalid_every
        self._first_answers: dict[Lang, int] = {"en": 0, "pt": 0}
        self.requests: list[LLMRequest] = []

    def _answer(self, request: LLMRequest) -> str:
        self.requests.append(request)
        ticket = self._by_message.get(request.messages[0].content) if request.messages else None
        answer = _fake_answer(ticket)
        if request.response_schema is not None or len(request.messages) > 1:
            return json.dumps(answer, ensure_ascii=False)
        language: Lang = ticket.language if ticket else "en"
        self._first_answers[language] += 1
        count = self._first_answers[language]
        if count % self._invalid_every == 0:
            return _broken_answer(answer, (count // self._invalid_every - 1) % 4)
        text = json.dumps(answer, ensure_ascii=False)
        if count % FAKE_FENCE_EVERY == 0:
            return f"Here is the triage:\n```json\n{text}\n```"
        return text

    async def complete(self, request: LLMRequest) -> LLMResponse:
        text = self._answer(request)
        return LLMResponse(
            text=text,
            tool_calls=[],
            usage=Usage(input_tokens=len(request.messages[0].content) // 4, output_tokens=40),
            model=FAKE_MODEL,
            provider=FAKE_PROVIDER,
            stop_reason="end_turn",
        )

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        response = await self.complete(request)
        for chunk in split_deltas(response.text):
            yield LLMStreamEvent(kind="text_delta", text=chunk)
        yield LLMStreamEvent(kind="usage", usage=response.usage)
        yield LLMStreamEvent(kind="stop", stop_reason=response.stop_reason)


# --- Run --------------------------------------------------------------------------------------


class _CallCounter:
    """Counts the model calls of one `classify` (1 + repairs) and keeps the model name."""

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm
        self.calls = 0
        self.model: str | None = None

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        response = await self._llm.complete(request)
        self.model = response.model
        return response

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        self.calls += 1
        return self._llm.stream(request)


@dataclass(frozen=True)
class CallResult:
    ticket_id: str
    language: Lang
    outcome: Outcome
    calls: int
    category_agrees: bool | None = None
    model: str | None = None


@dataclass(frozen=True)
class ModeSummary:
    mode: TriageMode
    max_repairs: int
    results: list[CallResult]

    def _count(self, outcome: Outcome) -> int:
        return sum(result.outcome == outcome for result in self.results)

    @property
    def valid(self) -> int:
        return self._count("valid")

    @property
    def invalid(self) -> int:
        return self._count("invalid")

    @property
    def errors(self) -> int:
        return self._count("error")

    @property
    def answered(self) -> int:
        return self.valid + self.invalid

    @property
    def rate(self) -> float:
        """Valid / answered; provider errors are left out."""
        return self.valid / self.answered if self.answered else 0.0

    @property
    def repaired(self) -> int:
        """Valid calls that needed at least one repair request."""
        return sum(r.outcome == "valid" and r.calls > 1 for r in self.results)

    @property
    def category_agreement(self) -> float:
        agreed = [r.category_agrees for r in self.results if r.category_agrees is not None]
        return sum(agreed) / len(agreed) if agreed else 0.0

    @property
    def models(self) -> list[str]:
        return sorted({r.model for r in self.results if r.model})

    def by_language(self) -> dict[Lang, tuple[int, int]]:
        """Valid and answered calls per language."""
        counts: dict[Lang, tuple[int, int]] = {}
        for lang in LANGUAGES:
            chosen = [r for r in self.results if r.language == lang and r.outcome != "error"]
            if chosen:
                counts[lang] = (sum(r.outcome == "valid" for r in chosen), len(chosen))
        return counts


def _outcome(error: AtlasError) -> Outcome:
    """`TRIAGE_UNAVAILABLE` caused by validation is invalid JSON; by `LLMError`, an error."""
    return "invalid" if isinstance(error.__cause__, ValidationError) else "error"


async def _classify_one(
    llm: LLMClient, settings: Settings, ticket: TriageTicket, mode: TriageMode
) -> CallResult:
    counter = _CallCounter(llm)
    try:
        result = await TriageService(counter, settings).classify(ticket.text, mode)
    except AtlasError as error:
        if not isinstance(error.__cause__, ValidationError | LLMError):
            raise
        return CallResult(ticket.id, ticket.language, _outcome(error), counter.calls)
    return CallResult(
        ticket.id,
        ticket.language,
        "valid",
        counter.calls,
        category_agrees=result.categoria == ticket.categoria,
        model=counter.model,
    )


async def run_mode(
    llm: LLMClient,
    settings: Settings,
    tickets: Sequence[TriageTicket],
    mode: TriageMode,
    n: int,
    max_repairs: int = 0,
    concurrency: int = 1,
) -> ModeSummary:
    """`n` triage calls in `mode`, cycling `tickets`, with `max_repairs` repair requests each."""
    if not tickets:
        raise ValueError("no triage tickets to run")
    run_settings = settings.model_copy(update={"triage_max_repairs": max_repairs})
    semaphore = asyncio.Semaphore(concurrency)

    async def one(index: int) -> CallResult:
        async with semaphore:
            return await _classify_one(llm, run_settings, tickets[index % len(tickets)], mode)

    results = await asyncio.gather(*(one(index) for index in range(n)))
    return ModeSummary(mode=mode, max_repairs=max_repairs, results=list(results))


def unusable_reason(summary: ModeSummary) -> str | None:
    """Why a mode's numbers must not be recorded, or `None` when they can be.

    No answer at all, or provider errors on more than `MAX_ERROR_SHARE` of the calls: the
    rate would describe the provider's outage, not the model's JSON.
    """
    calls = len(summary.results)
    if summary.answered == 0 or summary.errors > calls * MAX_ERROR_SHARE:
        return (
            f"{summary.mode} (repairs={summary.max_repairs}): provider errors on "
            f"{summary.errors}/{calls} calls, {summary.answered} answered"
        )
    return None


# --- Report -----------------------------------------------------------------------------------


def _percent(value: float, base: int, digits: int = 1) -> str:
    """`value` as a percentage, or `n/a` when there is nothing to measure (`base == 0`)."""
    return f"{value:.{digits}%}" if base else "n/a"


def _rate_cell(summary: ModeSummary) -> str:
    per_language = ", ".join(
        f"{lang} {valid}/{answered}" for lang, (valid, answered) in summary.by_language().items()
    )
    detail = f"{summary.valid}/{summary.answered}" + (f"; {per_language}" if per_language else "")
    return f"{summary.mode}: {_percent(summary.rate, summary.answered)} ({detail})"


def _before_cell(first: ModeSummary, repaired: ModeSummary | None) -> str:
    cell = _rate_cell(first)
    if repaired is None:
        return cell
    return (
        f"{cell}; with {repaired.max_repairs} repairs: "
        f"{_percent(repaired.rate, repaired.answered)} "
        f"({repaired.valid}/{repaired.answered}, {repaired.repaired} repaired)"
    )


def _notes(
    provider: str,
    settings: Settings,
    tickets: Sequence[TriageTicket],
    summaries: Sequence[ModeSummary],
    n: int,
) -> str:
    counts = {lang: sum(t.language == lang for t in tickets) for lang in LANGUAGES}
    if provider == "fake":
        source = "provider=fake (scripted: every 4th prompt_only first answer per language broken)"
    else:
        models = sorted({m for s in summaries for m in s.models}) or ["unknown"]
        source = f"provider=real ({settings.llm_provider}, model {', '.join(models)})"
    errors = ", ".join(f"{s.mode} {s.errors}" for s in summaries)
    agreement = ", ".join(
        f"{s.mode} {_percent(s.category_agreement, s.valid, digits=0)}" for s in summaries
    )
    return (
        f"{source}; {n} calls per mode cycling {len(tickets)} tickets "
        f"({counts['pt']} pt + {counts['en']} en); triage_max_repairs=0 for the measured rate "
        f"(first answer only, rate = valid / answered); provider errors: {errors}; "
        f"category agreement among valid answers: {agreement}"
    )


# --- Orchestration ----------------------------------------------------------------------------


def _positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}")
    return number


def _non_negative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError(f"expected a non-negative integer, got {value!r}")
    return number


def build_triage_llm(provider: str, tickets: Sequence[TriageTicket]) -> LLMClient:
    """`fake`: instrumented `FakeTriageLLM`; `real`: the configured client."""
    if provider == "fake":
        return InstrumentedLLMClient(
            FakeTriageLLM(tickets), model=FAKE_MODEL, provider=FAKE_PROVIDER
        )
    return build_demo_llm(provider)


async def _run_all(
    llm: LLMClient,
    settings: Settings,
    tickets: Sequence[TriageTicket],
    n: int,
    concurrency: int,
    repairs: int | None,
) -> tuple[ModeSummary, ModeSummary, ModeSummary | None]:
    prompt_only = await run_mode(llm, settings, tickets, "prompt_only", n, 0, concurrency)
    schema = await run_mode(llm, settings, tickets, "schema", n, 0, concurrency)
    repaired = None
    if repairs is not None:
        repaired = await run_mode(llm, settings, tickets, "prompt_only", n, repairs, concurrency)
    return prompt_only, schema, repaired


def _print_summary(summary: ModeSummary) -> None:
    print(
        f"[{summary.mode}] repairs={summary.max_repairs} valid={summary.valid} "
        f"invalid={summary.invalid} errors={summary.errors} "
        f"rate={_percent(summary.rate, summary.answered)} "
        f"repaired={summary.repaired}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = demo_parser("Phase 08: valid triage JSON, prompt-only vs native schema")
    parser.add_argument("--n", type=_positive, default=DEFAULT_N, help="calls per mode")
    parser.add_argument("--inputs", type=Path, default=INPUTS_PATH)
    parser.add_argument("--concurrency", type=_positive, default=DEFAULT_CONCURRENCY)
    parser.add_argument(
        "--repairs",
        type=_non_negative,
        default=None,
        help="repairs of the prompt_only repair run (default: ATLAS_TRIAGE_MAX_REPAIRS)",
    )
    parser.add_argument(
        "--no-repair-run",
        dest="repair_run",
        action="store_false",
        help="skip the prompt_only run with repairs",
    )
    parser.set_defaults(results=None)
    args = parser.parse_args(argv)
    if args.results is None:
        # A fake run never replaces the real `| 08 |` row (same rule as demo_04).
        fake_results = Path(tempfile.gettempdir()) / "atlas-demo08" / "results.md"
        args.results = fake_results if args.provider == "fake" else DEFAULT_RESULTS_PATH

    settings = get_settings()
    if args.provider == "real" and settings.llm_provider == "fake":
        print(
            "error: --provider real needs ATLAS_LLM_PROVIDER set to a real provider "
            "(anthropic, claude_cli or codex_cli); use --provider fake to run offline",
            file=sys.stderr,
        )
        return 2
    tickets = load_tickets(args.inputs)
    llm = build_triage_llm(args.provider, tickets)
    repairs = None
    if args.repair_run:
        repairs = settings.triage_max_repairs if args.repairs is None else args.repairs
    prompt_only, schema, repaired = asyncio.run(
        _run_all(llm, settings, tickets, args.n, args.concurrency, repairs)
    )
    for summary in (prompt_only, schema, repaired):
        if summary is not None:
            _print_summary(summary)

    before, after = _before_cell(prompt_only, repaired), _rate_cell(schema)
    print(before)
    print(after)
    reasons = [
        reason
        for summary in (prompt_only, schema, repaired)
        if summary is not None and (reason := unusable_reason(summary)) is not None
    ]
    if reasons:
        print(
            f"error: phase {PHASE:02d} not recorded, too many provider errors: "
            + "; ".join(reasons),
            file=sys.stderr,
        )
        return 3
    record_result(
        PHASE,
        METRIC,
        before,
        after,
        notes=_notes(args.provider, settings, tickets, (prompt_only, schema), args.n),
        results_path=args.results,
    )
    print(f"Recorded phase {PHASE:02d} in {args.results}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
