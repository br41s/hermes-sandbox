"""Pre-fix safety net for the ``shared-clone-branch-confusion`` remediation.

The fix this guards runs ``git reset --hard origin/main`` inside a *shared* clone
(``/opt/data/<profile>``, used by more than one agent — the SEO / Gap-Hunter
hazard). That command is the EXACT 2026-06-22 BigLobster cover-wipe class of
incident: a blind git operation that discarded 48 tracked images. A reset can
destroy two kinds of work that exist only in the clone — uncommitted changes and
unpushed local commits — so it can never be run blind.

Before the reset is allowed, ``assess_reset`` proves the clone has **nothing
unique to lose**: no uncommitted tracked changes, no commits ahead of
``origin/main``, and no mass file removal above the threshold. If any check
fails (or any git command errors), the verdict is *unsafe* and the caller MUST
refuse and escalate to a human. Under a clean verdict the reset is a
non-destructive realignment of a clone that merely drifted onto the wrong
branch — which is what admits this otherwise-destructive class to Tier-0.

Mirrors ``auditor/safety.py`` and ``scripts/git-guard/check-mass-deletion.sh``:
default deletion limit 10, overridable via ``HERMES_MASS_DELETION_LIMIT``.
Hermetic by injection — every entry point takes a ``git`` runner so tests touch
no real repo, clock, or network.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

# A git runner: (args, cwd) -> (returncode, stdout, stderr). Injected in tests.
GitRunner = Callable[[list[str], str], tuple[int, str, str]]

DEFAULT_DELETION_LIMIT = 10

# Identity re-pinned in the clone after a successful realign. Matches the
# convention in scripts/onboard-profile.sh (the "*@agent.local" identity the
# memory note calls out). Repo-local config, not --global.
AGENT_EMAIL = "hermes@agent.local"
AGENT_NAME = "Hermes Agent"


def _limit() -> int:
    try:
        return int(os.environ.get("HERMES_MASS_DELETION_LIMIT", DEFAULT_DELETION_LIMIT))
    except (ValueError, TypeError):
        return DEFAULT_DELETION_LIMIT


def _git(args: list[str], cwd: str) -> tuple[int, str, str]:
    """Default runner: run ``git -C <cwd> <args>``. Any failure to even launch
    git is returned as a non-zero rc so callers fail safe (refuse)."""
    try:
        p = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=120, check=False
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return 1, "", str(e)


@dataclass(frozen=True)
class ResetSafety:
    """Verdict of the pre-fix net. ``safe`` False => caller must NOT reset."""
    safe: bool
    reason: str
    files_lost: int = 0   # tracked files reset --hard would remove (cover-wipe metric)
    uncommitted: int = 0  # tracked working-tree changes the reset would discard
    ahead: int = 0        # commits ahead of origin/main (unpushed work)


def assess_reset(clone_dir: str, *, limit: Optional[int] = None,
                 git: GitRunner = _git) -> ResetSafety:
    """Decide whether ``git reset --hard origin/main`` is safe in ``clone_dir``.

    Safe ONLY if the clone has nothing unique to lose. Any git error => unsafe
    (fail-safe). Call this *after* the target branch is checked out so HEAD is the
    branch that will be reset.
    """
    limit = _limit() if limit is None else limit

    rc, out, _ = git(["rev-parse", "--is-inside-work-tree"], clone_dir)
    if rc != 0 or out.strip() != "true":
        return ResetSafety(False, f"{clone_dir} is not a git work tree — refusing to reset (escalate).")

    rc, out, err = git(["status", "--porcelain"], clone_dir)
    if rc != 0:
        return ResetSafety(False, f"git status failed ({err or rc}) — refusing to reset (escalate).")
    # Untracked files ("??") survive reset --hard, so they are not "discarded";
    # only tracked modifications/deletions/staged changes are at risk.
    uncommitted = len([ln for ln in out.splitlines() if ln.strip() and not ln.startswith("??")])

    rc, out, err = git(["rev-list", "--count", "origin/main..HEAD"], clone_dir)
    if rc != 0:
        return ResetSafety(False, f"cannot compare HEAD to origin/main ({err or rc}) — refusing (escalate).",
                           uncommitted=uncommitted)
    try:
        ahead = int(out.strip() or "0")
    except ValueError:
        ahead = 0

    # Files present in HEAD but absent from origin/main vanish on reset — the
    # direct cover-wipe vector (48 images that origin/main never had).
    rc, out, err = git(["diff", "--diff-filter=D", "--name-only", "HEAD", "origin/main"], clone_dir)
    if rc != 0:
        return ResetSafety(False, f"cannot diff HEAD vs origin/main ({err or rc}) — refusing (escalate).",
                           uncommitted=uncommitted, ahead=ahead)
    files_lost = len([ln for ln in out.splitlines() if ln.strip()])

    if uncommitted > 0:
        return ResetSafety(False,
                           f"⚠️ clone has {uncommitted} uncommitted tracked change(s) — "
                           f"reset --hard would discard them. Refusing; escalate for a human.",
                           files_lost, uncommitted, ahead)
    if ahead > 0:
        return ResetSafety(False,
                           f"⚠️ clone is {ahead} commit(s) ahead of origin/main (unpushed work) — "
                           f"reset --hard would destroy them. Refusing; escalate for a human.",
                           files_lost, uncommitted, ahead)
    if files_lost > limit:
        return ResetSafety(False,
                           f"⚠️ mass-deletion guard: reset --hard would remove {files_lost} tracked "
                           f"file(s) (limit {limit}) — the 2026-06-22 cover-wipe hazard. "
                           f"Refusing; escalate for a human.",
                           files_lost, uncommitted, ahead)
    return ResetSafety(True,
                       f"ok: clean clone, {files_lost} file(s) differ from origin/main "
                       f"(limit {limit}) — safe to realign.",
                       files_lost, uncommitted, ahead)


def realign_clone(clone_dir: str, *, git: GitRunner = _git) -> tuple[bool, str]:
    """The bounded fix: fetch -> checkout main -> SAFETY NET -> reset --hard ->
    re-pin identity. Returns ``(ok, detail)``. Never resets without a clean
    ``assess_reset`` verdict. Order matters: ``git checkout`` is git-protected
    (it refuses on conflict rather than clobbering), so it is safe to run before
    the assessment; the only destructive step (reset) runs solely after the net
    passes.

    KNOWN LIMITATION — there is a small TOCTOU window between ``assess_reset`` and
    the ``reset --hard``: in a *shared* clone a second agent could write new work
    in that gap, which the reset would then discard. This is acceptable ONLY
    because the class is gated-only (``auto_eligible=False``): a human runs it
    deliberately on a clone known to be idle/broken. It MUST NOT be promoted to an
    unattended auto-act without a clone-level lock closing this window first.
    """
    rc, _, err = git(["fetch", "origin", "main"], clone_dir)
    if rc != 0:
        return False, f"git fetch origin main failed ({err or rc}) — not resetting (escalate)."

    # Switch onto main. Git carries non-conflicting uncommitted changes over (the
    # net catches them next) and refuses outright on a conflict (we escalate).
    rc, _, err = git(["checkout", "main"], clone_dir)
    if rc != 0:
        return False, (f"git checkout main failed ({err or rc}) — clone has blocking "
                       f"local changes; escalate for a human.")

    verdict = assess_reset(clone_dir, git=git)
    if not verdict.safe:
        return False, verdict.reason

    rc, _, err = git(["reset", "--hard", "origin/main"], clone_dir)
    if rc != 0:
        return False, f"git reset --hard origin/main failed ({err or rc})."

    # Re-pin the agent identity in the clone so the next run commits as itself.
    git(["config", "user.email", AGENT_EMAIL], clone_dir)
    git(["config", "user.name", AGENT_NAME], clone_dir)

    return True, (f"realigned {clone_dir} to origin/main "
                  f"({verdict.files_lost} file(s) differed) and re-pinned {AGENT_EMAIL}.")
