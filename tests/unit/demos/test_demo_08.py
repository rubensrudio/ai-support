"""Phase 08 demo: valid JSON rate, prompt-only vs native schema (TASK-090, TRI-04, DOD-02).

The demo calls `TriageService` directly (no HTTP, no database). The fake provider answers broken
JSON on a deterministic fraction of the first answers in `prompt_only` and valid JSON in
`schema`, so the fake row shows the mechanism, not a model's behaviour.
"""

import re
from pathlib import Path

import pytest

from api.config import get_settings
from api.llm.client import LLMError, LLMRequest, LLMResponse, Usage
from api.llm.fake import FakeLLMClient
from demos import demo_08_triage
from demos.demo_08_triage import (
    INPUTS_PATH,
    FakeTriageLLM,
    TriageTicket,
    load_tickets,
    main,
    run_mode,
    unusable_reason,
)

CATEGORIES = {"pedido", "assinatura", "reembolso", "entrega", "conta", "outro"}
_PERCENT = re.compile(r"\d+\.\d%")


def _row(path: Path) -> str:
    rows = [line for line in path.read_text().splitlines() if line.startswith("| 08 |")]
    assert len(rows) == 1
    return rows[0]


def _cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _tickets() -> list[TriageTicket]:
    return load_tickets(INPUTS_PATH)


def _reply(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        usage=Usage(input_tokens=1, output_tokens=1),
        model="fake-model",
        provider="fake",
        stop_reason="end_turn",
    )


def test_inputs_have_twenty_tickets_in_pt_and_en_covering_every_category() -> None:
    tickets = _tickets()

    assert len(tickets) >= 20
    assert len({ticket.id for ticket in tickets}) == len(tickets)
    for language in ("pt", "en"):
        chosen = [ticket for ticket in tickets if ticket.language == language]
        assert {ticket.categoria for ticket in chosen} == CATEGORIES, language
    assert all(ticket.text.strip() for ticket in tickets)


def test_fake_run_records_the_08_row_with_two_percentages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results.md"

    exit_code = main(["--provider", "fake", "--n", "10", "--results", str(results)])

    assert exit_code == 0
    metric, before, after, notes = _cells(_row(results))[1:]
    assert "valid JSON" in metric
    assert before.startswith("prompt_only:")
    assert after.startswith("schema:")
    assert _PERCENT.search(before) and _PERCENT.search(after)
    # Every 4th first answer of the fake is broken in prompt_only: 8 of 10 are valid.
    assert "80.0% (8/10" in before
    assert "100.0% (10/10" in after
    assert "provider=fake" in notes
    assert "triage_max_repairs=0" in notes
    output = capsys.readouterr().out
    assert "prompt_only" in output and "schema" in output


def test_fake_run_without_results_flag_never_touches_docs_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)

    assert main(["--provider", "fake", "--n", "4", "--no-repair-run"]) == 0

    assert _row(tmp_path / "atlas-demo08" / "results.md")


async def test_prompt_only_breaks_a_fraction_and_schema_never_does() -> None:
    tickets = _tickets()
    fake = FakeTriageLLM(tickets)
    settings = get_settings()

    prompt_only = await run_mode(fake, settings, tickets, "prompt_only", n=40, max_repairs=0)
    schema = await run_mode(fake, settings, tickets, "schema", n=40, max_repairs=0)

    assert (prompt_only.valid, prompt_only.invalid, prompt_only.errors) == (30, 10, 0)
    assert (schema.valid, schema.invalid, schema.errors) == (40, 0, 0)
    assert prompt_only.rate == pytest.approx(0.75)
    assert schema.rate == pytest.approx(1.0)
    # Without repairs every ticket costs exactly one model call.
    assert all(result.calls == 1 for result in prompt_only.results + schema.results)
    schema_requests = [r for r in fake.requests if r.response_schema is not None]
    assert len(schema_requests) == 40
    assert len(fake.requests) == 80


async def test_calls_cycle_the_tickets_and_cover_both_languages() -> None:
    tickets = _tickets()
    fake = FakeTriageLLM(tickets)

    summary = await run_mode(fake, get_settings(), tickets, "schema", n=len(tickets) + 3)

    ids = [result.ticket_id for result in summary.results]
    assert ids[: len(tickets)] == [ticket.id for ticket in tickets]
    assert ids[len(tickets) :] == [ticket.id for ticket in tickets[:3]]
    assert set(summary.by_language()) == {"pt", "en"}


async def test_default_repairs_recover_the_broken_prompt_only_answers() -> None:
    tickets = _tickets()
    fake = FakeTriageLLM(tickets)

    summary = await run_mode(fake, get_settings(), tickets, "prompt_only", n=8, max_repairs=2)

    assert (summary.valid, summary.invalid, summary.errors) == (8, 0, 0)
    assert summary.repaired == 2
    assert sum(result.calls for result in summary.results) == 10


async def test_provider_error_is_counted_apart_from_invalid_json() -> None:
    tickets = _tickets()[:3]
    llm = FakeLLMClient(
        script=[
            _reply('{"categoria": "pedido"}'),
            LLMError("boom", recoverable=True),
            _reply(
                '{"categoria": "conta", "prioridade": "alta", "sentimento": "negativo", '
                '"precisa_humano": true, "resumo": "Login blocked."}'
            ),
        ]
    )

    summary = await run_mode(llm, get_settings(), tickets, "prompt_only", n=3, max_repairs=0)

    assert [r.outcome for r in summary.results] == ["invalid", "error", "valid"]
    assert summary.rate == pytest.approx(0.5)  # valid / answered (errors excluded)


def test_ticket_file_with_unknown_category_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        '{"id": "x", "language": "pt", "categoria": "refund", "text": "Quero meu dinheiro."}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="bad.jsonl:1"):
        load_tickets(bad)


class _SchemaDownLLM(FakeTriageLLM):
    """`FakeTriageLLM` whose provider fails every `schema` request."""

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if request.response_schema is not None:
            self.requests.append(request)
            raise LLMError("provider down", recoverable=True)
        return await super().complete(request)


def test_run_with_a_mode_without_answers_fails_and_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results.md"
    monkeypatch.setattr(
        demo_08_triage, "build_triage_llm", lambda provider, tickets: _SchemaDownLLM(tickets)
    )

    exit_code = main(["--provider", "fake", "--n", "10", "--results", str(results)])

    assert exit_code != 0
    assert not results.exists()
    captured = capsys.readouterr()
    assert "schema" in captured.err and "10/10" in captured.err
    assert "rate=n/a" in captured.out
    assert "schema: n/a (0/0)" in captured.out


async def test_errors_above_half_of_the_calls_make_the_mode_unusable() -> None:
    tickets = _tickets()[:4]
    ok = (
        '{"categoria": "conta", "prioridade": "alta", "sentimento": "negativo", '
        '"precisa_humano": true, "resumo": "Login blocked."}'
    )
    down = LLMError("boom", recoverable=True)
    half = FakeLLMClient(script=[_reply(ok), down, _reply(ok), down])
    most = FakeLLMClient(script=[_reply(ok), down, down, down])

    usable = await run_mode(half, get_settings(), tickets, "schema", n=4)
    unusable = await run_mode(most, get_settings(), tickets, "schema", n=4)

    assert unusable_reason(usable) is None
    assert unusable_reason(unusable) is not None
    assert "3/4" in (unusable_reason(unusable) or "")
