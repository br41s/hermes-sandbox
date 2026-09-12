"""List open PRs the auditor has not yet reviewed at their current head SHA.

The auditor agent cron runs this, reviews each returned PR, then records the
review with ``--mark`` so the same head isn't re-reviewed. A PR reappears here
the moment its author pushes a new commit (head SHA changes) — which is exactly
how the back-and-forth works: auditor comments, author addresses, new SHA,
auditor re-reviews.

Dedup mirrors the incident watcher (``incidents/sweep.py``): a bounded ``seen``
list of stable ids, persisted to ``$HERMES_HOME/auditor/state.json``. The id is
``"<repo>#<number>@<headSha>"`` — repo-qualified so the same PR number on two
different repos can never collide — and keyed to the exact reviewed tree.

Multi-repo (Phase 4): with no ``--repo`` the auditor reviews the union of every
``docker/profiles/*/repos.txt`` plus the engine repo (see ``review_repos()``).
Each returned PR carries its ``repo`` slug; every ``gh`` call the agent then makes
must pass ``--repo`` for that slug. Review is API-only — no local clones of the
profile repos are needed (or made) for this.

PR data comes from ``gh pr list`` (no GitHub Actions involved — this is plain
API polling, the only path available on a Free private account).

The CLI payload is deliberately COMPACT: the agent gets the fields it reviews
with (including a pre-computed ``tier``) and not ``gh``'s per-file blob, which
overran the terminal output cap and came back truncated — see ``compact()``.

CLI:
    python -m auditor.pending                      # PRs needing review, ALL repos
    python -m auditor.pending --repo owner/name     # one repo only
    python -m auditor.pending --limit 5             # cap the queue (default 10)
    python -m auditor.pending --raw                 # full gh objects, for humans
    python -m auditor.pending --mark 42 <sha> --repo owner/name   # record reviewed
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from auditor.tiers import classify

_SEEN_CAP = 2000
# Fields pulled per PR. ``files`` lets us tier in-process, without a second call.
_PR_FIELDS = "number,title,headRefName,headRefOid,author,isDraft,files,url"

# Per-run review cap. The agent loop stops at ``max_iterations`` (90, see
# run_agent.py); a PR costs roughly 6-8 iterations, so an uncapped list cannot
# finish and the run dies mid-review having marked only some of it. Cap the
# queue instead, and let the next run take the rest.
DEFAULT_LIMIT = 10
# Per-PR path cap in the CLI payload. Tiering already happened in-process, so
# the paths are only context for the agent; a 400-file PR must not blow the
# terminal output cap (tools/tool_output_limits.py) and get truncated.
_MAX_PATHS_SHOWN = 25

# Liveness: the auditor's Telegram channel (incidents thread) carries only
# escalations. To prove the gate is still running on quiet days, it emits one
# heartbeat per this window — mirrors the incident watcher (incidents/sweep.py).
HEARTBEAT_HOURS = 24


ENGINE_REPO = "br41s/hermes-sandbox"


def _state_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "auditor" / "state.json"


def _repo_root() -> Path:
    # auditor/pending.py -> repo root that holds auditor/ and docker/.
    return Path(__file__).resolve().parents[1]


def review_repos() -> List[str]:
    """The union repo set the auditor reviews: every ``docker/profiles/*/repos.txt``
    entry plus the engine repo. Read from the auditor's own engine clone, so the
    review set is itself version-controlled and reviewed. Always includes
    ``ENGINE_REPO`` even if a repos.txt is missing."""
    repos = {ENGINE_REPO}
    profiles = _repo_root() / "docker" / "profiles"
    if profiles.is_dir():
        for f in sorted(profiles.glob("*/repos.txt")):
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.split("#", 1)[0].strip()
                if line and "/" in line:
                    repos.add(line)
    return sorted(repos)


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _pr_id(repo: str, number: int, head_sha: str) -> str:
    # Repo-qualified: biglobster#5 and FinView#5 must never collide.
    return f"{repo}#{number}@{head_sha}"


def _gh_list_open_prs(repo: Optional[str]) -> List[dict]:
    """Return open PRs via ``gh``. Empty list if gh fails (degrade quietly)."""
    cmd = ["gh", "pr", "list", "--state", "open", "--limit", "100", "--json", _PR_FIELDS]
    if repo:
        cmd += ["--repo", repo]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, check=True
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"auditor.pending: gh pr list failed: {e}", file=sys.stderr)
        return []
    try:
        return json.loads(out) or []
    except ValueError:
        return []


def pending_prs(repo: Optional[str], state_path: Path, include_drafts: bool = False) -> List[dict]:
    """Open PRs whose current head SHA has not been reviewed yet.

    With ``repo`` set, only that repo is polled; with ``repo`` ``None`` the whole
    ``review_repos()`` union is polled. Each item is the ``gh`` PR object plus its
    ``repo`` slug and a flat ``changed_files`` list (paths) for convenient tiering
    by the caller.
    """
    state = _load_state(state_path)
    seen = set(state.get("seen", []))
    repos = [repo] if repo else review_repos()
    result = []
    for r in repos:
        for pr in _gh_list_open_prs(r):
            if pr.get("isDraft") and not include_drafts:
                continue
            number = pr.get("number")
            head = pr.get("headRefOid") or ""
            if number is None or not head:
                continue
            if _pr_id(r, number, head) in seen:
                continue
            pr["repo"] = r
            pr["changed_files"] = [f.get("path") for f in (pr.get("files") or []) if f.get("path")]
            # Tier here, not in the agent. Shelling out to ``classify`` needs
            # ``python -c``, which cron blocks unconditionally (no approver) —
            # the auditor livelocked on exactly that for 90 iterations a run.
            pr["tier"] = classify(pr["changed_files"], r)
            result.append(pr)
    # Deterministic, oldest-first: a PR can never be starved by newer arrivals.
    result.sort(key=lambda p: (p["repo"], p["number"]))
    return result


def compact(pr: dict) -> dict:
    """The slim view the agent actually needs to review one PR.

    ``gh``'s raw PR object carries a full per-file blob (path + additions +
    deletions each). For a normal pending queue that JSON overran the terminal
    output cap and came back truncated mid-array, so the agent re-ran
    ``auditor.pending`` dozens of times trying to get a parseable list. Every
    field here is one the agent uses; ``files`` is not one of them.
    """
    paths = pr.get("changed_files") or []
    out = {
        "repo": pr.get("repo"),
        "number": pr.get("number"),
        "title": pr.get("title"),
        "headRefName": pr.get("headRefName"),
        "headRefOid": pr.get("headRefOid"),
        "author": (pr.get("author") or {}).get("login"),
        "tier": pr.get("tier"),
        "changed_files_count": len(paths),
        "changed_files": paths[:_MAX_PATHS_SHOWN],
        "url": pr.get("url"),
    }
    if len(paths) > _MAX_PATHS_SHOWN:
        out["changed_files_truncated"] = True
    return out


def mark_reviewed(repo: str, number: int, head_sha: str, state_path: Path) -> None:
    """Record PR ``repo#number`` at ``head_sha`` as reviewed (bounded ``seen`` list)."""
    state = _load_state(state_path)
    seen_list: list = list(state.get("seen", []))
    pid = _pr_id(repo, number, head_sha)
    if pid not in seen_list:
        seen_list.append(pid)
    state["seen"] = seen_list[-_SEEN_CAP:]
    _save_state(state_path, state)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    try:
        ts = datetime.fromisoformat(value) if value else None
    except (ValueError, TypeError):
        return None
    if ts is None:
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _heartbeat_line(now: datetime) -> str:
    return (
        f"✅ Hermes auditor: still running, no PRs needed escalation in the last "
        f"{HEARTBEAT_HOURS}h (as of {now.strftime('%Y-%m-%d %H:%M UTC')})."
    )


def heartbeat(state_path: Path, *, now: Optional[datetime] = None,
              touch_only: bool = False) -> str:
    """Emit-and-record a liveness heartbeat iff one is due (>= HEARTBEAT_HOURS).

    Returns the heartbeat line when due (and records the clock), else "".
    ``touch_only`` resets the clock without emitting — call it after delivering
    an escalation so its delivery already proves liveness (mirrors the watcher,
    where any output resets the heartbeat clock).
    """
    now = now or _now()
    state = _load_state(state_path)
    last = _parse_iso(state.get("last_heartbeat_at"))
    due = last is None or (now - last) >= timedelta(hours=HEARTBEAT_HOURS)
    if touch_only:
        state["last_heartbeat_at"] = now.isoformat()
        _save_state(state_path, state)
        return ""
    if not due:
        return ""
    state["last_heartbeat_at"] = now.isoformat()
    _save_state(state_path, state)
    return _heartbeat_line(now)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="List PRs the auditor must review.")
    ap.add_argument("--repo", help="owner/name (default: gh infers from cwd)")
    ap.add_argument("--include-drafts", action="store_true")
    ap.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT,
        help=f"max PRs to return (default {DEFAULT_LIMIT}); the rest wait for the next run",
    )
    ap.add_argument(
        "--raw", action="store_true",
        help="emit the full gh objects instead of the compact review payload",
    )
    ap.add_argument(
        "--mark", nargs=2, metavar=("NUMBER", "SHA"),
        help="record a PR head as reviewed instead of listing",
    )
    ap.add_argument(
        "--heartbeat", action="store_true",
        help="print the 24h liveness line iff one is due (else nothing), and record it",
    )
    ap.add_argument(
        "--heartbeat-touch", action="store_true",
        help="reset the heartbeat clock without printing (call after delivering an escalation)",
    )
    args = ap.parse_args(argv)
    state_path = _state_path()

    if args.mark:
        number, sha = args.mark
        repo = args.repo or ENGINE_REPO
        mark_reviewed(repo, int(number), sha, state_path)
        print(f"marked {repo}#{number} @ {sha} reviewed")
        return 0

    if args.heartbeat or args.heartbeat_touch:
        line = heartbeat(state_path, touch_only=args.heartbeat_touch)
        if line:
            print(line)
        return 0

    prs = pending_prs(args.repo, state_path, include_drafts=args.include_drafts)
    total = len(prs)
    if args.limit > 0:
        prs = prs[: args.limit]
    if args.raw:
        print(json.dumps(prs, indent=2))
        return 0
    payload = {
        "count": len(prs),
        "total_pending": total,
        "deferred_to_next_run": max(0, total - len(prs)),
        "prs": [compact(p) for p in prs],
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
