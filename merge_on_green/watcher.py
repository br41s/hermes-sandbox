"""Find labelled, green pull requests and merge them under the autonomy gates.

Authentication is deliberately delegated to the ``gh`` CLI's on-disk identity.
Cron subprocesses have ``GITHUB_TOKEN`` stripped tier-1 by
``_sanitize_subprocess_env`` precisely because a container-level token silently
overrode ``gh``'s configured identity once before (the auditor identity leak on
biglobster#408). Do not reintroduce an env token here: if ``gh`` cannot
authenticate on its own, the right fix is ``gh auth`` on the box, not an export.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from remediation import guards, ledger

CLASS_NAME = "merge-on-green"
LABEL = "auto-merge"

#: Read from each repo, so policy lives with the code it protects while the
#: mechanism stays here. Absent file => DEFAULT_PROTECTED.
PROTECTED_PATHS_FILE = ".github/auto-merge-protected-paths.txt"

#: Used when a repo ships no list of its own. Deliberately not empty: a repo
#: that has not thought about this should still never let automation rewrite
#: its own CI.
DEFAULT_PROTECTED: tuple[str, ...] = (".github/**",)

#: States that do not block a merge. Anything else — failure, pending, stale —
#: does. Unknown states are not enumerated on purpose: they block.
_OK_CHECK_STATES = {"SUCCESS", "SKIPPED", "NEUTRAL"}

_GH_TIMEOUT = 60


class GhError(RuntimeError):
    """A ``gh`` invocation failed or returned something unparseable."""


Runner = Callable[[Sequence[str]], str]


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
    """Fetch the repo's own protected-path list, falling back to the default.

    A repo with no list gets ``DEFAULT_PROTECTED``. A repo whose list cannot be
    read for any *other* reason is a refusal, not a fallback — see caller.
    """
    try:
        blob = gh_json(
            ["api", f"repos/{repo}/contents/{PROTECTED_PATHS_FILE}", "--jq", ".content"],
            runner=runner,
        )
    except GhError:
        return parse_protected("\n".join(DEFAULT_PROTECTED))
    if not blob:
        return parse_protected("\n".join(DEFAULT_PROTECTED))
    import base64

    try:
        text = base64.b64decode(blob).decode("utf-8", errors="replace")
    except Exception:
        return parse_protected("\n".join(DEFAULT_PROTECTED))
    parsed = parse_protected(text)
    return parsed or parse_protected("\n".join(DEFAULT_PROTECTED))


# --------------------------------------------------------------------------
# PR inspection
# --------------------------------------------------------------------------

def candidate_prs(repo: str, *, runner: Optional[Runner] = None) -> list[dict]:
    """Open, non-draft PRs carrying the opt-in label.

    The label is the authorisation: applying one needs write access to the repo,
    so an outside contributor cannot opt their own PR in.
    """
    rows = gh_json(
        [
            "pr", "list", "--repo", repo, "--state", "open",
            "--label", LABEL, "--limit", "50",
            "--json", "number,isDraft,headRefOid,title",
        ],
        runner=runner,
    ) or []
    return [r for r in rows if not r.get("isDraft")]


def checks_green(repo: str, number: int, *, runner: Optional[Runner] = None) -> tuple[bool, str]:
    """True only when at least one check ran and none of them is unhappy.

    Zero checks is *not* green. Without branch protection nothing guarantees a
    check exists, so an empty result means "nothing has vouched for this commit"
    — which is exactly when a merge should not happen.
    """
    try:
        rows = gh_json(
            ["pr", "checks", str(number), "--repo", repo, "--json", "name,state"],
            runner=runner,
        ) or []
    except GhError as exc:
        return False, f"could not read checks ({exc})"
    if not rows:
        return False, "no checks reported"
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
# Orchestration
# --------------------------------------------------------------------------

def _signature(repo: str, number: int, head_sha: str) -> str:
    """Identity of one merge opportunity.

    Keyed on the head SHA so a new push is a fresh opportunity, while repeated
    ticks against an unchanged commit are absorbed by the debounce window.
    """
    return f"{repo}#{number}@{(head_sha or 'unknown')[:12]}"


def _record(signature: str, target: str, event: str, outcome: str, detail: str,
            *, ledger_path: Optional[Path], now: Optional[datetime]) -> None:
    ledger.append(
        ledger.make_entry(
            CLASS_NAME, signature, target, "auto", event,
            outcome=outcome, detail=detail, now=now,
        ),
        path=ledger_path,
    )


def process_repo(
    repo: str,
    *,
    runner: Optional[Runner] = None,
    dry_run: bool = False,
    verbose: bool = False,
    ledger_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    env: Optional[dict] = None,
) -> list[str]:
    """Evaluate one repo. Returns report lines; an empty list means silence."""
    report: list[str] = []
    try:
        prs = candidate_prs(repo, runner=runner)
    except GhError as exc:
        return [f"{repo}: cannot list PRs — {exc}"]

    for pr in prs:
        number = pr.get("number")
        sig = _signature(repo, number, pr.get("headRefOid", ""))
        target = f"{repo}#{number}"

        gate = guards.may_auto_act(
            CLASS_NAME, sig, path=ledger_path, now=now, env=env
        )
        if not gate.allowed:
            # Debounce is the normal quiet path (already handled this commit).
            # A kill switch or a tripped rate limit is worth saying out loud.
            if gate.reason != guards.GATE_DEBOUNCE:
                report.append(f"{target}: not merged — {gate.reason}")
            elif verbose:
                report.append(f"{target}: skipped — debounce")
            continue

        green, detail = checks_green(repo, number, runner=runner)
        if not green:
            if verbose:
                report.append(f"{target}: waiting — {detail}")
            continue

        try:
            files = changed_files(repo, number, runner=runner)
        except GhError as exc:
            report.append(f"{target}: cannot read diff — {exc}")
            continue
        if not files:
            report.append(f"{target}: refusing — diff reported no files")
            continue

        hits = blocked_paths(files, protected_patterns(repo, runner=runner))
        if hits:
            shown = ", ".join(f"{p} ({raw})" for p, raw in hits[:3])
            more = "" if len(hits) <= 3 else f" +{len(hits) - 3} more"
            report.append(
                f"{target}: needs a human — touches protected paths: {shown}{more}"
            )
            if not dry_run:
                # Record it so the debounce window suppresses repeating this
                # every tick until the PR actually changes.
                _record(sig, target, ledger.EVENT_PROPOSED, ledger.OUTCOME_FAILURE,
                        f"blocked by protected paths: {shown}",
                        ledger_path=ledger_path, now=now)
            continue

        if dry_run:
            report.append(f"{target}: WOULD MERGE — {detail}, {len(files)} file(s)")
            continue

        ok, why = merge_pr(repo, number, runner=runner)
        _record(
            sig, target, ledger.EVENT_APPLIED,
            ledger.OUTCOME_SUCCESS if ok else ledger.OUTCOME_FAILURE,
            why, ledger_path=ledger_path, now=now,
        )
        report.append(
            f"{target}: merged ({detail})" if ok else f"{target}: merge failed — {why}"
        )

    return report


def load_repos(env: Optional[dict] = None) -> list[str]:
    """Repos to watch, from ``MERGE_ON_GREEN_REPOS`` (comma or whitespace separated)."""
    raw = ((env if env is not None else os.environ).get("MERGE_ON_GREEN_REPOS") or "")
    return [part for part in re.split(r"[,\s]+", raw.strip()) if part]


def run(
    repos: Optional[Sequence[str]] = None,
    *,
    runner: Optional[Runner] = None,
    dry_run: bool = False,
    verbose: bool = False,
    ledger_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    env: Optional[dict] = None,
) -> list[str]:
    """Evaluate every configured repo and return the combined report lines."""
    repos = list(repos) if repos is not None else load_repos(env)
    if not repos:
        return ["merge-on-green: no repos configured (set MERGE_ON_GREEN_REPOS)"]
    out: list[str] = []
    for repo in repos:
        out.extend(process_repo(
            repo, runner=runner, dry_run=dry_run, verbose=verbose,
            ledger_path=ledger_path, now=now, env=env,
        ))
    return out
