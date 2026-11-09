"""LLM-as-judge faithfulness grader (TASK-050, RAG-18, EVAL-02)."""

import pytest

from api.llm.client import LLMResponse, Usage
from api.llm.fake import FAKE_MODEL, FAKE_PROVIDER, FakeLLMClient
from api.prompts import load_prompt
from evals.dataset import EvalCase
from evals.judge import (
    GRADER_NAME,
    INVALID_RATIONALE,
    JUDGE_PROMPT,
    JUDGE_PURPOSE,
    VERDICT_SCHEMA,
    JudgeOutputError,
    JudgeVerdict,
    faithfulness_grader,
    judge_faithfulness,
    parse_verdict,
    with_faithfulness,
)
from evals.runner import DEFAULT_GRADERS, run_cases

QUESTION = "How long do I have to ask for a refund?"
CHUNKS = ["Refund policy: you can request a refund within 7 days of the charge."]
ANSWER = "You can request a refund within 7 days of the charge [1]."


def _reply(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        usage=Usage(input_tokens=50, output_tokens=20),
        model=FAKE_MODEL,
        provider=FAKE_PROVIDER,
        stop_reason="end_turn",
    )


VALID = '{"faithful": true, "score": 0.9, "rationale": "Every claim is in excerpt [1]."}'
UNFAITHFUL = '{"faithful": false, "score": 0.1, "rationale": "The 30-day claim is invented."}'


async def test_valid_json_verdict_becomes_judge_verdict() -> None:
    fake = FakeLLMClient([_reply(VALID)])

    verdict = await judge_faithfulness(fake, QUESTION, ANSWER, CHUNKS)

    assert verdict == JudgeVerdict(
        faithful=True, score=0.9, rationale="Every claim is in excerpt [1]."
    )
    assert fake.remaining == 0


async def test_request_uses_the_judge_prompt_schema_and_purpose() -> None:
    fake = FakeLLMClient([_reply(VALID)])

    await judge_faithfulness(fake, QUESTION, ANSWER, CHUNKS)

    [request] = fake.requests
    assert request.purpose == JUDGE_PURPOSE == "judge"
    assert request.response_schema == VERDICT_SCHEMA
    assert set(VERDICT_SCHEMA["required"]) == {"faithful", "score", "rationale"}
    assert request.system[0].text == load_prompt(JUDGE_PROMPT)
    [message] = request.messages
    assert message.role == "user"
    assert QUESTION in message.content
    assert ANSWER in message.content
    assert '<excerpt index="1">' in message.content
    assert CHUNKS[0] in message.content


async def test_no_chunks_tells_the_judge_there_are_no_excerpts() -> None:
    fake = FakeLLMClient([_reply(UNFAITHFUL)])

    verdict = await judge_faithfulness(fake, QUESTION, "You have 30 days.", [])

    assert verdict.faithful is False
    content = fake.requests[0].messages[0].content
    assert "<excerpt index" not in content
    assert "no excerpts" in content.lower()


async def test_invalid_json_is_retried_once_then_the_valid_verdict_is_used() -> None:
    fake = FakeLLMClient([_reply("Sure! The answer looks faithful."), _reply(UNFAITHFUL)])

    verdict = await judge_faithfulness(fake, QUESTION, ANSWER, CHUNKS)

    assert verdict.faithful is False
    assert verdict.score == pytest.approx(0.1)
    assert len(fake.requests) == 2


async def test_invalid_json_twice_returns_the_unfaithful_fallback_verdict() -> None:
    fake = FakeLLMClient([_reply("not json"), _reply('{"faithful": "yes"}')])

    verdict = await judge_faithfulness(fake, QUESTION, ANSWER, CHUNKS)

    assert verdict == JudgeVerdict(faithful=False, score=0.0, rationale=INVALID_RATIONALE)
    assert INVALID_RATIONALE == "judge output invalid"
    assert len(fake.requests) == 2


@pytest.mark.parametrize(
    "text",
    [
        "not json at all",
        "[1, 2, 3]",
        '{"faithful": true, "score": 0.5}',
        '{"faithful": true, "score": 1.5, "rationale": "x"}',
        '{"faithful": true, "score": -0.1, "rationale": "x"}',
        '{"faithful": "true", "score": 0.5, "rationale": "x"}',
        '{"faithful": true, "score": 0.5, "rationale": 7}',
    ],
)
def test_parse_verdict_raises_a_typed_error_for_invalid_output(text: str) -> None:
    with pytest.raises(JudgeOutputError):
        parse_verdict(text)


def test_parse_verdict_accepts_a_fenced_json_block_and_integer_scores() -> None:
    verdict = parse_verdict('```json\n{"faithful": true, "score": 1, "rationale": "ok"}\n```')

    assert verdict == JudgeVerdict(faithful=True, score=1.0, rationale="ok")


def test_faithfulness_grader_is_in_the_runner_grader_registry() -> None:
    graders = with_faithfulness(FakeLLMClient([]))

    assert GRADER_NAME == "faithfulness"
    assert GRADER_NAME in graders
    assert set(DEFAULT_GRADERS) <= set(graders)
    assert GRADER_NAME not in DEFAULT_GRADERS  # the base registry is not mutated


async def test_faithfulness_grader_scores_a_rag_case_through_the_runner() -> None:
    case = EvalCase(
        id="judge-rag-en-001",
        phase=3,
        category="rag",
        language="en",
        input={"question": QUESTION},
        expected={"faithful": True},
        grader=GRADER_NAME,
    )

    class AnswerTarget:
        async def run(self, case: EvalCase) -> dict[str, object]:
            return {"answer": ANSWER, "chunks": CHUNKS}

    fake = FakeLLMClient([_reply(VALID)])
    report = await run_cases([case], AnswerTarget(), with_faithfulness(fake))

    assert report.status == "ok"
    [result] = report.cases
    assert result.passed is True
    assert result.score == pytest.approx(90.0)
    assert result.details["rationale"] == "Every claim is in excerpt [1]."
    assert QUESTION in fake.requests[0].messages[0].content


async def test_faithfulness_grader_fails_an_unfaithful_answer() -> None:
    case = EvalCase(
        id="judge-rag-pt-001",
        phase=3,
        category="rag",
        language="pt",
        input={"message": "Qual é o prazo de reembolso?"},
        expected={"faithful": True},
        grader=GRADER_NAME,
    )
    grader = faithfulness_grader(FakeLLMClient([_reply(UNFAITHFUL)]))

    grade = await grader(case, {"content": "Você tem 30 dias.", "chunks": []})

    assert grade.passed is False
    assert grade.score == pytest.approx(10.0)
