"""Markdown eval report for PR comments, in English (EVAL-03, DOD-04, TASK-128).

`render_markdown(result: GateResult, current: EvalReport | None) -> str`: overall score and per
category, delta against the base, verdict (pass / regression / error / baseline created) and the
cases that got worse.
"""

from evals.gate import MAX_OVERALL_DROP, GateResult
from evals.runner import EvalReport

MAX_WORSENED_ROWS = 25

_VERDICT_LABELS = {
    "pass": "✅ Pass",
    "regression": "❌ Regression",
    "error": "⚠️ Error",
    "baseline_created": "🆕 Baseline created",
}


def _score(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _signed(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _base_category(result: GateResult, category: str, score: float) -> float | None:
    delta = result.category_deltas.get(category)
    return None if delta is None else round(score - delta, 6)


def render_markdown(result: GateResult, current: EvalReport | None = None) -> str:
    """Render the gate result as a GitHub-flavored markdown comment."""
    lines = [
        "## Eval gate report",
        "",
        f"**Verdict:** {_VERDICT_LABELS[result.verdict]}",
        "",
    ]
    if result.message:
        lines += [f"> {_cell(result.message)}", ""]
    lines += [
        f"**Overall:** {_score(result.current_overall)} "
        f"(base {_score(result.base_overall)}, delta {_signed(result.overall_delta)})",
        "",
        f"The merge is blocked only when the overall score drops more than "
        f"{MAX_OVERALL_DROP:.1f} points; category drops are informational.",
        "",
        "### Scores by category",
        "",
    ]

    categories = current.scores.by_category if current is not None else {}
    if categories:
        lines += ["| Category | Current | Base | Delta |", "| --- | ---: | ---: | ---: |"]
        for category, score in sorted(categories.items()):
            base = _base_category(result, category, score)
            delta = result.category_deltas.get(category)
            lines.append(
                f"| {_cell(category)} | {_score(score)} | {_score(base)} | {_signed(delta)} |"
            )
    else:
        lines.append("_No category scores available._")
    lines.append("")

    if result.verdict in ("pass", "regression"):
        lines += [f"### Cases that got worse ({len(result.worsened)})", ""]
        if result.worsened:
            lines += [
                "| Case | Category | Base | Current | Delta |",
                "| --- | --- | ---: | ---: | ---: |",
            ]
            for case in result.worsened[:MAX_WORSENED_ROWS]:
                lines.append(
                    f"| {_cell(case.id)} | {_cell(case.category)} | {_score(case.base)} | "
                    f"{_score(case.current)} | {_signed(case.current - case.base)} |"
                )
            hidden = len(result.worsened) - MAX_WORSENED_ROWS
            if hidden > 0:
                lines += ["", f"_…and {hidden} more._"]
        else:
            lines.append("_No case scored lower than the baseline._")
        lines.append("")

    if current is not None:
        lines += [f"<sub>Run `{_cell(current.run_id)}` · {len(current.cases)} cases</sub>", ""]
    return "\n".join(lines)
