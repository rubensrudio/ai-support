# ADR 0013: Eval suite on a 0 to 100 scale with a 2-point merge gate against the default branch

- Status: accepted
- Date: 2026-11-10
- Phase: 13

## Context

By phase 12 every behaviour of Atlas (grounded answers with citations, tool trajectories, triage
JSON, red-team blocking, tone) had its own offline test, but nothing told us whether a change to
`prompts/` or `api/` made the assistant better or worse as a whole. A prompt edit that drops one
rule passes every unit test and still ships a worse product.

The requirements of the phase (EVAL-01..08, EVAL-90..92):

- about 100 bilingual cases (pt and en) covering `rag` (answer plus the right citation),
  `tool_trajectory`, `json_valid`, `red_team` and `tone`, each category in both languages
  (EVAL-01, EVAL-08);
- deterministic graders for trajectories and JSON, LLM-as-judge for faithfulness (EVAL-02);
- the evals run in CI on pull requests that touch `prompts/**` or `api/**`, and only then
  (EVAL-03, EVAL-04), with the report commented on the PR;
- the merge is blocked when the score drops beyond a limit against a baseline (EVAL-05); scale,
  baseline and limit were decided in LAC-22 (EVAL-07, EVAL-91), the first run without a
  baseline in LAC-33 (EVAL-92);
- a provider or infrastructure failure must fail the job as an execution error, never as a
  score regression (EVAL-90);
- `docs/results.md` documents a real regression caught by the gate, with the PR link (EVAL-06).

`demo_13` shows the gate at work: it runs the `rag` cases with the current
`prompts/system.md` and again with the citation rule removed from that file (restored right
after the run), and the gate blocks the second run. The numbers are in the `| 13 |` row of
`docs/results.md`.

## Decision

**Dataset.** `evals/datasets/*.jsonl` in the CT-10 shape (`id`, `phase`, `category`,
`language`, `input`, `expected`, `grader`): `core_en.jsonl` and `core_pt.jsonl` plus the
`phase_NN.jsonl` and `redteam.jsonl` files of every phase, between 90 and 130 non-smoke cases
in total (`tests/unit/evals/test_dataset_composition.py`). `phase_13.jsonl` adds `tone` cases in
pt and en (a curt complaint, an unsure customer). The older phase datasets were written with
`grader: exact_match` for their offline tests; in a run they are graded by the default grader of
their category (`evals.__main__.grader_name`), and red-team controls (`input.set: control`) by
`red_team_blocked`.

**Graders** (`evals/graders.py`), every one on a 0 to 100 scale:

- deterministic, no LLM: `tool_trajectory` (exact sequence), `json_valid` (validates as
  `TriageResult`, extra fields fail), `citation` (an expected `doc_key` among the citations),
  `red_team_blocked` (status `blocked` for attacks, not blocked for controls);
- LLM-as-judge: `faithfulness` (reuses `evals.judge.judge_faithfulness`, CT-33) and `tone`
  (polite, clear, in the case language); `rag` is the mean of `citation` and `faithfulness`.
  The judge uses `ATLAS_JUDGE_MODEL` when set. A judge that fails (provider error, timeout,
  invalid reply) never aborts the run, but it is not a score either: the case is recorded as
  an execution error (see the gate rules below).

**Targets** (`evals/targets.py`): in process, never through the HTTP server. `ChatTarget` sends
the case message through `ChatService` as a seeded persona, confirming destructive tools when
the case asks for it; `TriageTarget` calls `TriageService.classify`. `python -m evals run
--target auto` sends `json_valid` (and triage-shaped) cases to triage and every other case to
chat, so one report covers every dataset; the smoke cases that are not red-team controls
(stream shape, injected provider failures) are excluded there because the in-process output
cannot carry what they check.

**Scale, baseline and limit** (LAC-22, `evals/gate.py`). The overall score and the score by
category are means from 0 to 100. The baseline is the last successful eval run of the default
branch (`develop`), published by `evals.yml` on every push as the `eval-report` artifact and
downloaded in the PR job with `gh run list` and `gh run download`. `python -m evals gate
--current <report> --base <report>` exits:

- 0 `pass` when the overall score drops 2.0 points or less (exactly 2.0 does not block,
  EVAL-91; `MAX_OVERALL_DROP = 2.0`, with the delta rounded to 6 places so float noise never
  crosses the line);
- 1 `regression` when it drops more than 2.0 points;
- 0 `baseline_created` when there is no baseline yet (EVAL-92, LAC-33): the run does not block
  and its report becomes the baseline after the merge;
- exit 2 `error` when a report has `status='error'` or cannot be read (EVAL-90). A case
  whose target fails (the provider is down or times out, the database is unreachable, the
  service raises) or whose LLM judge fails is not scored 0 as if the assistant had answered
  badly: the runner records it as an execution error (`passed=False`, `score=0` and
  `details.error`) and that marks the whole report `status='error'`. `python -m evals run`
  then exits 2 and the gate answers `error` with exit 2 before comparing any score, so an
  outage is never read as a regression and never becomes a baseline either.

Drops by category are reported only: they appear in the PR comment with the cases that got
worse, and never block the merge.

**CI** (`.github/workflows/evals.yml`). Triggered by `pull_request` with `paths: ['prompts/**',
'api/**']` and by `push` to the default branch. The PR job migrates a Postgres service, ingests
the corpus, runs the evals with the real provider, comments the markdown report
(`evals.report.render_markdown`) on the PR and fails with the gate exit code. By decision of the
user, a repository without the `ANTHROPIC_API_KEY` secret (forks, unconfigured clones) does not
fail: the job passes marked `skipped: no ANTHROPIC_API_KEY` and says so on the PR. The merge is
only blocked once the Operator marks the job as a required check of the default branch (plan
section 16).

## Alternatives considered

- **Block on any category drop** (LAC-22 option B). Categories have 4 to 30 cases, so one flaky
  judge answer moves a category by several points; blocking on them would turn noise into
  failed PRs. The overall score is the merge signal and the categories are the diagnosis.
- **A fixed, committed baseline file.** Simple and offline, but it goes stale as soon as the
  default branch improves, and every PR would have to update it. The artifact of the last
  default-branch run always reflects what is actually merged.
- **Block the first run until the Operator records a baseline** (LAC-33 option B). Safer on
  paper, but it blocks the very PR that introduces the evals and needs a manual step; the first
  run becoming the baseline costs nothing.
- **Pass/fail per case instead of a 0 to 100 score.** Judges return graded scores; collapsing
  them to booleans hides partial regressions (an answer that lost its citation but stayed
  faithful drops from 100 to 50, not to "failed").
- **Run the evals over HTTP against a deployed API.** Closer to production, but it needs a
  server, auth and rate limits in CI; the in-process targets exercise the same `ChatService`
  and `TriageService` code paths.
- **Fail the job without an API key.** It would make every fork PR red for a reason the author
  cannot fix; skipping visibly keeps the signal honest without blocking contributors.

## Consequences

- A prompt or API change that loses a behaviour is caught before the merge: in `demo_13`,
  removing the citation rule from `prompts/system.md` takes the `rag` cases from 100 to 50 in
  the fake run (citation 0, faithfulness unchanged) and the gate answers `regression`, exit 1.
- Real evals cost provider tokens and minutes on every PR touching `prompts/**` or `api/**`;
  the path filter keeps documentation and frontend PRs free.
- LLM-as-judge scores move a little between runs. The 2-point margin absorbs most of that
  noise; a persistent drift needs a re-baseline (a push to the default branch).
- The baseline is only as fresh as the last successful push run; when artifacts expire (90 days
  by default) the next PR becomes `baseline_created` and does not block.
- A score drop and an execution error are different exit codes, so the PR comment and the
  required check tell a broken provider apart from a worse prompt.
- Follow-up for the Operator: mark the evals job as a required check, open a real PR that
  degrades a prompt to record the regression and its link in the `| 13 |` row (`PR: <link>`,
  EVAL-06), and run `demo_13 --provider real` to replace the fake numbers.
