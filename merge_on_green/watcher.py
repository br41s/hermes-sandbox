"""Merge system-tier PRs the auditor approved, once the CEO has labelled them.

The auditor (``auditor/``, profile ``auditor``) already reviews every open PR on
every repo, merges clean content-tier PRs itself, and for a clean system-tier PR
posts an APPROVE comment and leaves the merge to the CEO. This watcher is that
last, manual step. It merges a PR only when all of these hold:

  1. The CEO labelled it ``auto-merge`` — per-PR consent.
  2. hermes-auditor's APPROVE comment carries the marker
     ``<!-- hermes-auditor:approve <sha> -->`` naming the CURRENT head SHA, so a
     push after the approval voids it.
  3. GitHub reports it ``MERGEABLE`` and no check is failing or pending.
  4. It touches no protected path and passes ``auditor.safety``'s
     mass-deletion floor — the same floor the auditor's own merges use.
  5. The remediation gates allow it: ``HERMES_AUTONOMY`` kill switch,
     per-commit debounce, per-class rate limit.

Authentication is deliberately delegated to ``gh``'s on-disk identity. Cron
subprocesses have ``GITHUB_TOKEN`` stripped tier-1 by ``_sanitize_subprocess_env``
precisely because a container-level token silently overrode ``gh``'s configured
identity once before (the auditor identity leak on biglobster#408). Run under
the ``auditor`` profile, ``gh`` reads the hermes-auditor ``hosts.yml`` that
cont-init §4b writes. Do not reintroduce an env token here.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from remediation import guards, ledger

CLASS_NAME = "merge-on-green"
LABEL = "auto-merge"
AUDITOR_LOGIN = "hermes-auditor"

#: Written by the auditor at the end of a system-tier APPROVE comment (see
#: auditor/auditor.prompt PASO 2f and the auditor SOUL.md). Invisible when
#: rendered; binds the approval to one head SHA. A prefix of at least 7 hex
#: chars is accepted so a truncated SHA still binds to exactly one commit.
APPROVE_MARKER = re.compile(r"<!--\s*hermes-auditor:approve\s+([0-9a-fA-F]{7,40})\s*-->")

#: Read from each repo, so policy lives with the code it protects while the
#: mechanism stays here. Absent or empty file => DEFAULT_PROTECTED.
PROTECTED_PATHS_FILE = ".github/auto-merge-protected-paths.txt"

#: Deliberately not empty: a repo that has not thought about this should still
#: never let automation rewrite its own CI.
DEFAULT_PROTECTED: tuple[str, ...] = (".github/**",)

#: States that do not veto a merge. Anything else — failure, pending, unknown —
#: does. Unknown states are not enumerated on purpose: they block.
_OK_CHECK_STATES = {"SUCCESS", "SKIPPED", "NEUTRAL"}

_GH_TIMEOUT = 60

#: Report kinds, stored as the ledger detail prefix. ``ledger.recently_acted``
#: only sees ``applied`` rows, so it cannot stop a refusal from being repeated
#: every tick; these kinds let ``_reported_recently`` do that instead.
_KIND_BLOCKED = "blocked"   # needs a human (protected path, mass deletion, empty diff)
_KIND_HELD = "held"         # ready, but a gate said no (kill switch, rate limit)
_KIND_ERROR = "error"       # could not read GitHub


class GhError(RuntimeError):
    """A ``gh`` invocation failed or returned something unparseable."""


Runner = Callable[[Sequence[str]], str]
SafetyCheck = Callable[[str, int], Tuple[bool, str]]


def _default_runner(args: Sequence[str]) -> str:
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment specific
        raise GhError(
            "gh CLI not found. This watcher authenticates through gh's on-disk "
            "identity; install it and run `gh auth login` on the host."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise GhError(f"gh timed out after {_GH_TIMEOUT}s: {' '.join(args)}") from exc
    if proc.returncode != 0:
        raise GhError((proc.stderr or proc.stdout or "").strip() or f"gh failed: {args}")
    return proc.stdout


def gh_json(args: Sequence[str], *, runner: Optional[Runner] = None):
    raw = (runner or _default_runner)(args)
    try:
        return json.loads(raw or "null")
    except json.JSONDecodeError as exc:
        raise GhError(f"gh returned non-JSON for {' '.join(args)}: {raw[:200]!r}") from exc


def _default_safety(repo: str, number: int) -> Tuple[bool, str]:
    # Lazy: auditor.safety shells out to gh itself, and nothing else here needs it.
    from auditor.safety import check_mass_deletion

    return check_mass_deletion(repo, number)


# --------------------------------------------------------------------------
# Path guarding
# --------------------------------------------------------------------------

def glob_to_regex(pattern: str) -> re.Pattern:
    """Translate a gitignore-ish glob into an anchored regex.

    ``**/`` matches any directory prefix including none, a trailing ``/**``
    matches everything beneath, and a lone ``*`` stops at a path separator so
    ``**/requirements*.txt`` cannot reach into a subdirectory.
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append(r"(?:.*/)?")
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == len(pattern):
            out.append(r"/.*")
            i += 3
        elif pattern[i] == "*":
            out.append(r"[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def parse_protected(text: str) -> list[tuple[str, re.Pattern]]:
    """Parse a protected-paths file: one glob per line, ``#`` comments ignored."""
    out = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append((stripped, glob_to_regex(stripped)))
    return out


def blocked_paths(
    changed: Iterable[str], patterns: Sequence[tuple[str, re.Pattern]]
) -> list[tuple[str, str]]:
    """Return ``(path, matched_pattern)`` for every changed file that is protected."""
    return [
        (path, raw)
        for path in changed
        for raw, rx in patterns
        if rx.match(path)
    ]


def protected_patterns(repo: str, *, runner: Optional[Runner] = None):
    """The repo's own protected-path list, or ``DEFAULT_PROTECTED`` without one."""
    default = parse_protected("\n".join(DEFAULT_PROTECTED))
    try:
        blob = gh_json(
            ["api", f"repos/{repo}/contents/{PROTECTED_PATHS_FILE}", "--jq", ".content"],
            runner=runner,
        )
    except GhError:
        return default
    if not blob:
        return default
    try:
        text = base64.b64decode(blob).decode("utf-8", errors="replace")
    except Exception:
        return default
    return parse_protected(text) or default


# --------------------------------------------------------------------------
# PR inspection
# --------------------------------------------------------------------------

def candidate_prs(repo: str, *, runner: Optional[Runner] = None) -> list[dict]:
    """Open, non-draft PRs carrying the opt-in label.

    The label is the CEO's consent: applying one needs write access to the repo,
    so an outside contributor cannot opt their own PR in.
    """
    rows = gh_json(
        [
            "pr", "list", "--repo", repo, "--state", "open",
            "--label", LABEL, "--limit", "50",
            "--json", "number,isDraft,headRefOid,author",
        ],
        runner=runner,
    ) or []
    return [r for r in rows if not r.get("isDraft")]


def pr_details(repo: str, number: int, *, runner: Optional[Runner] = None) -> dict:
    return gh_json(
        ["pr", "view", str(number), "--repo", repo,
         "--json", "headRefOid,mergeable,comments"],
        runner=runner,
    ) or {}


def auditor_approved(comments: Iterable[dict], head_sha: str) -> bool:
    """True if hermes-auditor approved exactly this head commit.

    Only the auditor's own comments count. On a public repo anyone can comment,
    so a marker written by any other account is ignored — otherwise approving a
    PR would be one pasted line away.
    """
    head = (head_sha or "").lower()
    if not head:
        return False
    for comment in comments or []:
        if ((comment.get("author") or {}).get("login")) != AUDITOR_LOGIN:
            continue
        for match in APPROVE_MARKER.finditer(comment.get("body") or ""):
            if head.startswith(match.group(1).lower()):
                return True
    return False


def checks_ok(repo: str, number: int, *, runner: Optional[Runner] = None) -> tuple[bool, str]:
    """No check may be failing or pending. Zero checks passes.

    GitHub Actions never runs on this account, so most repos report no checks
    at all — the auditor's approval is the gate here, and checks are a veto
    only. That matches the auditor SOUL's CI/status gate: any FAILING check is a
    merge blocker.
    """
    try:
        rows = gh_json(
            ["pr", "checks", str(number), "--repo", repo, "--json", "name,state"],
            runner=runner,
        ) or []
    except GhError as exc:
        # `gh pr checks` exits non-zero when a PR has no checks at all.
        if "no checks reported" in str(exc).lower():
            return True, "no checks reported"
        return False, f"could not read checks ({exc})"
    if not rows:
        return True, "no checks reported"
    bad = [f"{r.get('name')}={r.get('state')}" for r in rows
           if str(r.get("state", "")).upper() not in _OK_CHECK_STATES]
    if bad:
        return False, "not green: " + ", ".join(sorted(bad))
    return True, f"{len(rows)} check(s) green"


def changed_files(repo: str, number: int, *, runner: Optional[Runner] = None) -> list[str]:
    raw = (runner or _default_runner)(
        ["pr", "diff", str(number), "--repo", repo, "--name-only"]
    )
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]


def merge_pr(repo: str, number: int, *, runner: Optional[Runner] = None) -> tuple[bool, str]:
    try:
        (runner or _default_runner)(
            ["pr", "merge", str(number), "--repo", repo, "--squash", "--delete-branch"]
        )
    except GhError as exc:
        return False, str(exc)
    return True, "merged"


# --------------------------------------------------------------------------
# Ledger helpers
# --------------------------------------------------------------------------

def _signature(repo: str, number, head_sha: str) -> str:
    """Identity of one merge opportunity: a new push is a new opportunity."""
    return f"{repo}#{number}@{(head_sha or 'unknown')[:12]}"


def _entry(signature: str, target: str, event: str, outcome: str, detail: str,
           now: Optional[datetime]) -> ledger.LedgerEntry:
    return ledger.make_entry(CLASS_NAME, signature, target, "auto", event,
                             outcome=outcome, detail=detail, now=now)


def _reported_recently(signature: str, kind: str, entries: Sequence[ledger.LedgerEntry],
                       now: Optional[datetime]) -> bool:
    """True if this outcome was already reported for this commit in the window.

    Uses ``proposed`` rows, which neither the debounce nor the rate limit count,
    so remembering a refusal never consumes merge budget.
    """
    cutoff = (now or ledger._now()) - timedelta(hours=ledger.DEBOUNCE_HOURS)
    for e in entries:
        if (e.cls != CLASS_NAME or e.event != ledger.EVENT_PROPOSED
                or e.signature != signature or not str(e.detail).startswith(kind + ":")):
            continue
        ts = ledger._parse_iso(e.ts)
        if ts is not None and ts >= cutoff:
            return True
    return False


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def process_repo(
    repo: str,
    *,
    runner: Optional[Runner] = None,
    safety_check: Optional[SafetyCheck] = None,
    dry_run: bool = False,
    verbose: bool = False,
    ledger_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    env: Optional[dict] = None,
) -> list[str]:
    """Evaluate one repo. Returns report lines; an empty list means silence."""
    safety_check = safety_check or _default_safety
    entries: List[ledger.LedgerEntry] = ledger.read(path=ledger_path)
    report: list[str] = []

    def say_once(signature: str, target: str, kind: str, message: str) -> None:
        # Manual runs (--dry-run / --verbose) always show everything; the cron
        # lane says each outcome once per commit per window, then stays quiet.
        if not (dry_run or verbose) and _reported_recently(signature, kind, entries, now):
            return
        report.append(f"{target}: {message}")
        if not dry_run:
            row = _entry(signature, target, ledger.EVENT_PROPOSED,
                         ledger.OUTCOME_FAILURE, f"{kind}: {message}", now)
            ledger.append(row, path=ledger_path)
            entries.append(row)

    def note(target: str, message: str) -> None:
        if verbose:
            report.append(f"{target}: {message}")

    try:
        prs = candidate_prs(repo, runner=runner)
    except GhError as exc:
        say_once(f"{repo}#list", repo, _KIND_ERROR, f"cannot list PRs — {exc}")
        return report

    for pr in prs:
        number = pr.get("number")
        target = f"{repo}#{number}"

        if ((pr.get("author") or {}).get("login")) == AUDITOR_LOGIN:
            # Separation of duties: the reviewer never lands its own work.
            note(target, f"skipped — authored by {AUDITOR_LOGIN}")
            continue

        try:
            details = pr_details(repo, number, runner=runner)
        except GhError as exc:
            say_once(_signature(repo, number, pr.get("headRefOid", "")), target,
                     _KIND_ERROR, f"cannot read PR — {exc}")
            continue

        head = details.get("headRefOid") or pr.get("headRefOid", "")
        sig = _signature(repo, number, head)

        if ledger.recently_acted(sig, entries=entries, now=now):
            note(target, "skipped — already acted on this commit")
            continue

        if not auditor_approved(details.get("comments"), head):
            note(target, f"waiting — no {AUDITOR_LOGIN} approval at {head[:7]}")
            continue

        mergeable = str(details.get("mergeable") or "UNKNOWN").upper()
        if mergeable != "MERGEABLE":
            note(target, f"waiting — mergeable={mergeable}")
            continue

        ok, check_detail = checks_ok(repo, number, runner=runner)
        if not ok:
            note(target, f"waiting — {check_detail}")
            continue

        try:
            files = changed_files(repo, number, runner=runner)
        except GhError as exc:
            say_once(sig, target, _KIND_ERROR, f"cannot read diff — {exc}")
            continue
        if not files:
            say_once(sig, target, _KIND_BLOCKED, "needs a human — diff reported no files")
            continue

        hits = blocked_paths(files, protected_patterns(repo, runner=runner))
        if hits:
            shown = ", ".join(f"{p} ({raw})" for p, raw in hits[:3])
            more = "" if len(hits) <= 3 else f" +{len(hits) - 3} more"
            say_once(sig, target, _KIND_BLOCKED,
                     f"needs a human — touches protected paths: {shown}{more}")
            continue

        safe, why = safety_check(repo, number)
        if not safe:
            say_once(sig, target, _KIND_BLOCKED, f"needs a human — {why}")
            continue

        gate = guards.may_auto_act(CLASS_NAME, sig, entries=entries, now=now, env=env)
        if not gate.allowed:
            say_once(sig, target, _KIND_HELD, f"ready but not merged — {gate.reason}")
            continue

        if dry_run:
            report.append(f"{target}: WOULD MERGE — auditor-approved at {head[:7]}, "
                          f"{check_detail}, {len(files)} file(s)")
            continue

        merged, why = merge_pr(repo, number, runner=runner)
        row = _entry(sig, target, ledger.EVENT_APPLIED,
                     ledger.OUTCOME_SUCCESS if merged else ledger.OUTCOME_FAILURE,
                     why, now)
        ledger.append(row, path=ledger_path)
        # Same-tick bookkeeping: without this, five ready PRs in one tick would
        # all see the pre-tick rate count and all merge past the limit.
        entries.append(row)
        report.append(
            f"{target}: merged — auditor-approved at {head[:7]}, {check_detail}"
            if merged else f"{target}: merge failed — {why}"
        )

    return report


def load_repos() -> list[str]:
    """The auditor's own review set: every docker/profiles/*/repos.txt + the engine."""
    from auditor.pending import review_repos

    return list(review_repos())


def run(
    repos: Optional[Sequence[str]] = None,
    *,
    runner: Optional[Runner] = None,
    safety_check: Optional[SafetyCheck] = None,
    dry_run: bool = False,
    verbose: bool = False,
    ledger_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    env: Optional[dict] = None,
) -> list[str]:
    """Evaluate every repo and return the combined report lines."""
    repos = list(repos) if repos is not None else load_repos()
    if not repos:
        return ["merge-on-green: no repos to watch (auditor review set is empty)"]
    out: list[str] = []
    for repo in repos:
        out.extend(process_repo(
            repo, runner=runner, safety_check=safety_check, dry_run=dry_run,
            verbose=verbose, ledger_path=ledger_path, now=now, env=env,
        ))
    return out
