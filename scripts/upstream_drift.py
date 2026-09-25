#!/usr/bin/env python3
"""Report how far this fork has drifted from upstream — on every run.

WHY THIS EXISTS. Merging upstream v2026.7.20 after ~2 months of drift cost 44
conflicted files, 9 real regressions, and two days. The cost is superlinear in
elapsed time, not in commit count: waiting long enough for upstream to *rewrite*
a file (web_server.py moved +16,446/-4,756 across 312 commits) turns a hunk
resolution into "re-apply our intent onto unfamiliar code", which is where every
serious bug came from. Merging monthly keeps you resolving code you recognise.

IT ALWAYS PRINTS A STATUS LINE. It used to print nothing under the threshold,
and it measured the threshold against upstream's NEWEST tag. Upstream releases
every 3-10 days, so the newest tag was never 30 days old and the watchdog said
nothing from v2026.7.20 to v2026.9.24 — 14 releases, 109 would-be conflicts.
A silent watchdog and a broken one look identical, so now every run reports,
and the age that decides "merge is due" is the OLDEST tag we have not merged.

WHERE IT RUNS (one Telegram message per week, never zero):

  * GitHub Actions — `.github/workflows/upstream-drift.yml`, Mondays. Full mode
    from a full clone, so it carries the real conflict count. Primary.
  * Hermes cron   — no-agent job, Tuesdays, with ``--defer-to-actions``: stays
    silent when that workflow succeeded in the last 72h, otherwise reports
    itself and says the Actions check did not. Fallback.

        hermes cron create '0 9 * * 2' --no-agent --script upstream_drift.sh \
            --name upstream-drift --deliver telegram

TWO MODES, because the production image has no .git (.dockerignore excludes it):

  * full   — run from a clone with an `upstream` remote. Fetches, lists the
             upstream tags we have not merged, and computes the REAL conflict
             count with `git merge-tree` (which writes nothing).
  * remote — no git repo: asks the GitHub API for upstream's tags and compares
             them to UPSTREAM_VERSION. No conflict count.

Exit codes: 0 up to date or merge not yet due, 1 merge is due, 2 the check
itself failed.
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
FORK_REPO = os.environ.get("HERMES_FORK_REPO", "br41s/hermes-sandbox")
ACTIONS_WORKFLOW = "upstream-drift.yml"
RUNBOOK = "tasks/upstream-merge-hygiene.md"
_REPO_ROOT = Path(__file__).resolve().parent.parent
# Upstream cuts same-day re-releases as a fourth component (v2026.8.16.2).
_TAG_RE = re.compile(r"^v(\d{4})\.(\d+)\.(\d+)(?:\.(\d+))?$")


def _version_key(tag: str) -> tuple[int, int, int, int]:
    """Sort key for a vYYYY.M.D[.N] tag.

    MUST be numeric. Sorting these as strings puts v2026.7.7 above v2026.7.30
    because '7' > '3' character-wise — which is exactly what this script got
    wrong on its first run in the container, reporting a tag three weeks stale
    as the newest. ``git tag --sort=-v:refname`` (full mode) already does this
    correctly; only the GitHub API path needed it.
    """
    m = _TAG_RE.match(tag)
    return tuple(int(g or 0) for g in m.groups()) if m else (0, 0, 0, 0)


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


def _api_get(url: str):
    """GET a GitHub API URL as JSON. Raises urllib/json errors to the caller.

    Unauthenticated is fine for public repos (60 req/h). The Hermes cron
    sandbox strips GITHUB_TOKEN from script envs anyway; Actions does not set it.
    """
    req = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json",
                      "User-Agent": "hermes-upstream-drift"},
    )
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _api_tag_date(commit_url: str) -> str:
    try:
        return _api_get(commit_url)["commit"]["committer"]["date"]
    except Exception:
        return ""


def _api_release_tags() -> list[tuple[str, str]] | None:
    """Upstream's release tags as (name, commit_url), newest first."""
    try:
        tags = _api_get(f"https://api.github.com/repos/{UPSTREAM_REPO}/tags?per_page=100")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        print(f"upstream-drift: could not reach the GitHub API: {exc}", file=sys.stderr)
        return None
    found = [(t["name"], (t.get("commit") or {}).get("url", "")) for t in tags
             if isinstance(t, dict) and _TAG_RE.match(t.get("name") or "")]
    return sorted(found, key=lambda t: _version_key(t[0]), reverse=True)


def _days_since(iso: str) -> int | None:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).days


def _status(current: str, unmerged: list[str], oldest_age: int | None,
            threshold: int, detail: list[str]) -> int:
    """Print the status message and return the exit code.

    ``unmerged`` is newest first. The age that decides whether a merge is due
    is the OLDEST unmerged tag's: the newest is almost always days old, which
    is how the previous version of this check stayed quiet for two months.
    """
    if not unmerged:
        print(f"✅ Upstream: up to date — on {current}, upstream's newest release.")
        return 0

    newest, oldest = unmerged[0], unmerged[-1]
    n = len(unmerged)
    due = oldest_age is None or oldest_age >= threshold
    icon = "⚠️ Upstream drift: merge is due" if due else "ℹ️ Upstream: behind, merge not due yet"
    print(f"{icon} — {n} release{'s' if n != 1 else ''} behind.")
    print(f"    We are on {current}; upstream's newest is {newest}.")
    age = f"{oldest_age} days ago" if oldest_age is not None else "on an unknown date"
    print(f"    Oldest unmerged, {oldest}, came out {age} (merge due at {threshold} days).")
    for line in detail:
        print(f"    {line}")
    if due:
        print(f"    Merge runbook: {RUNBOOK}")
    return 1 if due else 0


def _conflict_count(target: str) -> str:
    """Files a merge of ``target`` into HEAD would conflict on, or "?"."""
    try:
        out = subprocess.run(
            ["git", "merge-tree", "--write-tree", "--name-only", "HEAD", target],
            capture_output=True, text=True, cwd=_REPO_ROOT,
        )
    except FileNotFoundError:
        return "?"
    if out.returncode == 0:
        return "0"
    if out.returncode != 1:
        return "?"
    # Exit 1 = conflicts: tree oid, then one conflicted path per line, then a
    # blank line before the informational messages.
    files = []
    for line in out.stdout.splitlines()[1:]:
        if not line.strip():
            break
        files.append(line)
    return str(len(files)) if files else "?"


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
    current = _recorded_version()
    if not current:
        print("upstream-drift: UPSTREAM_VERSION is missing or empty", file=sys.stderr)
        return 2

    # UPSTREAM_VERSION must name a tag actually merged into HEAD. Recording a
    # tag we have NOT merged makes this check report "up to date" through real
    # drift. It is an easy slip: the newest tag is the one on screen while you
    # are doing the merge, and it is exactly the wrong value to write.
    merged = subprocess.run(
        ["git", "merge-base", "--is-ancestor", current, "HEAD"],
        capture_output=True, cwd=_REPO_ROOT,
    ).returncode == 0
    if not merged:
        print(f"❌ UPSTREAM_VERSION says {current}, but that tag is NOT merged "
              f"into HEAD.")
        print("    The drift check is meaningless until this is corrected — it "
              "would report\n    up to date through real drift. Set it to the newest "
              "tag that IS an ancestor of HEAD:")
        print("      for t in $(git tag -l --sort=-v:refname); do "
              "git merge-base --is-ancestor $t HEAD 2>/dev/null && "
              "{ echo $t; break; }; done")
        return 1

    unmerged = [t for t in tags if _version_key(t) > _version_key(current)]
    if not unmerged:
        return _status(current, [], None, threshold, [])

    oldest_date = _run("git", "log", "-1", "--format=%cI", unmerged[-1], check=False)
    behind = _run("git", "rev-list", "--count", f"HEAD..{unmerged[0]}", check=False) or "?"
    detail = [f"{behind} commits behind · ~{_conflict_count(unmerged[0])} files "
              f"would conflict merging {unmerged[0]}"]
    return _status(current, unmerged,
                   _days_since(oldest_date) if oldest_date else None,
                   threshold, detail)


def _remote_report(threshold: int) -> int:
    current = _recorded_version()
    if not current:
        print("upstream-drift: UPSTREAM_VERSION is missing or empty", file=sys.stderr)
        return 2
    tags = _api_release_tags()
    if tags is None:
        return 2
    unmerged = [(name, url) for name, url in tags
                if _version_key(name) > _version_key(current)]
    if not unmerged:
        return _status(current, [], None, threshold, [])
    date = _api_tag_date(unmerged[-1][1])
    return _status(current, [name for name, _ in unmerged],
                   _days_since(date) if date else None, threshold,
                   ["No git repo here, so no conflict count — the Actions "
                    "report carries it."])


def _actions_reported(window_hours: int) -> tuple[bool, str]:
    """Did the drift workflow succeed within ``window_hours``? (answer, why)

    Success means it delivered, because the workflow fails when the Telegram
    send fails. Anything we cannot confirm counts as "did not report": the
    fallback exists for exactly the cases where something is off.
    """
    # No ?status= filter: it was seen answering an empty list once for a
    # workflow with a success on record. Filter locally, and ask twice before
    # believing "no runs" — a false negative costs a duplicate message, but
    # there is no reason to pay it.
    url = (f"https://api.github.com/repos/{FORK_REPO}/actions/workflows/"
           f"{ACTIONS_WORKFLOW}/runs?per_page=20")
    runs: list = []
    for _ in range(2):
        try:
            runs = _api_get(url).get("workflow_runs") or []
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False, f"workflow {ACTIONS_WORKFLOW} not found on {FORK_REPO}"
            return False, f"GitHub API answered HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            return False, f"could not reach the GitHub API: {exc}"
        if runs:
            break
    # Newest first by our own sort: the API's ordering is undocumented.
    ok = sorted((r for r in runs if r.get("conclusion") == "success"),
                key=lambda r: r.get("run_started_at") or r.get("created_at") or "",
                reverse=True)
    if not ok:
        return False, "no successful run on record" if runs else "it has never run"
    started = ok[0].get("run_started_at") or ok[0].get("created_at") or ""
    try:
        dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
    except ValueError:
        return False, f"unreadable run timestamp {started!r}"
    hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    if hours <= window_hours:
        return True, f"succeeded {hours:.0f}h ago"
    return False, f"last success was {started[:10]} ({hours / 24:.0f} days ago)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--threshold-days", type=int, default=30,
                    help="the merge is due once the oldest unmerged upstream "
                         "tag is this many days old (default: 30 — merge monthly)")
    ap.add_argument("--defer-to-actions", action="store_true",
                    help="fallback mode: print nothing if the GitHub Actions "
                         "drift workflow succeeded recently, else report")
    ap.add_argument("--defer-window-hours", type=int, default=72,
                    help="how recent that Actions success must be (default: 72)")
    args = ap.parse_args()

    if args.defer_to_actions:
        reported, why = _actions_reported(args.defer_window_hours)
        if reported:
            return 0  # silent: Actions already delivered this week's report
    rc = _full_report(args.threshold_days) if _have_git_repo() \
        else _remote_report(args.threshold_days)
    if args.defer_to_actions and rc != 2:
        print(f"    Sent by the Hermes fallback: the GitHub Actions drift check "
              f"did not report ({why}).")
    return rc


def _entry() -> int:
    """main(), with any crash turned into exit 2.

    An uncaught exception exits 1 — the same code as "merge is due" — and
    the Hermes wrapper delivers exit 1 as a normal report, so a crash with
    empty stdout would read as a silent run. Exit 2 is "the check broke".
    """
    try:
        return main()
    except Exception:
        import traceback

        traceback.print_exc()
        print("upstream-drift: crashed — see the traceback above", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_entry())
