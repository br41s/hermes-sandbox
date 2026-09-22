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

> **Context correction, 2026-09-22.** The manual dance described above is no
> longer what happens. Since 2026-09-18 GitHub Actions
> (`.github/workflows/ghcr-publish.yml`) builds every push to `main` and
> publishes `:latest` + `:sha-<commit>`, and `scripts/deploy.sh` derives the SHA
> itself, refuses a non-`main` branch, refuses to move the tag to an image that
> was never published, and polls the pod until it reports that commit. Cloud
> Build is now the `--build` fallback, not the primary path. The premise below
> that "this account has no Actions at all" was corrected — Actions do run; the
> June reading was wrong.

## Half 1 — build + tag move (mechanical, low-risk, automate fully)

> **SUPERSEDED 2026-09-22 — see "Half 1 (revised)" below.** Kept for its
> reasoning, not as a plan. The Cloud Build GitHub trigger it proposes is moot:
> Actions already builds and publishes on every merge, so the build half of this
> section is *done*. What remained open was the tag move, and that was
> deliberately re-scoped rather than automated — see the revised section for the
> decision and why.


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

## Half 1 (revised) — detect the drift, keep the human on the trigger

**Decided 2026-09-22.** Build automation is done (Actions). The tag move stays
manual *by choice*; what gets built instead is a watcher signal that makes stale
production impossible to miss.

### Why not finish the automation

The original Half 1 assumed the expensive part was the deploy. It is not — since
`scripts/deploy.sh` landed, a deploy is one idempotent tag move against an image
Actions has already built and verified. It takes seconds.

The expensive part is **nobody noticing**. On 2026-09-22 production sat 11
commits behind `main` and it surfaced only because someone happened to look. The
gap was attention, not effort, so automating the tag move would be solving the
cheap half.

Auto-deploy-on-merge was considered and **rejected for now** on two grounds:

- It needs a Zeabur credential in Actions secrets. This repo is the control
  plane for every other project; a token that can repoint production is new
  attack surface on exactly the wrong repo.
- It makes every merge ship, including one the auditor's `auto-merge` label
  lands unattended overnight. That removes the last human checkpoint in front of
  the control plane, which is not a trade worth making to save ten seconds.

Revisit once the drift signal has produced a few weeks of real data on how often
production actually lags, and by how much. That is a decision to make on
evidence, not on a guess about deploy cadence.

### What gets built

One new incident producer in `incidents/sweep.py`, `deploy_drift_incidents()`,
wired into `sweep()` exactly like `checkout_drift` — a
`deploy_drift: Optional[List[Incident]] = None` kwarg defaulting to the
producer. It follows the established shape of `prompt_drift_incidents` and
`checkout_drift_incidents`; this establishes no new pattern.

**Running commit — a local file read, no credential.** The watcher runs *inside*
the pod, so the deployed commit is `cat /opt/hermes/.hermes_build_sha` (stamped
by the Dockerfile from `HERMES_GIT_SHA`). No `zeabur service exec`, no Zeabur
token anywhere in this design. That is the main reason this approach is cheap.

**Target + drift — one GitHub API call.**
`GET /repos/{repo}/compare/{running_sha}...main` returns `ahead_by`, `commits[]`
and `files[]` together. Token resolution mirrors `dependency_alert_incidents`
exactly: `HERMES_DEPLOY_DRIFT_GITHUB_TOKEN` -> `GITHUB_TOKEN` -> `GH_TOKEN`.
Cost is one call per hourly sweep.

> **Verified 2026-09-22 against the founding case.** `compare/777a10031...2cd61760e`
> returns `ahead_by: 11`, `total_commits: 11`, `files: 13`.
>
> **`files` is the AGGREGATE diff across the range, not per-commit.** A
> per-commit breakdown costs one `/commits/{sha}` call *each* — 11 extra calls
> for this range — which is the wrong shape for an hourly sweep. Decide on the
> aggregate list and stay at one call.

**Runtime-relevance filter — the anti-crying-wolf rule.** Of the 11 commits in
the founding incident, **10 touched `.github/workflows` and nothing else**
(confirmed per-commit via the API); the eleventh carried 3 runtime files. An
alert that shouts "11 commits behind" when ten are inert trains you to ignore
it.

So: fire only when the **aggregate** `files` list contains at least one path
outside the inert set (`.github/**`, `tasks/**`, `tests/**`). Report `ahead_by`
alongside the count of runtime-relevant *files* — deliberately files, not
commits, because that is what one call can tell you honestly. In the founding
case that reads "11 commits behind, 3 runtime files changed", which is both true
and actionable.

Keep the inert set *tight* — **confirmed 2026-09-22, build it as written**.
Under-filtering costs one unnecessary alert; over-filtering costs a missed
deploy. Specifically, `*.md` is NOT inert — skill
`SKILL.md` files and `AGENTS.md` are read by the runtime — and `.prompt` files
are emphatically not inert (they are Half 2's whole subject).

**Grace threshold.** Fire on the age of the *oldest* undeployed
runtime-relevant commit, not on the count, so a normal merge-then-deploy cycle
never alerts. `DEPLOY_DRIFT_GRACE_HOURS`, default 6, env-tunable, mirroring
`STALE_GRACE_HOURS`.

**Dedup key: `deploy-drift:<running_sha>`.** One alert per stale deployment; it
goes quiet until the running sha changes, i.e. until you actually deploy.
Deliberately NOT keyed on the drift count — that would re-alert on every
subsequent merge and turn one stale deploy into a stream.

The accepted cost: if production stays stale for a week you get one alert, not
an escalating nag. The 24h heartbeat still runs underneath. If that proves too
quiet in practice, re-arm on crossing a commit-count bucket — but ship the
quiet version first. **Confirmed 2026-09-22: ship quiet, no escalating nag in
v1.**

### Failing loudly, not silently

A producer that returns `[]` on every error is indistinguishable from a healthy
one with nothing to report. That is exactly how the auditor judge went 12 weeks
without running, and it is the failure mode this repo keeps re-learning.

So: a `deploy-drift-blind:<reason>` incident, exempt from the `seen` baseline,
following the `depalert-blind:` precedent verbatim. Reasons that must be loud
rather than silent:

- no token resolved;
- `/opt/hermes/.hermes_build_sha` absent (true of images built before the arg
  was wired — report it, never guess "up to date");
- the compare API errored;
- **the running sha is not an ancestor of `main`** — production is serving code
  that is not on `main` at all. That is the scariest state of the four and
  `scripts/deploy.sh` already calls it out separately; mirror that here.

Nothing in this producer may raise. It must never be able to take the rest of
the sweep down with it.

### Tests

Hermetic, mirroring `tests/test_incident_sweep_regression.py` — inject the
compare payload and the running sha; no network, no container.

- **The founding case**: the real 2026-09-22 compare payload (`ahead_by: 11`,
  13 aggregate files, 3 of them runtime) -> fires, and the brief reads
  "11 commits behind, 3 runtime files changed".
- Aggregate diff touching only `.github/**` -> silent, however many commits.
- Inside the grace window -> silent.
- Same running sha across two sweeps -> exactly one alert.
- Each of the four blind reasons -> a blind incident, never silence.
- A blind incident on a fresh state file is NOT swallowed by the baseline.

### Verification

Run the producer against real production state and confirm it reports the drift
that genuinely exists, then deploy and confirm it goes quiet. A passing unit
test is not evidence the signal works against the live pod — read the value out
of `/opt/hermes/.hermes_build_sha` and compare by hand once.

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
- Reversal: cheap in the common case, but **not universally** — and the
  original wording of this bullet ("trivial and cheap") was wrong in a way
  worth keeping visible, because it is the reasoning that would have made
  this class unsafe to automate.

  `sync-prompt` is idempotent against the *repo* file, so re-running it is a
  no-op and a bad repo prompt is fixed by fixing the file and syncing again.
  That is true. What it misses is that sync only ever pushes **repo → live**.
  A fix applied ONLY to a live job — the emergency path, where a job is
  failing in production and someone edits its prompt in place — exists
  nowhere else. Syncing over it does not "revert" anything; it deletes the
  only copy. That is unreversible, and it is not hypothetical: the Gap
  Hunter's output-limit fix reached the live job days before it reached the
  `.prompt` file (see `docs/runbook-gap-hunter-cron.md` in `br41s/biglobster`).

  **Guard (shipped, 2026-09-08).** `cronjob(action="sync_prompt")` now records
  `prompt_synced_sha` — the hash of what it last wrote — and refuses to sync
  when the live prompt no longer matches it, because that means someone
  changed the live side since and their edit is what would be overwritten.
  `force=True` overrides and reports what it destroyed.

  Two consequences for this plan, both binding on the remediation class:

  - The class must **never pass `force=True`**. A refusal is the correct
    outcome and should surface as a human-review item, not be retried around.
  - A job with **no `prompt_synced_sha` yet** cannot be judged — the guard
    lets that first sync through and flags it with a `warning`, which is fine
    for a human but not for an unattended fixer. The class must treat a
    missing baseline as needing review, so every job's first sync is
    hand-approved and the baseline is established under supervision.

  With those two rules the class is reversible in the sense Phase 0 requires;
  without them it is not, and per Phase 0's "no reversal = not auto-eligible,
  ever" guard it would not qualify for promotion to `auto`. Note this
  explicitly in the registry entry.

## Explicitly NOT doing

- NOT wiring Half 2 to auto-fire on merge the same way Half 1 does — that
  collapses the review checkpoint self-remediation was built to preserve.
  Half 1 and Half 2 should ship as separate changes even though this doc
  covers both.
- NOT touching `HERMES_AUTONOMY`/kill-switch semantics — the new class
  inherits the same global pause every other registered class already
  respects.
- **NOT moving the service tag from CI (added 2026-09-22).** No Zeabur
  credential in Actions secrets, and none in the watcher either — the drift
  producer only ever *reads* (a local file plus one GitHub compare call). The
  human stays the trigger for every production rollout.
- **NOT auto-deploying on merge (added 2026-09-22).** Deferred, not rejected
  forever; revisit with the drift data the signal produces. See "Half 1
  (revised)" for the two grounds.

## Verification (when this is actually built)

- ~~Half 1: a real PR merge triggers a build, the build publishes, the tag
  moves, and `service exec` confirms the new file — no manual command
  anywhere in that chain. Test with a trivial doc-only PR first, not a
  behavior change, in case the pipeline itself has a bug.~~
  **Superseded 2026-09-22** — the build half of this is live and verified
  (Actions publishes on every merge); the tag move is deliberately not
  automated. Half 1's acceptance criteria now live in "Half 1 (revised) →
  Verification".
- Half 2: `python -m remediation.cli list` shows a `prompt-drift` incident
  as classifiable after deliberately editing a `.prompt` file without
  syncing; `apply` runs sync-prompt and the next watcher tick shows it
  resolved. Mirror the existing test style in
  `tests/test_remediation_registry.py`.
