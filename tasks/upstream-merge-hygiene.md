# Staying in sync with upstream (NousResearch/hermes-agent)

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

`scripts/upstream_drift.py` — silent while drift is under the threshold, then
nags. Run it as a no-agent cron job (empty stdout = no message):

```bash
hermes cron create '0 9 * * 1' --no-agent --script upstream_drift.py \
    --name 'Upstream drift' --deliver telegram
```

The production image has no `.git` (`.dockerignore` excludes it), so in the
container it falls back to the GitHub API and compares against the
`UPSTREAM_VERSION` file. Run it locally for the real conflict count.

**`UPSTREAM_VERSION` must be updated as part of every upstream merge** — it is
the anchor the whole check hangs off.

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
   silences the drift watcher until upstream's next release.

   ```bash
   for t in $(git tag -l --sort=-v:refname); do \
     git merge-base --is-ancestor $t HEAD 2>/dev/null && { echo $t > UPSTREAM_VERSION; break; }; done
   cat UPSTREAM_VERSION
   python3 scripts/upstream_drift.py   # must be silent, exit 0
   ```

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
