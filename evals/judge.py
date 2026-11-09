"""LLM-as-judge faithfulness grader (CT-33, RAG-18, EVAL-02).

`judge_faithfulness` asks an LLM whether an answer is supported by the excerpts the assistant
was given, with the rubric of `prompts/judge_faithfulness.md` and a structured output
(`response_schema`, `purpose="judge"`). Output that is not a valid verdict is retried once;
a second invalid output yields `JudgeVerdict(faithful=False, score=0.0,
rationale="judge output invalid")`, so a broken judge never counts an answer as faithful.
`LLMError` from the provider is not caught: the caller decides how to report it.

`faithfulness_grader(llm)` adapts the judge to the eval runner (`Grader`, CT-10): the case
input holds the `question` (or `message`), the target output holds the `answer` (or
`content`) and the `chunks` texts. `with_faithfulness(llm)` returns the runner registry with
the `faithfulness` grader added; the base `DEFAULT_GRADERS` stays untouched because this grader
needs an LLM client.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, ValidationError

from api.llm.client import ChatMessage, LLMClient, LLMRequest, SystemBlock
from api.prompts import load_prompt
from evals.dataset import EvalCase
from evals.runner import DEFAULT_GRADERS, Grader, GradeResult

JUDGE_PROMPT = "judge_faithfulness"
JUDGE_PURPOSE = "judge"
JUDGE_MAX_TOKENS = 512
JUDGE_ATTEMPTS = 2
GRADER_NAME = "faithfulness"
INVALID_RATIONALE = "judge output invalid"

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "faithful": {"type": "boolean"},
        "score": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string"},
    },
    "required": ["faithful", "score", "rationale"],
    "additionalProperties": False,
}

_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


@dataclass(frozen=True)
class JudgeVerdict:
    faithful: bool
    score: float
    rationale: str


class JudgeOutputError(ValueError):
    """The judge reply is not a JSON object matching `VERDICT_SCHEMA`."""


class _VerdictPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    faithful: StrictBool
    score: float = Field(ge=0.0, le=1.0)
    rationale: StrictStr


def build_judge_request(question: str, answer: str, chunks: Sequence[str]) -> LLMRequest:
    """System rubric plus one user message with the question, numbered excerpts and answer."""
    if chunks:
        excerpts = "\n".join(
            f'<excerpt index="{index}">\n{chunk}\n</excerpt>'
            for index, chunk in enumerate(chunks, start=1)
        )
    else:
        excerpts = "(no excerpts were given to the assistant)"
    content = (
        f"<question>\n{question}\n</question>\n\n"
        f"<excerpts>\n{excerpts}\n</excerpts>\n\n"
        f"<answer>\n{answer}\n</answer>"
    )
    return LLMRequest(
        system=[SystemBlock(text=load_prompt(JUDGE_PROMPT))],
        messages=[ChatMessage(role="user", content=content)],
        response_schema=VERDICT_SCHEMA,
        max_tokens=JUDGE_MAX_TOKENS,
        purpose=JUDGE_PURPOSE,
    )


def parse_verdict(text: str) -> JudgeVerdict:
    """Parse a judge reply (optionally inside a ```json fence). Raises `JudgeOutputError`."""
    stripped = text.strip()
    fenced = _FENCE.match(stripped)
    if fenced:
        stripped = fenced.group(1)
    try:
        raw = json.loads(stripped)
    except json.JSONDecodeError as error:
        raise JudgeOutputError(f"judge output is not JSON ({error.msg})") from error
    if not isinstance(raw, dict):
        raise JudgeOutputError("judge output is not a JSON object")
    try:
        payload = _VerdictPayload.model_validate(raw)
    except ValidationError as error:
        raise JudgeOutputError(f"judge output does not match the schema: {error}") from error
    return JudgeVerdict(
        faithful=payload.faithful, score=float(payload.score), rationale=payload.rationale
    )


async def judge_faithfulness(
    llm: LLMClient, question: str, answer: str, chunks: list[str]
) -> JudgeVerdict:
    """Verdict on whether `answer` is supported by `chunks`; see the module docstring."""
    request = build_judge_request(question, answer, chunks)
    for _ in range(JUDGE_ATTEMPTS):
        response = await llm.complete(request)
        try:
            return parse_verdict(response.text)
        except JudgeOutputError:
            continue
    return JudgeVerdict(faithful=False, score=0.0, rationale=INVALID_RATIONALE)


def _text_field(data: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str):
            return value
    raise KeyError(f"expected one of {keys!r} as text")


def faithfulness_grader(llm: LLMClient) -> Grader:
    """Runner grader: score = 100 × judge score, passed = judge `faithful`."""

    async def grade(case: EvalCase, output: dict[str, Any]) -> GradeResult:
        question = _text_field(case.input, "question", "message")
        answer = _text_field(output, "answer", "content")
        chunks = [str(chunk) for chunk in output.get("chunks", [])]
        verdict = await judge_faithfulness(llm, question, answer, chunks)
        return GradeResult(
            score=verdict.score * 100.0,
            passed=verdict.faithful,
            details={"rationale": verdict.rationale, "judge_score": verdict.score},
        )

    return grade


def with_faithfulness(
    llm: LLMClient, graders: Mapping[str, Grader] = DEFAULT_GRADERS
) -> dict[str, Grader]:
    """A copy of `graders` with the `faithfulness` grader bound to `llm`."""
    return {**graders, GRADER_NAME: faithfulness_grader(llm)}
