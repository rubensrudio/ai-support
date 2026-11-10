"""Markdown eval report for PR comments, in English (EVAL-03, DOD-04, TASK-128).

`render_markdown(result: GateResult, current: EvalReport | None) -> str`: overall score and per
category, delta against the base, verdict (pass / regression / error / baseline created) and the
cases that got worse.
"""

from evals.gate import GateResult
from evals.runner import EvalReport


def render_markdown(result: GateResult, current: EvalReport | None = None) -> str:
    raise NotImplementedError("TASK-128")
