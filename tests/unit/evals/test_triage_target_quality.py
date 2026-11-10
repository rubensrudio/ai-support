from api.config import Settings
from api.llm.client import LLMError, LLMResponse, Usage
from api.llm.fake import FakeLLMClient
from evals.dataset import EvalCase
from evals.targets import TriageTarget


def _case() -> EvalCase:
    return EvalCase(
        id="q-1",
        phase=13,
        category="json_valid",
        language="en",
        input={"text": "My box arrived crushed and the tea leaked everywhere.", "mode": "schema"},
        expected={},
        grader="json_valid",
    )


def _settings() -> Settings:
    return Settings(_env_file=None, llm_provider="fake", triage_max_repairs=1)  # type: ignore[call-arg]


def _bad(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        usage=Usage(input_tokens=1, output_tokens=1),
        model="fake-model",
        provider="fake",
        stop_reason="end_turn",
    )


async def test_invalid_json_after_repairs_is_a_quality_zero_not_an_execution_error() -> None:
    llm = FakeLLMClient([_bad('{"categoria": "billing"}'), _bad("not json at all")])

    output = await TriageTarget(_settings(), llm=llm).run(_case())

    assert output["triage"] is None
    assert output.get("error") is None


class _Down:
    async def complete(self, request: object) -> LLMResponse:
        raise LLMError("provider down", recoverable=True, status=503)

    def stream(self, request: object) -> object:
        raise LLMError("provider down", recoverable=True, status=503)


async def test_provider_failure_is_an_execution_error() -> None:
    output = await TriageTarget(_settings(), llm=_Down()).run(_case())  # type: ignore[arg-type]

    assert output["triage"] is None
    assert output["error"]
