# Staying in sync with upstream (NousResearch/hermes-agent)

> **Status (2026-09-29): living runbook.** Tooling shipped in PR #147 (2026-07-31),
> hardened in #151, #329, #331, #335; last followed for the v2026.8.31 and v2026.9.24
> merges (PRs #339/#344, 2026-09-27). The counts below (81 in-place edits, the hot-file
> list) are a 2026-07-31 snapshot that predates #331/#335. **Step 7 was skipped on both
> September merges**: `UPSTREAM_VERSION` still reads `v2026.7.20`. Moved from `tasks/`;
> `scripts/upstream_drift.py` prints this path.

Written after the `v2026.7.20` merge, which cost 44 conflicted files, 9 real
regressions and two days. Everything here exists to stop that repeating.

## The one number that matters

| | files | conflict cost |
|---|---|---|
| Our own top-level dirs (`auditor/`, `remediation/`, `incidents/`, …) | 86 | **zero, ever** |
| Our new files placed *inside* upstream dirs | 66 | no conflict, but **name-collision risk** |
| **True in-place edits of upstream files** | **81** (61 non-test) | **the entire recurring cost** |

The fork is cheap to merge because it is *additive*: 9,255 upstream commits cost
only 44 conflicts. **Protect that property.** New capability goes in a new
top-level directory, not inside upstream's tree.

Most of the in-place cost is concentrated in ~11 files: `cron/scheduler.py`,
`plugins/image_gen/openrouter/`, `cli.py`, `hermes_cli/web_server.py`,
`web/src/pages/CronPage.tsx`, `tools/cronjob_tools.py`,
`hermes_cli/container_boot.py`, `tools/approval.py`, `gateway/run.py`,
`hermes_cli/dashboard_auth/middleware.py`, `cron/jobs.py`.

## Cadence — the biggest lever, and it is superlinear

**Merge monthly. Never exceed ~6 weeks.**

Cost scales with elapsed time, not commit count, because time is what lets
upstream *rewrite* a file. `web_server.py` moved +16,446/−4,756 across 312
commits while we were away, which is why Tier D ("re-apply our intent onto
unfamiliar code") existed at all — and every serious bug came from Tier C/D.

Measured, the day after merging `v2026.7.20`:

    10 days of drift  ->  ~23 conflicted files
    2 months of drift ->   44 conflicted files

Half the work for a tenth of the wait.

## Automation

**The merge itself is prepared weekly.** A claude.ai Routine fires on Mondays with
this repo attached and follows `ops/upstream-merge/weekly-sync-routine.md`: if a
release is unmerged, it merges it on a branch per this runbook and opens one
`Upstream merge: <tag>` PR. It never merges or deploys; that stays the owner's.
The drift report below is the watchdog behind it: if it says "merge is due" while
no such PR is open, the Routine isn't running.

`scripts/upstream_drift.py` reports **on every run** — "up to date", "behind,
merge not due yet", or "merge is due" (the OLDEST unmerged upstream tag is
older than 30 days). One Telegram message a week, never zero:

- **Primary — `.github/workflows/upstream-drift.yml`**, Mondays 07:17 UTC. Full
  clone, so it carries the real commit and conflict counts. Needs repo secrets
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and optionally `TELEGRAM_THREAD_ID`.
  The job fails if the message did not go out. Run it by hand from the Actions
  tab (`workflow_dispatch`) to test.
- **Fallback — Hermes no-agent cron**, Tuesdays, seeded by
  `docker/cont-init.d/03-biglobster-config` as `upstream_drift.sh`. It runs with
  `--defer-to-actions`: silent if the workflow succeeded in the last 72h,
  otherwise it reports itself (remote mode, no conflict count) and says why the
  workflow did not. Register once in the container shell:

  ```bash
  hermes cron create '0 9 * * 2' --no-agent --script upstream_drift.sh \
      --name upstream-drift --deliver telegram
  ```

**No message on a Tuesday means both paths are down.** That is the only state
that should ever be silent.

Until 2026-09-25 the check was silent by design under the threshold, and it
measured the threshold against upstream's NEWEST tag. Upstream releases every
3-10 days, so that tag was never 30 days old: it said nothing from v2026.7.20
through v2026.9.24 while drift grew to 14 releases and 109 conflicted files.

The production image has no `.git` (`.dockerignore` excludes it), so in the
container it asks the GitHub API and compares against the `UPSTREAM_VERSION`
file.

**`UPSTREAM_VERSION` must be updated as part of every upstream merge** — it is
the anchor the whole check hangs off.

## Keep fork code out of upstream files

Every fork hunk inside an upstream file is a future conflict, and after upstream
rewrites that file it becomes "re-apply our intent onto unfamiliar code". So:

- **Fork code lives in fork-owned modules** — `hermes_cli/fork_ext/`,
  `cron/fork_ext/` (neither exists upstream) — and the upstream file keeps a
  one-to-three-line call site or re-export. `hermes_cli/web_server.py` includes
  one router; `cron/scheduler.py` imports its fork helpers in three blocks.
- **Fork tests live in `*_fork.py` files**, never appended to upstream test files,
  and never in `tests/cli/` or `tests/run_agent/` (upstream renames both).
- **Resolve a merge by re-anchoring the call site, not by re-pasting a copy.**
  `cli.py` carried 306 lines of stale handler copies for months because an old
  merge re-pasted code upstream had moved (#332); it now differs from upstream by
  one hunk, and `tests/hermes_cli/test_cli_fork.py` fails if the copies return.

## The merge itself

1. `git fetch upstream --tags` and merge the newest **tag**, never `upstream/main`
   (untagged HEAD is unreleased).
2. Resolve. **Union is the right default on this additive fork, but not a rule** —
   ~1/3 of conflicts are "same intent, different implementation", where union
   produces code that is valid and describes neither side.
3. `python scripts/check_merge_splice.py --base <tag>` after each batch.
4. Regenerate, never merge: `uv lock` and
   `python scripts/build_model_catalog.py` (`.gitattributes` keeps ours; run
   `scripts/setup-merge-drivers.sh` once per clone or the rule silently no-ops).
5. `python scripts/check_fork_collisions.py --ref <tag>`.
6. `scripts/gate.sh` — compare failing **filenames** against the baseline in
   `upstream-merge-v2026.7.20.md`, never the raw count.
7. Update `UPSTREAM_VERSION` — **derive it, never type it**. The newest tag is
   the one on screen while you merge, and it is exactly the WRONG value: it is
   what you are merging *toward*, not what is merged. Recording an unmerged tag
   makes the drift watcher report "up to date" through real drift.

   ```bash
   python3 scripts/upstream_drift.py --newest-merged > UPSTREAM_VERSION
   cat UPSTREAM_VERSION
   python3 scripts/upstream_drift.py   # must exit 0: "up to date", or "not due yet"
   ```

   Never derive it with `git tag --sort=-v:refname`: it ranks `v2026.9.24`
   above `v0.22.4` (see "Upstream's tag schemes" below).

   `upstream_drift.py` now refuses to run if the recorded tag is not an ancestor
   of HEAD, so this is caught rather than silently believed.
8. Deploy.

## The failure mode that actually bites

**Merge splice: both sides' blocks kept, the later one wins.** Six of nine
regressions. It parses, compiles, and passes ruff. Grep merged Tier C/D files
for *duplicated definitions* before reading diffs — higher yield.

Caught this way: a duplicate `cron` subparser that broke the **entire** `hermes`
CLI including `--help`; upstream's `pytest_configure` body fused onto our
fixture (`NameError: config` ×70); a pre-lock `TERMINAL_CWD` block that
reintroduced a cron writer-lock deadlock; both cron-deny blocks in `approval.py`,
which made upstream's content-threat scan dead code so **unattended cron
approved commands it should have blocked**.

Three things static analysis cannot see, so **the suite is the only real gate**:

* a dropped *parameter* still passed by keyword
* two code paths registering the same name (the CLI break — no duplicate symbol
  exists; only importing the module reveals it)
* a file silently dropped from the image by an auto-merged `.dockerignore`

## Upstream's tag schemes

Upstream tagged releases `vYYYY.M.D[.N]` through `v2026.9.24`, then switched
to semver from `v0.21.6` (2026-10-08). Every semver release is newer than
every calendar one. Only plain `vX.Y.Z` and `vYYYY.M.D[.N]` are releases:
`v0.22.4+canary.<stamp>` (nightly), `rc.N-v…` and `abandoned-rc.N-v…` are not,
and nor is anything else in upstream's 200+ tags. `scripts/upstream_drift.py`
owns this rule (`_is_release_tag`, `_version_key`); go through it rather than
sorting tags in the shell.

Until 2026-10-23 the script matched the calendar scheme only, so both it and
the weekly sync reported "up to date on v2026.9.24" through seven semver
releases. If upstream changes scheme again, the symptom is the same: a busy
upstream and a watchdog that keeps saying "up to date". Check with
`git ls-remote --tags upstream` when that happens.

## Traps that cost real time

* **`.dockerignore` name collisions.** Upstream has its own `infographic/`
  (README assets) and excludes it; ours holds a **live cron prompt**. It
  auto-merged — never conflicted — and the prompt stopped shipping.
  `scripts/check_fork_collisions.py` now catches exactly this.
* **`COPY --link` breaks Cloud Build.** Its `dockerfile.v0` frontend predates
  the flag. Local BuildKit accepts it, so **a local `docker build` is NOT a
  Cloud Build gate**. Marked as a deliberate divergence in the Dockerfile.
* **Local behaviour ≠ container behaviour.** Four levers explain nearly every
  "failure" in a containerised run: the image stamps
  `.install_method=docker` (so all `hermes update` tests no-op by design);
  running as root ignores `chmod 000` and read-only dirs; `is_container()`
  changes real behaviour (HOME → profile home, s6 present); and conftest's
  network/live-system guards block model catalog fetches and real `os.kill`.
* **Upstream moves log lines.** `Cron ticker started` in `gateway.log` became
  `In-process cron scheduler started` in **agent.log**. Its absence looks like a
  dead scheduler and is not. Authoritative check: `hermes cron status`.
* **Concurrency assumptions expire.** Upstream moved sequential cron jobs onto a
  `cron-seq` pool, so they now run alongside `cron-parallel`. That turned our
  process-wide `_hermes_home` global into a live race that leaked one job's
  profile into another and broke the incident watcher. When upstream changes
  *dispatch*, re-read every comment that says "this is safe because X runs
  sequentially".
* **Three fork-owned test lists in the root `conftest.py`, re-checked every merge**
  (v0.22.5, PR 489):
  * `_UPSTREAM_CI_LAYOUT_TESTS`: upstream tests of its own `ci.yaml` / `tests.yml` /
    `docker.yml` layout. The fork keeps `ci.yml` (owner, 2026-10-23). Drop an entry
    upstream deleted; add one only when every case fails on that layout.
  * `_UPSTREAM_NETWORK_TESTS`: upstream tests that use the real network in upstream's CI.
    Add one only after tracing the live call; a fork test is mocked instead.
  * `_FORK_PLUGIN_TOOLSETS_OFF`: upstream modules that assert exact default toolsets and
    would see the bundled `shorts` toolset.
* **Upstream's CI runs on Blacksmith runners (32 vCPU) the fork does not have.** A
  workflow the fork runs goes back to `ubuntu-latest`; an upstream-only one gets
  `if: github.repository != 'br41s/hermes-sandbox'` (never `== 'NousResearch/...'`,
  which upstream's gate tests evaluate as false). Load-sensitive e2e suites starve on
  4 vCPU: the provider catalogs run serially in `tests.yml`.
* **`js-autofix.yml` stays off here** (owner, 2026-10-24). On the fork it reformatted 282
  files byte-identical to upstream (PR 490, closed), which would turn every later upstream
  change to them into a conflict. Keep its upstream-only guard on both jobs when the merge
  takes upstream's copy.
* **The Langfuse catalog plugin is baked into the image** (owner, 2026-10-23). The
  Dockerfile's `catalog_plugins` stage fetches it at `plugin-catalog/langfuse.yaml`'s sha;
  `boot_reconcile.place_catalog_plugins` places it in every home that uses it. When upstream
  moves that sha and the plugin's `langfuse==` pin changes, the image build stops with a
  message: bump pyproject's `observability` extra to the plugin's pin and relock.

## Reducing the 81 (do after a couple of clean monthly merges)

* **Move our features out of upstream files.** `agent/memory_curator.py` is
  1,219 lines of *ours* inside *upstream's* directory. Wired through the
  plugin/hook system in our own tree, it stops being conflict surface forever.
* **Prefer config to code — DONE, and it worked.** Our OpenRouter image provider
  was a 659-line divergence that conflicted every merge. Upstream's honours
  `OPENROUTER_IMAGE_MODEL`, so on 2026-07-31 we adopted theirs and the whole
  divergence became one env var. It also gained 37 tests, model-chain fallback,
  aspect ratios and reference images. Use this as the template for the rest.
  NOTE: leaving that env var unset silently selects upstream's default
  (`openai/gpt-5.4-image-2`), the most expensive image model on OpenRouter —
  config-driven means the config must actually be set.
* **Accept the irreducible core.** `cron/scheduler.py`, `approval.py`,
  `container_boot.py` encode genuinely custom behaviour. They will keep
  conflicting — that is fine, and a much smaller surface.
