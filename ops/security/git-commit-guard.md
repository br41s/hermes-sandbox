# Agent git commit guard

Makes the **2026-06-22 "agent silently deleted files" failure class** impossible
across every project repo the agents write to — not just biglobster.

## What happened (the class we are closing)

An autonomous Hermes Agent commit (`dd0e1f5`, "chore: commit pending changes for
rebase sync", pushed straight to `main`) ran a blind `git add -A && git commit`
in a shared working tree that was momentarily missing files (rebase/build state).
The commit captured the **deletion of 48 cover/infographic images** in
`br41s/biglobster` while the HTML still referenced them. The blog served
404s for every cover for ~22h until `d872e9f` re-added them.

Root-cause class: **blind `git add -A`** in agent git flows **+ a shared mutable
working tree** where partial state leaks in.

## Hard constraint: client-side only

The GitHub account (`br41s`) is on the **Free plan** and the project repos
are **private**. That disables the entire server-side enforcement tier:

- ❌ GitHub Actions (workflows sit queued forever)
- ❌ Branch protection / required reviews (`403 Upgrade to Pro`)
- ❌ Rulesets (`403`)
- ❌ Custom pre-receive / server-side hooks (github.com offers none)

So enforcement **must** be a **client-side git hook installed into every clone
the agents write to**. There is no server-side backstop.

## Design

### Single source of truth — image-shipped hooks, no per-repo copies

The guard ships in the Hermes image at **`scripts/git-guard/`** and is wired into
each agent clone by setting `core.hooksPath` to that absolute path:

| File | Role |
|------|------|
| `scripts/git-guard/pre-commit` | Managed pre-commit: runs the universal check, then chains to the repo's own `.githooks/pre-commit`. Writes an alert signal on block. |
| `scripts/git-guard/check-mass-deletion.sh` | Universal, config-free mass-deletion tripwire. |
| `scripts/git-guard/post-commit` | Chains to the repo's own `.githooks/post-commit` (e.g. biglobster doc-sync). |

Because the hooks live in the image, **every deploy updates them everywhere at
once** — nothing is committed into the 5 project repos and there is no per-repo
copy to drift.

### Two layers in `pre-commit`

1. **Universal mass-deletion tripwire** (config-free, file-type-agnostic).
   Blocks any commit staging **more than N tracked deletions** (default `N=10`).
   This is what would have stopped `dd0e1f5` (48 deletions).
   - Override: `HERMES_ALLOW_MASS_DELETION=1 git commit ...`
   - Tune: `HERMES_MASS_DELETION_LIMIT=<n>`

2. **Per-project broken-refs check** — chains to the repo's own committed
   `.githooks/pre-commit` if present. biglobster's
   [`scripts/validate-assets.sh`](https://github.com/br41s/biglobster)
   (every `web/**/*.html`-referenced image must exist, baseline-ratcheted via
   `scripts/asset-refs-baseline.txt`) is reused as-is. Other repos get an adapter
   only where there is a real referenced-asset risk (see *Per-project status*).

`core.hooksPath` now points at the managed dir instead of `<repo>/.githooks`, so
the managed `post-commit` explicitly chains to the repo's own `post-commit` to
keep biglobster's model-config doc-sync firing.

### Install point

`docker/cont-init.d/03-biglobster-config`, **section 6c**, runs every boot
(idempotent, non-fatal). It walks:

- `$HERMES_HOME/profiles/*/workspace/*` — per-profile clones (section 6)
- `$HERMES_HOME/checkouts/*` — isolated biglobster site checkouts (section 6b)

and runs `git config core.hooksPath /opt/hermes/scripts/git-guard` on each. The
checkpoint shadow store (`$HERMES_HOME/checkpoints`) is not under these roots, so
it is untouched.

### Alert on block — through the incident watcher

On block, the hook appends one JSON line to
`$HERMES_HOME/incidents/blocked-commits.jsonl`. The existing **hourly incident
watcher** (`incidents/sweep.py`, PR #46) reads it via `blocked_commit_incidents()`
and delivers a brief to the **incidents Telegram thread (1904)** with the same
dedup/heartbeat as cron/Langfuse signals. No bot token is handled in the hook.
Latency is ≤1h — the block already protected the repo; the alert is awareness.

### Defeat prevention — `--no-verify` is hard-blocked

`git commit --no-verify` (and the `-n` short form) bypasses pre-commit hooks
entirely, which would defeat the whole guard. It is on the **unconditional
hardline blocklist** in `tools/approval.py` — the agent cannot run it, not even
with `--yolo` / `approvals.mode=off` / cron approve mode. (biglobster's own
post-commit doc-sync uses `--no-verify` *inside the hook subprocess*, not via the
terminal tool, so it is unaffected.)

## Per-project status

| Profile | Repo | Workspace path | Universal guard | Broken-refs adapter |
|---------|------|----------------|-----------------|---------------------|
| biglobster | br41s/biglobster | `workspace/biglobster` + 3 checkouts | ✅ | ✅ `scripts/validate-assets.sh` (HTML→images) |
| grow-shop | grow-shop-api, grow-shop-landing | `workspace/<repo>` | ✅ | follow-up (landing may serve static assets) |
| socialagenda | SocialAgenda | `workspace/SocialAgenda` | ✅ | follow-up |
| finview | FinView | `workspace/FinView` | ✅ | follow-up |

A per-project adapter is just a committed `<repo>/.githooks/pre-commit` that
validates that repo's referenced assets/links and exits non-zero on new breakage
(baseline-ratcheted so pre-existing debt does not block). The managed pre-commit
auto-discovers and chains to it.

> **finview note:** finview previously had no `repos.txt` seed, so its clone was
> hand-created on the volume. A seed (`docker/profiles/finview/repos.txt` →
> `br41s/FinView`, matching its `SOUL.md` working dir) now clones it into
> `profiles/finview/workspace/FinView` so section 6c guards it consistently.

## Override (legitimate large removal)

```bash
HERMES_ALLOW_MASS_DELETION=1 git commit -m "remove deprecated 2025 covers"
# explain the deletion in the commit body
```

## Tests

`tests/test_git_guard.py` drives the real hook scripts through a throwaway repo
(staging deletions with `git update-index --force-remove`, never `rm`) and
asserts: mass deletion blocked, override allows, small/clean commit passes,
custom limit, repo `.githooks` chaining, and the watcher signal + dedup.
`tests/tools/test_hardline_blocklist.py` covers the `--no-verify` block.
