# Unified Infographic Engineer

One agent, one prompt, one canvas contract, deterministic verification before
anything is written. Replaces two prompts that had drifted into different
formats, different failure protocols and different quality.

Plan: `~/.claude/plans/effervescent-finding-stream.md` (approved 2026-09-22).

## Why

The CEO reported four defects: too simple, spelling errors, colour codes
rendered, edges cut off. Measured across the 82 SVG infographics published to
biglobster.top:

| Defect | Count |
|---|---|
| `<style>`/`<defs>` inside the SVG (theme-fighting, stripped on client sites) | 37 |
| `viewBox` not `0 0 800 H` (type renders up to 3x bigger on one article than another) | 30 |
| Emoji used as an icon (renders as a tofu box) | 18 |
| CSS token that does not exist (silently renders black) | 6 |
| Text overflowing its own card — the visible "cut off" | 11 articles |
| Text outside the canvas | 2 articles |

Root cause was never the model. The prompt asked the agent to do layout
arithmetic in its head, on a graphic it cannot render, with nothing downstream
checking the result.

The rendered colour codes were the **client** agent: it generated raster images
with a diffusion model, which misspells its own labels and draws hex codes as
pixels. Removing raster generation deletes that whole class.

## Status

### hermes-sandbox — branch `feat/unified-infographic-engineer`
- [x] `infographic/validate_infographic.py` — the pre-publish gate, stdlib-only,
      invoked by path (cron denies `execute_code`). Checks canvas contract, text
      vs. canvas, **text vs. its own container box**, type floor, forbidden
      elements, `var()` in geometry, multi-value geometry, per-stack design
      tokens, emoji, Spanish spelling.
- [x] `infographic/inter-metrics.json` + `measure_inter_metrics.js` — real Inter
      advance widths measured in the browser, replacing the prompt's 0.55 guess.
- [x] `infographic/infographic-engineer.prompt` — the single shared prompt. Lane
      resolved at runtime from `BL_SITE_URL`.
- [x] `infographic/bl-site-package-infographic.prompt` — deleted.
- [x] `scripts/provision_bl_client.py` — points at the shared prompt, `image_gen`
      dropped from the toolset.
- [x] `tests/scripts/test_bl_prompts_no_customer_names.py` — glob extended so the
      renamed shared prompt stays covered by the leak check.
- [x] `Dockerfile` — `hunspell hunspell-es` (neither the binary nor any Python
      spell library was in the image).
- [x] `tests/infographic/test_validate_infographic.py` — 36 tests.
- [x] `AGENT_RENTAL_SETUP.md` — the "inserts one inline SVG" row was stale; the
      comment-sentinel note was wrong for rendered pages.
- [ ] `CLAUDE.md` — record the unification and the validator.

### biglobster — branch `feat/infographic-guard-and-viewer`
- [x] `lib/infographic-guard.mjs` — build guard for the rules that need no font
      metrics. Skips legacy raster figures (171 of 253 are PNGs). Keyed on the
      path relative to `site/`, because `blog/x.html` and `en/blog/x.html` are
      different articles sharing a basename.
- [x] `lib/infographic-baseline.txt` — 65 article/code pairs, the ratchet.
- [x] `scripts/infographic-guard.test.mjs` — 25 tests, incl. the "typography is
      not emoji" cases that would otherwise fail correct articles.
- [x] Registered in `eleventy.config.mjs`; `npm run build` exits 0, and exits 1
      with a deliberate new defect.
- [ ] Ampliar viewer: fit-to-width + fullscreen zoom control, replacing the
      `min-width: 680px` / `overflow-x: auto` mobile rule.
- [ ] `projects/biglobster-web/INFOGRAPHICS.md`, `web/DESIGN.md`, `CLAUDE.md`.

### bl-site-package
- [ ] Same Ampliar viewer in `web/style.css` + `web/site.js`.
- [ ] Version bump 1.8.4 → 1.8.5.

### Prod rollout
- [x] Deployed `sha-96135994f`. First deploy (`dd6cb7fab`) shipped the prompt but
      NOT the validator — `.dockerignore` excluded `infographic/` wholesale and
      re-included only `*.prompt`. Caught by checking the pod, not by trusting
      the green deploy. Fixed in #313 with a sweep test over every prompt's
      `/opt/hermes/...` references.
- [x] `sync_prompt` on both jobs (16,926 chars each, clobber guard passed clean).
- [x] Shoroban's absolute `prompt_source` normalised to repo-relative;
      `image_gen` dropped from its live `enabled_toolsets`.
- [x] Reconciled the agent's self-written skill (see below).
- [ ] **BLOCKED** — A/B on `xiaomi/mimo-v2.6-pro`. Two arm-A runs died on the
      cron idle watchdog before producing anything. Fix the timeout budget first
      or the A/B measures the watchdog, not the model.
- [ ] Pin both jobs via `cron.jobs.update_job()` (the CLI has no model flag).
- [x] bl-site-package shipped to main as **1.8.6** (main was already on 1.8.5;
      the local tree was stale and the CI version gate caught it).

### Billing isolation — verified 2026-09-22
Shoroban runs on **its own** OpenRouter key, confirmed three ways: a distinct
key in its profile `.env`; the scheduler's exact
`resolve_runtime_provider(**runtime_kwargs)` returning it under
`_job_profile_context("bl-shoroban")` (also for `tencent/hy3` and
`xiaomi/mimo-v2.6-pro`, so the pending pin does not change it); and OpenRouter
itself reporting two separate accounts with independent usage. Future clients
are structural — `--openrouter-key` is `required=True` and validated live before
any profile or job is created.

Note the near-miss: `get_env_value()` checks `os.environ` FIRST and
`_job_profile_context` never loads the profile `.env` into it, so
`get_env_value("OPENROUTER_API_KEY")` under a tenant context really does return
BigLobster's key. The LLM runtime does not use that function. `is_available()`
is a capability check, not a billing boundary.

**Watch:** Shoroban's key had `limit_remaining` $4.69 of a $5 cap with six
rented agents against it. Hitting the cap 402s all of them, silently. Nothing
monitors tenant credit.

## Open

- **BLOCKER: the cron timeout budget ignores retries.**
  `request_timeout_seconds=600` x 3 Hermes retries = 1800s against a 1200s
  watchdog, so the watchdog always wins and the run dies as an opaque idle-kill
  instead of a reportable API timeout. `docker/config.yaml:7` says the value
  "must stay below the cron inactivity watchdog, 1200s" — right intent, wrong
  denominator. ~350s leaves margin. Fixes diagnosability, NOT a genuinely slow
  generation. Memory: `cron-timeout-budget-ignores-retries`.
- **Is the generation itself too slow?** Unresolved. The new prompt asks for
  richer SVGs; one call produced 6,279 output tokens in 170.8s. If a full
  800-wide graphic needs >600s on deepseek-v4.1-flash, that is a finding about
  the model, not the infrastructure — and an argument for the mimo test, once
  the budget stops killing every arm.
- **The self-written skill is a second surface.**
  `/opt/data/skills/creative/infographic-engineer/SKILL.md` (698 lines) was
  written by the agent's own background review and encoded the pre-change world:
  "use 360/400/600 verbatim", the old `min-width:680px` mobile regime, the
  comment sentinel, and `scripts/verify_infographic.py` — a file that does not
  exist in the biglobster repo, referenced 5 times. Reconciled by hand; the
  durable craft detail was kept. **A prompt rewrite has a second surface, and
  the background review may regenerate divergence after any run.**
- **`max_iterations_reached(16/16)` was a misread of mine.** That is the
  background memory/skill review thread (`agent/background_review.py:748`),
  which runs AFTER the agent's work; the infographic turn ends cleanly at
  ~35/90. Real but separate: memory curation is silently truncated on ~half of
  runs.
- **mimo has never run an agentic tool loop here** — only one-shot judging, as
  the auditor's system gate. Still untested.
