"""Phase 02 demo: conversation history and a bounded context window (CHAT-02, CHAT-07, DOD-02).

1. Repeats the scenario of `demo_01` (state the name, then ask for it) in pt and en, now
   inside one conversation: the 2nd message carries the `conversation_id` returned by the 1st,
   so the service sends the earlier turns and the answer recalls the name.
2. Runs one 50-turn conversation per history mode (`ATLAS_HISTORY_MODE`, DA-16): `full`
   sends every earlier message, `summarized` folds the oldest ones into a summary so the
   history never goes above `ATLAS_CONTEXT_BUDGET_TOKENS` (CHAT-06, CHAT-08). The last turn
   asks for the name stated in the first one.
3. Plots the history tokens of every chat call of both modes in `<out-dir>/phase02_tokens.png`
   and records the totals in the `| 02 |` row of `docs/results.md`.

"History tokens" of a call are counted locally (`api.llm.tokens`, DA-9) over what the service
sent besides its fixed system prompt: the extra system blocks (the summary) plus the message
window. Totals come from the `usage` each call returns, summarize calls included.

The demo needs the database (`ATLAS_DATABASE_URL`) and seeds the personas `alex` (en) and
`bia` (pt). `--provider fake` uses `ContextAwareFakeLLM`, an offline model whose answers
depend only on what it receives, so the recall is proved by the request, not by a script.

Usage:
    uv run python -m demos.demo_02_context_window --provider fake --results /tmp/results.md \
        --out-dir /tmp/img
    uv run python -m demos.demo_02_context_window --provider real   # writes docs/results.md
"""

import asyncio
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from api.chat.schemas import ChatRequest
from api.config import HistoryMode, Settings, get_settings
from api.db.engine import dispose_engine
from api.llm.client import LLMClient, LLMRequest, LLMResponse, LLMStreamEvent, Usage
from api.llm.fake import FAKE_MODEL, FAKE_PROVIDER
from api.llm.instrumented import InstrumentedLLMClient
from api.llm.streaming import stream_or_complete
from api.llm.tokens import count_messages, count_tokens
from demos.common import ROOT_DIR, build_demo_llm, parse_demo_args, record_result
from demos.demo_01_stateless import (
    DIALOGUES,
    NAME,
    DialogueResult,
    seed_demo_principals,
    send_message,
)

PHASE = 2
TURNS = 50
MODES: tuple[HistoryMode, ...] = ("full", "summarized")
CHART_NAME = "phase02_tokens.png"
METRIC = "Tokens in a 50-turn conversation (full vs summarized history)"

INTRODUCTION = f"Hi, my name is {NAME}. I have a few questions about my Atlas account today."
NAME_QUESTION = DIALOGUES["en"][1]
NAME_ANSWERS: dict[str, str] = {
    DIALOGUES["en"][1]: f"Your name is {NAME}.",
    DIALOGUES["pt"][1]: f"Seu nome é {NAME}.",
}
NAME_UNKNOWN: dict[str, str] = {
    DIALOGUES["en"][1]: "I'm sorry, I don't know your name. Could you tell me?",
    DIALOGUES["pt"][1]: "Desculpe, não sei o seu nome. Pode me dizer?",
}
SUPPORT_QUESTIONS = (
    "How do I change the email address on my account? I moved to a new provider last month.",
    "Where can I download the invoices for the last three months? I need them for my taxes.",
    "My password reset link says it has expired. What should I do to get a new one?",
    "Can I add a second user to my plan, and how much would that cost per month?",
    "The mobile app logs me out every time I close it. Is there a setting to stay signed in?",
    "How long does a refund usually take to show up on my card after you approve it?",
    "I was charged twice this month. Can you check what happened with my last payment?",
    "Is there a way to export all my order history to a spreadsheet file?",
    "How do I turn off the marketing emails but keep the order notifications?",
    "My delivery address is wrong on an open order. Can it still be changed today?",
    "What happens to my saved data if I end my subscription and come back later?",
    "Do you offer a discount if I switch from monthly billing to yearly billing?",
)
FILLER_ANSWER = (
    "Thanks for the question. Here is what usually solves it: open Settings in the Atlas app, "
    "choose Account, and look for the section that matches your request. Every change you "
    "make there is saved right away, and you will get a confirmation email within a few "
    "minutes. If the option is greyed out, it normally means another change is still being "
    "processed, so wait about fifteen minutes and try again. When that does not help, reply "
    "here with the exact message you see on the screen, the device you are using and the "
    "approximate time of the attempt, and I will look into it step by step with you. A "
    "quick tip that saves time: changes to billing details only apply from the next invoice, "
    "while profile and notification changes apply immediately on every device where you are "
    "signed in. You can review the full history of changes under Account, Activity, which "
    "also shows who made each change when your plan has more than one user. Is there "
    "anything else about your account that I can help you with right now?"
)


@dataclass(frozen=True)
class CallRecord:
    purpose: str
    history_tokens: int
    """Local count of the extra system blocks (summary) plus the message window."""
    input_tokens: int
    output_tokens: int


def history_tokens(request: LLMRequest) -> int:
    """Tokens the service sent besides its first (fixed) system block."""
    return count_messages(request.system[1:], request.messages)


class RecordingLLMClient:
    """Wraps any `LLMClient` and records the history tokens and usage of every call."""

    def __init__(self, inner: LLMClient) -> None:
        self.inner = inner
        self.calls: list[CallRecord] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        response = await self.inner.complete(request)
        self.calls.append(
            CallRecord(
                purpose=request.purpose,
                history_tokens=history_tokens(request),
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
        )
        return response

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        # The chat streams since phase 09: record the call when its usage arrives.
        async for event in stream_or_complete(self.inner, request):
            if event.kind == "usage" and event.usage is not None:
                self.calls.append(
                    CallRecord(
                        purpose=request.purpose,
                        history_tokens=history_tokens(request),
                        input_tokens=event.usage.input_tokens,
                        output_tokens=event.usage.output_tokens,
                    )
                )
            yield event


class ContextAwareFakeLLM:
    """Offline model whose answers depend only on the request it gets.

    - `purpose="summarize"`: a short summary that keeps the name when the input has it.
    - chat, asked for the name: answers it only when an earlier message or the summary has it.
    - any other chat message: a fixed answer of about 150 tokens.
    """

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.purpose == "summarize":
            text = self._summary(request)
        else:
            text = self._answer(request)
        return LLMResponse(
            text=text,
            tool_calls=[],
            usage=Usage(
                input_tokens=count_messages(request.system, request.messages),
                output_tokens=count_tokens(text),
            ),
            model=FAKE_MODEL,
            provider=FAKE_PROVIDER,
            stop_reason="end_turn",
        )

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        response = await self.complete(request)
        yield LLMStreamEvent(kind="text_delta", text=response.text)
        yield LLMStreamEvent(kind="usage", usage=response.usage)
        yield LLMStreamEvent(kind="stop", stop_reason=response.stop_reason)

    @staticmethod
    def _summary(request: LLMRequest) -> str:
        texts = [block.text for block in request.system] + [m.content for m in request.messages]
        summary = "Summary of the earlier conversation: the customer asked account questions."
        if any(NAME in text for text in texts):
            summary += f" The customer's name is {NAME}."
        return summary

    @staticmethod
    def _answer(request: LLMRequest) -> str:
        if not request.messages:
            return FILLER_ANSWER
        current = request.messages[-1].content
        earlier = [block.text for block in request.system[1:]]
        earlier += [message.content for message in request.messages[:-1]]
        if current in NAME_ANSWERS:
            known = any(NAME in text for text in earlier)
            return NAME_ANSWERS[current] if known else NAME_UNKNOWN[current]
        if NAME in current:
            return f"Nice to meet you, {NAME}! How can I help you today?"
        return FILLER_ANSWER


@dataclass(frozen=True)
class ModeRun:
    mode: HistoryMode
    calls: list[CallRecord]
    last_answer: str

    @property
    def name_remembered(self) -> bool:
        return NAME.lower() in self.last_answer.lower()

    @property
    def chat_calls(self) -> list[CallRecord]:
        return [call for call in self.calls if call.purpose == "chat"]

    @property
    def summarize_calls(self) -> int:
        return sum(1 for call in self.calls if call.purpose == "summarize")

    @property
    def total_tokens(self) -> int:
        return sum(call.input_tokens + call.output_tokens for call in self.calls)

    @property
    def max_history_tokens(self) -> int:
        return max((call.history_tokens for call in self.chat_calls), default=0)


def conversation_script(turns: int) -> list[str]:
    """Introduce the name, ask support questions, and ask for the name on the last turn."""
    if turns < 2:
        raise ValueError(f"turns must be at least 2, got {turns}")
    middle = [SUPPORT_QUESTIONS[i % len(SUPPORT_QUESTIONS)] for i in range(turns - 2)]
    return [INTRODUCTION, *middle, NAME_QUESTION]


async def run_name_dialogues(llm: LLMClient, settings: Settings) -> list[DialogueResult]:
    """The `demo_01` scenario, with the 2nd message in the conversation of the 1st."""
    principals = await seed_demo_principals(settings)
    results = []
    for language, (introduce, ask_name) in DIALOGUES.items():
        principal = principals[language]
        first = await send_message(
            llm, settings, principal, ChatRequest(message=introduce, ui_language=language)
        )
        second = await send_message(
            llm,
            settings,
            principal,
            ChatRequest(
                message=ask_name,
                conversation_id=first.conversation_id,
                ui_language=language,
            ),
        )
        results.append(
            DialogueResult(
                language,
                first.content,
                second.content,
                (first.conversation_id, second.conversation_id),
            )
        )
    return results


async def run_long_conversation(
    llm: RecordingLLMClient, settings: Settings, mode: HistoryMode, *, turns: int = TURNS
) -> ModeRun:
    """One conversation of `turns` messages as the en persona, with `history_mode=mode`."""
    mode_settings = settings.model_copy(update={"history_mode": mode})
    principal = (await seed_demo_principals(settings))["en"]
    start = len(llm.calls)
    conversation_id: UUID | None = None
    answer = ""
    for message in conversation_script(turns):
        response = await send_message(
            llm,
            mode_settings,
            principal,
            ChatRequest(message=message, conversation_id=conversation_id, ui_language="en"),
        )
        conversation_id = response.conversation_id
        answer = response.content
    return ModeRun(mode=mode, calls=llm.calls[start:], last_answer=answer)


def plot_tokens(runs: list[ModeRun], budget: int, path: Path) -> Path:
    """Line chart of the history tokens of every chat call, one line per mode."""
    figure = Figure(figsize=(9, 5), dpi=110)
    FigureCanvasAgg(figure)
    axes = figure.add_subplot()
    for run in runs:
        tokens = [call.history_tokens for call in run.chat_calls]
        axes.plot(range(1, len(tokens) + 1), tokens, marker=".", label=f"{run.mode} history")
    axes.axhline(budget, linestyle="--", color="gray", label=f"budget ({budget} tokens)")
    axes.set_title("Phase 02: history tokens per chat call")
    axes.set_xlabel("Chat call (turn)")
    axes.set_ylabel("History tokens (summary + messages)")
    axes.grid(True, alpha=0.3)
    axes.legend()
    figure.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path, format="png")
    return path


def _demo_llm(provider: str) -> RecordingLLMClient:
    if provider == "fake":
        inner: LLMClient = InstrumentedLLMClient(
            ContextAwareFakeLLM(), model=FAKE_MODEL, provider=FAKE_PROVIDER
        )
    else:
        inner = build_demo_llm(provider)
    return RecordingLLMClient(inner)


async def _run(provider: str, settings: Settings) -> tuple[list[DialogueResult], list[ModeRun]]:
    try:
        dialogues = await run_name_dialogues(_demo_llm(provider), settings)
        runs = [await run_long_conversation(_demo_llm(provider), settings, mode) for mode in MODES]
        return dialogues, runs
    finally:
        # The engine's pooled connections belong to this event loop; close them here.
        await dispose_engine()


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    return str(resolved.relative_to(ROOT_DIR)) if resolved.is_relative_to(ROOT_DIR) else str(path)


def _mode_cell(run: ModeRun) -> str:
    recalled = "yes" if run.name_remembered else "no"
    return (
        f"{run.mode}: {run.total_tokens:,} tokens, peak history "
        f"{run.max_history_tokens:,} tokens, name recalled on turn {TURNS}: {recalled}"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_demo_args(
        "Phase 02: one conversation remembers the name; full vs summarized history tokens",
        argv,
    )
    settings = get_settings()
    budget = settings.context_budget_tokens

    dialogues, runs = asyncio.run(_run(args.provider, settings))
    for result in dialogues:
        introduce, ask_name = DIALOGUES[result.language]
        print(f"[{result.language}] user: {introduce}")
        print(f"[{result.language}] assistant: {result.first_answer}")
        print(f"[{result.language}] user: {ask_name}")
        print(f"[{result.language}] assistant: {result.second_answer}")
        print(f"[{result.language}] name_remembered={result.name_remembered}")
    for run in runs:
        print(f"[{run.mode}] chat_calls={len(run.chat_calls)}")
        print(f"[{run.mode}] summarize_calls={run.summarize_calls}")
        print(f"[{run.mode}] total_tokens={run.total_tokens}")
        print(f"[{run.mode}] max_history_tokens={run.max_history_tokens}")
        print(f"[{run.mode}] name_remembered={run.name_remembered}")

    chart = plot_tokens(runs, budget, args.out_dir / CHART_NAME)
    print(f"Chart written to {chart}")
    by_mode = {run.mode: run for run in runs}
    recall = ", ".join(f"{r.language} {'yes' if r.name_remembered else 'no'}" for r in dialogues)
    record_result(
        PHASE,
        METRIC,
        _mode_cell(by_mode["full"]),
        _mode_cell(by_mode["summarized"]),
        notes=(
            f"provider={args.provider}; budget {budget} history tokens; {TURNS} turns; "
            f"summarized total includes {by_mode['summarized'].summarize_calls} summarize "
            f"calls; name recalled in one conversation: {recall}; "
            f"chart {_display_path(chart)}"
        ),
        results_path=args.results,
    )
    print(f"Recorded phase {PHASE:02d} in {args.results}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
