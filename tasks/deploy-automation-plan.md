# Deploy automation — close the merge→production loop

## Context

Every PR merge to `main` currently ends in the same manual dance, walked by
hand each time (see `CLAUDE.md`'s "Deploy by moving the image tag" section):
`gcloud builds submit` with a manually-computed `_COMMIT_SHA` substitution,
then `zeabur service update tag` to move the service onto it, then a manual
verification, then (if any cron job's `.prompt` file changed) a manual
`hermes cron sync-prompt <job_id>` per affected job. This has directly caused
at least one incident already (a wrong guessed SHA taken from a stale build,
causing "Service Image Pull Failed" in production — see the PR #197/#198
session). Two different mechanisms close two different halves of this, and
they should NOT be built the same way — see the tradeoff below.

## Half 1 — build + tag move (mechanical, low-risk, automate fully)

This is pure plumbing with no judgment call in it: every merge to `main`
should build and the service should end up pointed at that exact build. No
control-plane-specific risk beyond what a merge to `main` already implies.

- **Cloud Build GitHub trigger** (GCP-side, NOT GitHub Actions — this account
  has no Actions at all, per `[[github-no-actions]]`; a Cloud Build trigger is
  a separate GCP resource that subscribes to GitHub via Google's own GitHub
  App integration, unaffected by that limitation). Configure a trigger on
  `br41s/hermes-sandbox` for pushes to `main`.
- This actually SIMPLIFIES `cloudbuild.yaml`: Step 0 today hard-fails without
  a manually-passed `_COMMIT_SHA` specifically because `gcloud builds submit`
  uploads a local directory, not a repo checkout, so `$SHORT_SHA` arrives
  empty (see the comment block at the top of `cloudbuild.yaml`). A real
  trigger checks out the repo itself, so `$SHORT_SHA` is populated
  automatically — Step 0's manual-substitution requirement and its loud
  failure path can both be deleted once the trigger is live.
- **New final step** in `cloudbuild.yaml`: call the Zeabur CLI
  (`zeabur service update tag --id 6a5ea5074d439e41ee4cd38c -t sha-$SHORT_SHA -y -i=false`)
  right after the image publishes. Needs the Zeabur auth token available to
  Cloud Build as a secret (Secret Manager binding on the trigger's service
  account) — check what auth `zeabur` CLI actually needs (API token vs. the
  interactive OAuth login used today) before assuming this is a drop-in; the
  CLI may need a non-interactive auth mode that hasn't been used yet.
- **Verification step** stays as documented today (mtime / new-file check via
  `service exec`) but now runs automatically at the end of the same Cloud
  Build pipeline, failing the build loudly instead of silently leaving stale
  code running if the tag move didn't take.

## Half 2 — cron prompt sync (judgment call, route through the existing gated-remediation framework, don't build new)

This is NOT the same risk class as Half 1: a `.prompt` file changing on
`main` and a live cron job's prompt auto-rewriting itself, unattended, is
exactly the kind of "rewrites live production behavior with no human
checkpoint" action `tasks/self-remediation-loop.md` was built to gate. Reuse
that machinery instead of building a second one:

- `incidents/sweep.py:prompt_drift_incidents` (line 187) already detects
  every drifted job on each hourly watcher tick — this is the SAME detection
  the incident-watcher already uses to post to Telegram thread 1904.
- Add a new `RemediationClass` to `remediation/registry.py`, alongside the
  existing `cron-transient-failure` / `shared-clone-branch-confusion`
  entries: matcher = a `prompt-drift:*` incident id (already minted by
  `prompt_drift_incidents`), fix = the existing
  `cronjob(action="sync_prompt")` call (already built, PR #113 — this is
  calling an existing one-shot tool, not writing a new fixer).
- **Starts `gated`**, per the project's own stated policy for every new
  remediation class (`gated` → K=5 clean hand-approved runs → `auto`, per
  `tasks/self-remediation-loop.md`'s locked decisions) — never seed a new
  class straight to `auto`.
- Reversal: trivial and cheap (`sync-prompt` is idempotent — re-running it
  against an unchanged repo file is a no-op, so there's no real "undo"
  needed; if a prompt sync went out with a genuinely bad prompt, the fix is
  the same as today: fix the `.prompt` file and sync again). Note this
  explicitly in the registry entry per Phase 0's "no reversal = not
  auto-eligible, ever" guard.

## Explicitly NOT doing

- NOT wiring Half 2 to auto-fire on merge the same way Half 1 does — that
  collapses the review checkpoint self-remediation was built to preserve.
  Half 1 and Half 2 should ship as separate changes even though this doc
  covers both.
- NOT touching `HERMES_AUTONOMY`/kill-switch semantics — the new class
  inherits the same global pause every other registered class already
  respects.

## Verification (when this is actually built)

- Half 1: a real PR merge triggers a build, the build publishes, the tag
  moves, and `service exec` confirms the new file — no manual command
  anywhere in that chain. Test with a trivial doc-only PR first, not a
  behavior change, in case the pipeline itself has a bug.
- Half 2: `python -m remediation.cli list` shows a `prompt-drift` incident
  as classifiable after deliberately editing a `.prompt` file without
  syncing; `apply` runs sync-prompt and the next watcher tick shows it
  resolved. Mirror the existing test style in
  `tests/test_remediation_registry.py`.
