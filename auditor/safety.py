"""Merge-time safety net for the auditor's content auto-merge.

``gh pr merge`` is server-side, so it BYPASSES the local pre-commit git guard
(``scripts/git-guard``) that blocks mass deletions — those hooks only fire when an
agent commits locally. A PR authored without them (the GitHub web UI, an unhooked
clone) could carry a mass asset deletion that auto-merge would land straight on a
live site — the 2026-06 cover-wipe class of incident. So before auto-merging ANY
content PR, the auditor re-checks the PR's file list via the GitHub API and refuses
(escalates instead) if it removes more files than the threshold. This mirrors
``scripts/git-guard/check-mass-deletion.sh`` (default 10, env
``HERMES_MASS_DELETION_LIMIT``).

This is a floor, not the full asset-integrity check (broken-reference validation
needs the repo content); it specifically closes the mass-deletion hole that local
hooks cannot cover on a server-side merge. Fail-safe: if the file list can't be
fetched, the answer is "not safe to auto-merge" (escalate / human-merge).

CLI:
    python -m auditor.safety --repo owner/name --number 42
Prints a one-line reason; exit 0 = safe to auto-merge, exit 1 = do NOT auto-merge.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import List, Optional, Tuple

DEFAULT_DELETION_LIMIT = 10


def _limit() -> int:
    try:
        return int(os.environ.get("HERMES_MASS_DELETION_LIMIT", DEFAULT_DELETION_LIMIT))
    except (ValueError, TypeError):
        return DEFAULT_DELETION_LIMIT


def _pr_file_statuses(repo: str, number: int) -> Optional[list[str]]:
    """Per-file status strings ('added'|'removed'|'modified'|'renamed') for a PR.

    Uses the REST ``pulls/{n}/files`` endpoint (the ``--json files`` view from
    ``gh pr`` omits status, so it can't tell a deletion from an emptied file).
    ``--paginate`` + ``--jq`` streams one status per line across all pages, so it
    is correct even for a removal larger than one API page. Returns ``None`` on any
    failure so the caller can fail safe.
    """
    cmd = [
        "gh", "api", "--paginate",
        f"repos/{repo}/pulls/{number}/files",
        "--jq", ".[].status",
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, check=True
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"auditor.safety: gh api failed: {e}", file=sys.stderr)
        return None
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def check_mass_deletion(repo: str, number: int) -> tuple[bool, str]:
    """Return ``(safe, reason)``. ``safe`` is False if the PR removes more than the
    threshold, or if the file list could not be fetched (fail-safe)."""
    statuses = _pr_file_statuses(repo, number)
    if statuses is None:
        return False, (
            "could not fetch PR files to run the mass-deletion safety check — "
            "not auto-merging (escalate / merge by hand)"
        )
    removed = sum(1 for s in statuses if s == "removed")
    limit = _limit()
    if removed > limit:
        return False, (
            f"⚠️ mass-deletion guard: PR removes {removed} files (limit {limit}). "
            "Server-side merge bypasses the pre-commit guard — do NOT auto-merge; "
            "escalate for a human merge."
        )
    return True, f"ok: {removed} file(s) removed (limit {limit}) — safe to auto-merge"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Merge-time mass-deletion safety check for content auto-merge."
    )
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--number", type=int, required=True, help="PR number")
    args = ap.parse_args(argv)
    safe, reason = check_mass_deletion(args.repo, args.number)
    print(reason)
    return 0 if safe else 1


if __name__ == "__main__":
    raise SystemExit(main())
