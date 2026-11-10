"""Sentence-buffered streaming output filter (CT-75, DA-22, GRD-04, GRD-08).

`feed(delta)` buffers deltas and returns only complete, approved sentences, in order; the first
sentence is also topic-checked. The first blocked sentence stops all further release: `feed`
returns `[]` from then on and `finish()` returns `blocked=True` with the reason, so the service
replaces the whole answer with the standard message. `finish()` checks and releases the trailing
text without final punctuation.

The topic is checked on the opening of the answer: the first sentence when it has at least
`TOPIC_MIN_CHARS` characters, otherwise the first two sentences, so a courtesy opener ("Sure!",
"Claro!", "Let me check…") is judged together with what follows it. The opening sentences are
held (not released) until the topic check approves them.

A sentence ends at `.`, `!`, `?` or `…` (optionally followed by closing quotes, brackets or
Markdown emphasis) when whitespace follows, or at a blank line. A period does not end a sentence
inside an amount (`$12.50`, no whitespace follows), after a dotted abbreviation (`e.g.`, `U.S.`,
`p.ex.`, `Dr.`) or a single letter, or after the number of a numbered list item (`1.` at the
start of a line). Each released piece keeps its trailing whitespace, so the concatenation of
every released piece is exactly the answer. `FilterOutcome.released` is the trailing text
released by `finish()`, including an opening still held for the topic check (empty
when blocked or when nothing is left).
"""

import re
from dataclasses import dataclass

from api.guardrails.output_guard import OutputContext, OutputGuard, OutputVerdict

_END = re.compile(r"[.!?…]+[\"'”’)\]*_]*(?=\s)|\n[^\S\n]*\n")
_WHITESPACE = re.compile(r"\s*")
_LAST_WORD = re.compile(r"(\S*)$")
_DOTTED = re.compile(r"(?:[A-Za-z]\.)+[A-Za-z]|[A-Za-z]")
_ABBREVIATIONS = frozenset(
    {
        "approx",
        "aprox",
        "dr",
        "dra",
        "ex",
        "jr",
        "mr",
        "mrs",
        "ms",
        "no",
        "num",
        "nº",
        "p.ex",
        "prof",
        "profa",
        "sr",
        "sra",
        "srta",
        "st",
        "vs",
    }
)


TOPIC_MIN_CHARS = 40


@dataclass(frozen=True)
class FilterOutcome:
    blocked: bool
    released: str
    reason: str | None = None


class StreamOutputFilter:
    def __init__(self, guard: OutputGuard, ctx: OutputContext) -> None:
        self.guard = guard
        self.ctx = ctx
        self._buffer = ""
        self._at_line_start = True
        self._topic_checked = False
        self._pending: list[str] = []
        self._blocked_reason: str | None = None

    async def feed(self, delta: str) -> list[str]:
        if self._blocked_reason is not None:
            return []
        self._buffer += delta
        released: list[str] = []
        while (end := self._sentence_end()) is not None:
            sentence, self._buffer = self._buffer[:end], self._buffer[end:]
            self._at_line_start = sentence.endswith("\n")
            approved = await self._accept(sentence)
            if approved is None:
                self._buffer = ""
                return released
            released.extend(approved)
        return released

    async def finish(self) -> FilterOutcome:
        if self._blocked_reason is None:
            rest, self._buffer = self._buffer, ""
            released = await self._accept(rest, final=True) if rest.strip() else []
            if released is not None and not self._topic_checked and self._pending:
                released = await self._check_topic()
            if released is not None:
                return FilterOutcome(blocked=False, released="".join(released))
        return FilterOutcome(blocked=True, released="", reason=self._blocked_reason)

    async def _accept(self, piece: str, final: bool = False) -> list[str] | None:
        """Checks one sentence; returns the pieces it releases, or None once blocked.

        Until the topic is checked, approved sentences are held: the topic is checked on the
        first sentence when it has at least `TOPIC_MIN_CHARS` characters, otherwise on the
        first two sentences (a short opener such as "Sure!" says nothing about the topic).
        """
        text = piece.strip()
        if text:
            verdict = await self.guard.check_sentence(text, self.ctx)
            if not self._record(verdict):
                self._pending = []
                return None
        if self._topic_checked:
            return [piece]
        self._pending.append(piece)
        held = [part.strip() for part in self._pending if part.strip()]
        if final or len(held) >= 2 or (held and len(held[0]) >= TOPIC_MIN_CHARS):
            return await self._check_topic()
        return []

    async def _check_topic(self) -> list[str] | None:
        self._topic_checked = True
        held, self._pending = self._pending, []
        opening = " ".join(part.strip() for part in held if part.strip())
        verdict = await self.guard.check_topic(self.ctx.question, opening, self.ctx.language)
        return held if self._record(verdict) else None

    def _record(self, verdict: OutputVerdict) -> bool:
        if verdict.action == "allow":
            return True
        self._blocked_reason = verdict.reason or "blocked"
        return False

    def _sentence_end(self) -> int | None:
        """Index just after the first complete sentence and its trailing whitespace."""
        for match in _END.finditer(self._buffer):
            if match.group(0).startswith(".") and not self._period_ends_sentence(match):
                continue
            whitespace = _WHITESPACE.match(self._buffer, match.end())
            return whitespace.end() if whitespace else match.end()
        return None

    def _period_ends_sentence(self, match: re.Match[str]) -> bool:
        if match.group(0).rstrip("\"'”’)]*_") != ".":
            return True  # "..." or ".?" style endings always end the sentence
        before = self._buffer[: match.start()]
        found = _LAST_WORD.search(before)
        word = (found.group(1) if found else "").lstrip("\"'“‘([*_")
        if word.casefold() in _ABBREVIATIONS or _DOTTED.fullmatch(word):
            return False
        if word.isdigit():
            line_start = before[: len(before) - len(word)]
            at_line_start = line_start.rstrip(" \t").endswith("\n") or (
                not line_start.strip() and self._at_line_start
            )
            return not at_line_start
        return True
