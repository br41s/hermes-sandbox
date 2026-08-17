# Per-run isolated checkouts for cron agents

> **STATUS: SHIPPED & VERIFIED LIVE 2026-06-28** (PR #94 core, #95 hardening,
> trace c4538230…). All tasks below done; kept for design record.

## Problem
Cron agents (biglobster SEO, gap-hunter) share ONE physical git working tree
`/opt/data/biglobster` as their `workdir`. Uncommitted edits from agent A survive
in the shared tree and agent B's "clean tree" protocol (`git checkout -- <tracked>`)
reverts them. Near-miss data loss 2026-06-28 (Langfuse 97a19e01 / 31df12d734bf...).
The mailbox page-lock is a JSON advisory lock — it does NOT lock the filesystem.

## Decisions (locked with CEO)
- Mechanism: **local clone per run** (`git clone --local`), the proven gap-hunter
  `/tmp/biglobster-pr` pattern.
- Scope: **auto for any agent job whose `workdir` is a git working tree**. No per-job
  opt-in. (no_agent script jobs are OUT of scope — they use absolute paths and are
  read-mostly; auto-cloning them is high-risk/low-value.)

## Design
Scheduler provisions an ephemeral local clone before each git-workdir AGENT run,
points `TERMINAL_CWD` at it, removes it after. Agent physically cannot reach the
shared tree, so cross-agent clobbering is impossible. A fresh clone always starts
from clean committed `origin/main`, so prior-run leftovers vanish too.

## Tasks
- [x] `cron/scheduler.py`: add `tempfile` import.
- [x] `cron/scheduler.py`: `_is_git_worktree(path)` helper.
- [x] `cron/scheduler.py`: `_provision_isolated_checkout(job, workdir) -> (eff, cleanup)`
      kill-switch `HERMES_CRON_ISOLATE_WORKDIR=0/false/no`; non-git/missing -> passthrough;
      else mkdtemp under `HERMES_CRON_CHECKOUT_DIR` or tempdir, `git clone --local`,
      set origin to source's origin URL, copy `user.*`+`credential.*` local config.
      Clone failure -> **fail-closed** (abort run; never run a write-agent on shared tree).
- [x] Wire into AGENT run path (~scheduler.py:1548); clean up in existing `finally` (~1910).
- [x] `_sweep_stale_checkouts(base, max_age_h=6)` once per `tick()` — reap crashed-run dirs.
- [x] `onsite-seo/seo-agent.prompt`: stop hardcoding `/opt/data/biglobster`; use run workdir.
      Simplify PASO 0 (fresh clone => no leftovers). Keep per-URL atomic commit+push.
- [x] `tests/test_cron_isolated_checkout.py`: detection, provisioning, isolation from a
      DIRTY source (the incident), origin/identity copy, cleanup, kill-switch, passthrough.

## Out of scope / follow-up
- Live jobs.json prompt is INDEPENDENT of repo `.prompt` — SEO prompt edit needs redeploy.
- Concurrent pushes to `main` from two clones can race (non-ff). Pre-existing; not fixed here.
- no_agent git-workdir jobs left on shared tree by design.

---

# Auditor Agent — implementation plan

A second-LLM technical reviewer that validates and improves changes before they
reach `main`, able to converse with the originating agent via the PR thread.

## Locked decisions
- **Identity:** dedicated GitHub bot account `hermes-auditor` (own PAT, repo write).
  Distinct author; future-proofs branch protection.
- **Model:** tiered — strong paid model (OpenRouter) for system-code PRs,
  free/cheap (owl-alpha tier) for content PRs.
- **Pilot scope:** hermes repo only, live agents (Claude Code + Telegram Hermes),
  hard gate on system code. Dead-cron "who fixes" deferred to phase 2.

## Architecture (locked earlier)
- Changes to hermes repo go via `review/*` branch + PR — never direct to `main`.
- New Hermes profile `auditor` runs an **agent** cron that polls open PRs.
- Per PR: classify risk by changed paths → review → comment / push fix commits /
  approve+merge / escalate to Telegram thread. `gh pr merge` is server-side, so it
  bypasses local hooks (no self-push-to-main needed).
- Reuses: incident-watcher polling+state pattern, Telegram thread routing,
  per-profile `.env` wiring in `docker/cont-init.d/03-biglobster-config`.

## Constraints (from repo, non-negotiable)
- GitHub Free + private: NO Actions, NO server-side hooks. Polling only.
- jobs.json lives on the volume; cron create needs profile/workdir via CLI.
- Repo `.prompt` files and live job prompts are INDEPENDENT — update both.

## Build slices

### Phase 1 — review loop, dry-run (no merge authority)  ✅ DONE, tests green
- [x] `docker/profiles/auditor/SOUL.md` — persona + review rubric + decision policy
      + risk-tier globs.  ← review rubric awaiting Brais's edits.
- [x] `auditor/__init__.py`, `auditor/pending.py` — open PRs unreviewed at head SHA;
      `$HERMES_HOME/auditor/state.json` dedup (mirrors incidents/state.json).
- [x] `auditor/tiers.py` — classify changed files `system` | `content` (fail-safe).
- [x] `auditor/auditor.prompt` — dry-run cron prompt (review + comment, NO merge).
- [x] Tests: `tests/auditor/test_pending.py` + `test_tiers.py` — 15 passing.

### Phase 2 — auth + live wiring
Bot account `hermes-auditor` created + Write collaborator on hermes-sandbox ✓ (2026-06-24).
CODE DONE (this branch), needs deploy + runtime steps:
- [x] `docker/profiles/auditor/repos.txt` — clones br41s/hermes-sandbox into workspace.
- [x] `docker/profiles/auditor/routing.env` — escalations → incidents thread 1904.
- [x] `03-biglobster-config §1b` — auditor `.env` GITHUB_TOKEN/GH_TOKEN overridden from
      `HERMES_AUDITOR_GITHUB_TOKEN` (resolves env → main .env). Invariant: auditor holds
      ONLY the bot token. Verified: bash -n + py_compile clean.
- [x] `03-biglobster-config §4b` — auditor `home/.git-credentials` + commit identity
      (`hermes-auditor`) pinned to the bot token; never the shared one.
- [x] `auditor/llm.py` — LLM-as-judge: resolves review model per tier from env
      (`HERMES_AUDITOR_SYSTEM_MODEL` / `HERMES_AUDITOR_CONTENT_MODEL`), OpenRouter call,
      cheap fail-safe defaults. `--show-model` to verify env took effect. +6 tests.
- [x] `03-biglobster-config §1c` — stamp those two model vars into auditor `.env` so the
      cron agent inherits them (Zeabur env → .env). Auditor-only, not in shared inject.
- [x] `auditor.prompt` + SOUL — use `auditor/llm.py --tier <tier>` for the review.
- [x] RUNTIME (on container, post-deploy): `hermes profile create auditor`; confirm
      `HERMES_AUDITOR_GITHUB_TOKEN` reachable by cont-init; `hermes cron create` agent job
      (profile=auditor, workdir=.../workspace/hermes-sandbox, ~*/10min, prompt=auditor.prompt).
      ✓ LIVE 2026-06-26.
- [x] DEPLOY GATE: commit + push + deploy (Cloud Build→ghcr→Zeabur) — deployed 2026-06-26.
- [x] Adopt branch+PR workflow: Claude Code (me) + Telegram Hermes prompt.

### Phase 3 — grant merge + enforce  ✅ LIVE 2026-06-26 (account-wide, all repos)
- [x] Flip merge authority on after clean dry-runs — content tier auto-merges;
      system tier stays advisory BY DESIGN (APPROVE comment, CEO merges).
- [x] git-guard `pre-push` layer: block agent identities pushing directly to `main`
      — deployed with the merge-time mass-deletion safety net.

### Phase 4 (later) — extend to profile repos + dead-cron handling — STILL OPEN
NOTE 2026-07-02: the auditor already polls account-wide (all repos), but the
per-repo risk-tier profiles and dead-cron handling below remain unbuilt.
TOPOLOGY: each profile owns its OWN GitHub repo, not a path in hermes-sandbox.
Repo set = union of `docker/profiles/<name>/repos.txt` + `hermes-sandbox`:
biglobster, grow-shop-api, grow-shop-landing, FinView, SocialAgenda (+ engine).
- [ ] Repo list: aggregate the `repos.txt` files (+ hermes-sandbox); cron loops
      `auditor/pending.py --repo <slug>` per repo (pending.py already repo-agnostic).
- [ ] Per-repo risk tiers: `tiers.py` is hermes-tuned only. Add tier profiles keyed
      by repo slug (biglobster Node site, FinView, grow-shop split, etc.) — without
      them, profile-repo content PRs over-review on the strong model.
- [ ] Dead-cron handling: low-risk dead-cron PRs → patch-and-merge; high-risk →
      escalate to human (live agents already converse via PR comments).
- [ ] PAT scope: `hermes-auditor` needs write on every gated repo (see Blocked).

## Blocked on Brais — RESOLVED 2026-06-24
- ~~Create GitHub bot account `hermes-auditor` + fine-grained PAT~~ — done; bot is
  a Write collaborator and the token flows via `HERMES_AUDITOR_GITHUB_TOKEN`.
  Phase 4 note: the PAT still needs write on every profile repo before extension.

## Verification
- Phase 1: unit tests green; run `auditor/pending.py` against a throwaway PR; confirm
  it posts a sensible review comment in dry-run.
- Phase 3: confirm a deliberately bad PR is blocked + escalated; a clean PR is merged.
