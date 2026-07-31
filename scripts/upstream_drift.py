#!/usr/bin/env python3
"""Report how far this fork has drifted from upstream. Silent when it hasn't.

WHY THIS EXISTS. Merging upstream v2026.7.20 after ~2 months of drift cost 44
conflicted files, 9 real regressions, and two days. The cost is superlinear in
elapsed time, not in commit count: waiting long enough for upstream to *rewrite*
a file (web_server.py moved +16,446/-4,756 across 312 commits) turns a hunk
resolution into "re-apply our intent onto unfamiliar code", which is where every
serious bug came from. Merging monthly keeps you resolving code you recognise.

This is designed to run as a no-agent cron job: it prints NOTHING while drift is
under the threshold, so it is silent until it matters, then nags weekly.

    hermes cron create '0 9 * * 1' --no-agent --script upstream_drift.py \
        --name 'Upstream drift' --deliver telegram

TWO MODES, because the production image has no .git (.dockerignore excludes it):

  * full   — run from a clone with an `upstream` remote. Fetches, finds the
             newest upstream tag, and computes the REAL conflict count with
             `git merge-tree` (which writes nothing).
  * remote — no git repo: asks the GitHub API for upstream's newest tag and
             compares it to UPSTREAM_VERSION. Tells you drift exists; you run
             the full mode locally for the conflict count.

Exit codes: 0 under threshold (or informational), 1 over threshold, 2 error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

UPSTREAM_REPO = "NousResearch/hermes-agent"
_REPO_ROOT = Path(__file__).resolve().parent.parent
_TAG_RE = re.compile(r"^v(\d{4})\.(\d+)\.(\d+)$")


def _version_key(tag: str) -> tuple[int, int, int]:
    """Sort key for a vYYYY.M.D tag.

    MUST be numeric. Sorting these as strings puts v2026.7.7 above v2026.7.30
    because '7' > '3' character-wise — which is exactly what this script got
    wrong on its first run in the container, reporting a tag three weeks stale
    as the newest. ``git tag --sort=-v:refname`` (full mode) already does this
    correctly; only the GitHub API path needed it.
    """
    m = _TAG_RE.match(tag)
    return tuple(int(g) for g in m.groups()) if m else (0, 0, 0)


def _run(*args: str, check: bool = True) -> str:
    return subprocess.run(
        args, capture_output=True, text=True, check=check, cwd=_REPO_ROOT
    ).stdout.strip()


def _have_git_repo() -> bool:
    try:
        return _run("git", "rev-parse", "--is-inside-work-tree") == "true"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _recorded_version() -> str | None:
    try:
        return (_REPO_ROOT / "UPSTREAM_VERSION").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _api_latest_tag() -> tuple[str, str] | None:
    """(tag, iso_date) of upstream's newest release-style tag, via GitHub API."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{UPSTREAM_REPO}/tags?per_page=50",
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "hermes-upstream-drift"},
    )
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            tags = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        print(f"upstream-drift: could not reach the GitHub API: {exc}", file=sys.stderr)
        return None

    names = sorted(
        (t["name"] for t in tags if _TAG_RE.match(t.get("name", ""))),
        key=_version_key, reverse=True,
    )
    if not names:
        return None
    newest = names[0]
    commit_url = next(t["commit"]["url"] for t in tags if t["name"] == newest)
    try:
        creq = urllib.request.Request(commit_url, headers={"User-Agent": "hermes-upstream-drift"})
        if token:
            creq.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(creq, timeout=30) as resp:
            date = json.load(resp)["commit"]["committer"]["date"]
    except Exception:
        date = ""
    return newest, date


def _days_since(iso: str) -> int | None:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).days


def _full_report(threshold: int) -> int:
    try:
        _run("git", "fetch", "--tags", "--quiet", "upstream")
    except subprocess.CalledProcessError:
        print("upstream-drift: no 'upstream' remote. Add it with:\n"
              f"  git remote add upstream https://github.com/{UPSTREAM_REPO}.git",
              file=sys.stderr)
        return 2

    tags = [t for t in _run("git", "tag", "-l", "--sort=-v:refname").splitlines()
            if _TAG_RE.match(t)]
    if not tags:
        print("upstream-drift: no upstream release tags found", file=sys.stderr)
        return 2
    newest, current = tags[0], _recorded_version()

    if current == newest:
        return 0  # silent: nothing to do

    behind = _run("git", "rev-list", "--count", f"HEAD..{newest}", check=False) or "?"
    tag_date = _run("git", "log", "-1", "--format=%cI", newest, check=False)
    age = _days_since(tag_date) if tag_date else None

    conflicts = "?"
    try:
        out = subprocess.run(
            ["git", "merge-tree", "--write-tree", "HEAD", newest],
            capture_output=True, text=True, cwd=_REPO_ROOT,
        )
        # Non-zero exit means conflicts; the conflict list follows the tree oid.
        files = {ln.split()[-1] for ln in out.stdout.splitlines()
                 if ln.startswith(("100", "120", "160"))}
        conflicts = str(len(files)) if files else ("0" if out.returncode == 0 else "?")
    except (subprocess.CalledProcessError, FileNotFoundError, IndexError):
        pass

    # Stay silent until the newest tag has been out longer than the threshold.
    # A tag cut today is not yet a reason to act — conflict count is NOT part of
    # this test, because it is essentially never zero on an active fork.
    if age is not None and age < threshold:
        return 0

    print(f"⚠️  Upstream drift: we are on {current}, upstream released {newest}"
          + (f" {age} days ago" if age is not None else ""))
    print(f"    {behind} commits behind · ~{conflicts} files would conflict")
    print(f"    Merge runbook: tasks/upstream-merge-hygiene.md")
    return 1


def _remote_report(threshold: int) -> int:
    current = _recorded_version()
    latest = _api_latest_tag()
    if latest is None:
        return 2
    newest, date = latest
    if current == newest:
        return 0  # silent
    age = _days_since(date) if date else None
    if age is not None and age < threshold:
        return 0

    print(f"⚠️  Upstream drift: we are on {current}, upstream released {newest}"
          + (f" {age} days ago" if age is not None else ""))
    print("    No git repo here (the image excludes .git), so no conflict count.")
    print("    Run locally for the real number:  python scripts/upstream_drift.py")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--threshold-days", type=int, default=30,
                    help="stay silent until upstream's newest tag is older than "
                         "this many days (default: 30 — merge monthly)")
    args = ap.parse_args()
    return _full_report(args.threshold_days) if _have_git_repo() \
        else _remote_report(args.threshold_days)


if __name__ == "__main__":
    raise SystemExit(main())
