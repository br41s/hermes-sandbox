# Self-repair eval loop (Phase A PoC)

A small harness that runs **plain-English assertions** against Hermes behaviour
and, on failure, asks a Hermes sub-agent to **propose a fix**. Built on the
existing Langfuse tracing plugin (`plugins/observability/langfuse`). Inspired by
Opik/Ollie — _"Your Agent Harness Should Repair Itself."_

```
trace  ->  judge  ->  diagnose  ->  human-approved diff  ->  verify  ->  regression-lock
(Langfuse)  (LLM /     (sub-agent     (you)                  (re-run)    (pytest in CI)
            det.)      proposes diff)
```

## Run

```bash
# Deterministic judge (no model call — same path CI uses):
uv run python -m fork_evals.run fallback_switch_notice

# LLM-as-judge (needs `hermes` CLI on PATH + provider creds):
uv run python -m fork_evals.run fallback_switch_notice --llm-judge

# TypeSafe System One judge (needs TYPESAFE_API_KEY):
uv run python -m fork_evals.run fallback_switch_notice --typesafe-judge

# On failure, ask a sub-agent to propose a fix (printed, never applied):
uv run python -m fork_evals.run fallback_switch_notice --diagnose --trace-id <langfuse_trace_id>
```

Exit code is `0` if all assertions pass, `1` otherwise.

## How it maps to Opik's four layers

| Opik layer            | Here                                                              |
|-----------------------|-------------------------------------------------------------------|
| 1. Trace              | `plugins/observability/langfuse` (write) + `diagnose.fetch_langfuse_trace` (read) |
| 3. Eval suite / judge | `cases/*.yaml` + `judge.py` (TypeSafe Noul / LLM-as-judge, deterministic fallback) |
| 2. Diagnose ("Ollie") | `diagnose.py` — sub-agent reads source + trace, proposes a diff   |
| 4. Regression lock    | a graduated pytest, e.g. `tests/test_fallback_switch_notice_regression.py` |

## Case format (`cases/<name>.yaml`)

```yaml
name: <id>
description: <what the behaviour should be — fed to the judge & diagnosis>
scenario:
  kind: function_call          # the only kind in v0
  module: agent.some_module
  function: some_function
  agent_state: {attr: value}   # set on a stub agent passed as the sole arg
  wrap: "body...\n\n{result}"  # optional: simulate caller-side formatting
assertions:
  - text: <plain-English assertion for the LLM judge>
    check:                     # deterministic fallback for hermetic / CI runs
      must_contain: [...]
      must_not_contain: [...]
      must_be_nonempty: true
source_hints: [path/to/file.py]  # what diagnose reads on failure
```

## Guardrails

- `diagnose` **never applies** a diff — it prints the proposal for human review
  (matches the delegate auto-deny default and Opik's explicit-approval model).
- Both model judges degrade to the deterministic `check` on any error, so a
  missing CLI or expired key never turns a suite red for the wrong reason. The
  degradation always shows in the printed `[mode]`, and the TypeSafe path also
  names the cause in its reason — a judge that quietly never ran is a failure
  this repo has already paid for once.
- The TypeSafe judge reads a **probability**, not a keyword, so it has no
  "unparseable verdict" state. It uses two thresholds
  (`NOUL_FAIL_BELOW` / `NOUL_PASS_ABOVE` in `judge.py`): outside the band the
  verdict is clear, and inside it the assertion **fails as `uncertain`** rather
  than being rounded into a confident green. An `uncertain` line means the
  assertion or the output is ambiguous — not necessarily that the behaviour
  is wrong.
- **Measured against `jev-latest`, 2026-09-19** (~600ms and ~420 input tokens
  per assertion). A well-posed assertion separates cleanly and repeatably:
  `fallback_switch_notice#0` scores 0.98 on correct output and 0.01 when the
  notice is missing, with 0.000 spread over three identical calls. Sampling
  noise (~0.05) appears only *inside* the band, so nothing lands near a
  threshold by accident.
- **The band earned its keep on the first live run.** Three of six assertions
  landed in it, and every one was a case asserting more than its output could
  evidence — not a judge error. `fallback_switch_notice#1` scored 0.47 because
  the `wrap:` body was a placeholder *promising* a summary; `cron_routing` and
  `dashboard_lockdown` (0.61 / 0.36) asked the judge to accept a one-line
  `OK:` summary as proof of a claim about live state. All three were fixed by
  making the output carry its evidence — the population audited, and for the
  gate the allowlist itself — and now score 0.97, 0.96 and 0.96. **Do not tune
  the thresholds to make a case green; fix what the case reports.**
- Note `cron_routing` audits **0 jobs** on a machine with no live cron jobs
  (a dev checkout, and probably CI). It used to render that as `OK:`; it now
  says `audited 0`, which both the judge and the deterministic `check` treat
  as a failure. A vacuous audit is not a pass.

## v0 boundary / next (v1)

v0 = one case, manual trigger, human-approved diff. Deferred: auto-trigger on
production bad-traces (fallback switch / 👎 reaction), multi-case suites, CI
wiring of the eval run, a results dashboard.
