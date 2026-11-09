"""LLM-as-judge faithfulness grader (CT-33). Skeleton stub: implemented by TASK-050."""

from dataclasses import dataclass

from api.llm.client import LLMClient


@dataclass(frozen=True)
class JudgeVerdict:
    faithful: bool
    score: float
    rationale: str


async def judge_faithfulness(
    llm: LLMClient, question: str, answer: str, chunks: list[str]
) -> JudgeVerdict:
    raise NotImplementedError("TASK-050")
