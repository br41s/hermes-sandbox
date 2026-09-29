# Weekly upstream sync — instructions for the scheduled Claude session

A Routine (claude.ai → Routines) fires every Monday afternoon Bangkok time with
`br41s/hermes-sandbox` attached. Its whole prompt is: *"Run the weekly upstream
sync described in `ops/upstream-merge/weekly-sync-routine.md`."* This file is that
description. Edit it here, reviewed, rather than in the Routine.

The owner wants the fork kept current with upstream releases through **one reviewed
PR per week**. You prepare that PR. **You never merge it, never deploy, and never add
the `ci-reviewed` label**; the owner does all three.

## 0. Read first

`CLAUDE.md`, then `ops/upstream-merge/upstream-merge-hygiene.md` in full: "The merge
itself", "The failure mode that actually bites" and "Traps". The runbook is
authoritative; this file only adds the weekly logistics.

## 1. Is there anything to merge?

```bash
git remote get-url upstream || git remote add upstream https://github.com/NousResearch/hermes-agent.git
git fetch --unshallow origin 2>/dev/null || true   # ancestry on a shallow clone lies
git fetch origin main && git fetch --tags upstream
python3 scripts/upstream_drift.py
```

- Merge **release tags only**, never `upstream/main`. The target is the newest
  upstream tag that is not an ancestor of `origin/main`.
- If an open PR titled `Upstream merge: …` already exists, don't open a second one.
  Bring that one up to the newest tag, or leave it and report why.
- **If every tag is merged and no such PR is open, stop.** Reply
  "Upstream: up to date on <tag>", with no branch and no PR.

## 2. Merge

Merge the target tag into a branch cut from `origin/main` with a real merge commit
(`git merge --no-ff <tag>`), never a rebase. Then follow the runbook's merge steps in
order:
- resolve conflicts;
- run `scripts/check_merge_splice.py --base <tag>` after each batch;
- regenerate `uv lock` and the model catalog, never hand-merging them;
- run `scripts/check_fork_collisions.py --ref <tag>` and `scripts/gate.sh`;
- **derive** `UPSTREAM_VERSION` from ancestry (runbook step 7);
  `python3 scripts/upstream_drift.py` must then print "up to date".

Fork rules (see `CLAUDE.md`):
- Fork code lives in `hermes_cli/fork_ext/` and `cron/fork_ext/` behind one-to-three-line
  call sites. Re-anchor call sites; never re-paste copies.
- Fork tests are the `*_fork.py` files.
- The two invariants hold: prompt caching, and a narrow core.
- Never touch `scripts/whatsapp-lead-bot`.
- Never print environment variable values or secrets.
- No model names or identifiers in commits or PR text.

Hunt for **merge splices** in every file that conflicted: duplicated definitions, or
blocks where both sides survived. That bug class caused six of nine past regressions.
Run the fork test files, and the suites covering every conflicted file. Compare
failures by **filename** against the same suites on `origin/main`, never by raw count.

## 3. PR

Open a PR titled `Upstream merge: <tag>` against `main`. The body covers:
- the tags covered, and the commit and conflicted-file counts;
- **anything you are unsure of, first**;
- each conflict and how it was resolved: union, ours, theirs, or re-anchored;
- gate results, naming any new failing test files;
- whether the `Dockerfile`, `.dockerignore`, `docker/` or
  `hermes_cli/fork_ext/boot_reconcile.py` changed, since those change how the
  production pod boots;
- post-deploy checks from `CLAUDE.md`: wait for `Gateway running` in `agent.log`,
  send a plain message in General, and confirm
  `grep -c "exited UNCLEANLY" /opt/data/logs/gateway.log` did not grow;
- rollback: `zeabur service update tag --id 6a5ea5074d439e41ee4cd38c -t sha-<previous main, short 9> -y -i=false`.

Subscribe to the PR's activity and drive CI to green. Fix real failures and answer
auditor findings; never skip or disable a test. If something can't be resolved
safely, open the PR as a **draft** with the exact open question at the top.

## 4. Report

Finish with 3–5 lines: the tag, the PR link, the CI state, and what the owner must decide.
