"""Remediation class registry (Phase 1).

A remediation *class* is a known, bounded failure-and-fix pattern: a matcher that
recognises the failure from the incident the watcher already produces, a bounded
fix, an optional reversal, and a default lifecycle mode. The registry is code
(version-controlled, deploys via the §6 clone pull); per-class *mode* is runtime
state on the volume (see ``remediation/modes.py``) so a promotion survives reboot
and is never clobbered by a resync.

Signature = the incident's own ``id`` (e.g. ``cron:<jid>:<last_run>``), which the
watcher already mints stable-per-occurrence. The debounce/ledger machinery keys
off it directly — Phase 1 invents no new signature scheme.

Tier-0 admission rule: a class is eligible only if its fix is **reversible OR
bounded-and-idempotent** (re-running it cannot compound damage). A retry has no
meaningful "undo" but is idempotent at our layer — the debounce guard ensures one
fix per failure occurrence, and the job's own guards (auditor, git-guard) bound
what a re-run can touch. Such a class declares ``reversal=None`` with a rationale.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from incidents.sweep import Incident

# Substrings that mark a failure as *transient* (safe to retry). Conservative
# allowlist: if NONE is present the failure is treated as non-transient and the
# class does NOT match — it stays a human-handled incident. Better to under-fix
# (escalate to Brais) than to retry a deterministic failure forever.
_TRANSIENT_MARKERS = (
    "provider returned error",   # owl-alpha upstream 5xx (the first real incident)
    "rate limit", "rate-limit",
    "timeout", "timed out", "timed-out",
    "temporarily", "temporary failure",
    "connection reset", "connection refused", "connection error",
    "bad gateway", "service unavailable", "gateway timeout",
    "read timed out", "remote end closed",
)

# Bare HTTP status codes, matched on word boundaries rather than as free
# substrings: as substrings they fire on anything that merely CONTAINS the
# digits (a request id, a byte count, a duration). Boundaries can only ever make
# this tuple match LESS, so the retry class only gets *less* eager — the safe
# direction. The veto tuples below deliberately stay plain substrings: loosening
# a veto would make the classes MORE eager, and one of them is destructive.
_TRANSIENT_CODE_RE = re.compile(r"\b(?:429|502|503|504)\b")

# Substrings that VETO a retry even if a transient marker is also present — these
# are deterministic faults a retry can never clear (config, auth, missing code).
_HARD_FAULT_MARKERS = (
    "modulenotfound", "no module named", "no models provided",
    "401", "403", "unauthorized", "forbidden", "permission denied",
    "not found", "no such file", "invalid", "traceback",
)


def _looks_transient(text: str) -> bool:
    low = (text or "").lower()
    if any(m in low for m in _HARD_FAULT_MARKERS):
        return False
    if any(m in low for m in _TRANSIENT_MARKERS):
        return True
    return _TRANSIENT_CODE_RE.search(low) is not None


# Substrings that mark a cron failure as a shared-clone *branch/identity*
# confusion — the SEO / Gap-Hunter hazard where agents sharing
# ``/opt/data/<profile>`` leave the clone on the wrong branch or with no pinned
# git identity. Deliberately PRECISE: the fix is destructive, so under-matching
# (escalate) is far safer than firing a ``reset --hard`` on the wrong incident.
_BRANCH_CONFUSION_MARKERS = (
    "detached head", "head detached",
    "wrong branch", "not on main", "not on branch main", "current branch is not main",
    "diverged",
    "author identity unknown", "committer identity unknown",
    "please tell me who you are", "empty ident name",
    "would be overwritten by checkout", "would be overwritten by merge",
    "local changes to the following files would be overwritten",
    "non-fast-forward", "updates were rejected",
)

# A branch reset cannot fix a deterministic code/auth/config fault — and running
# a destructive reset on one would be reckless. If any of these is present the
# branch-confusion class does NOT match (it escalates as a plain incident).
_CONFUSION_VETO = (
    "modulenotfound", "no module named", "no models",
    "401", "403", "unauthorized", "forbidden",
)


def _looks_branch_confusion(text: str) -> bool:
    low = (text or "").lower()
    if any(v in low for v in _CONFUSION_VETO):
        return False
    return any(m in low for m in _BRANCH_CONFUSION_MARKERS)


def _match_text(inc: Incident) -> str:
    """The text the marker tuples are scanned against: the raw error alone.

    ``inc.detail`` is the brief's human-facing body — the sweep renders it as
    ``when: <iso>\\nerror: <text>``, so scanning it handed the matchers an
    ISO-8601 timestamp as if it were error text. A microsecond field containing
    ``401``/``403`` then tripped the hard-fault veto (and the confusion veto),
    and a genuinely transient failure silently stopped classifying — an absence,
    not an error, which is why it went unnoticed.

    Falls back to ``detail`` when a producer supplies no separate ``error``, so
    no caller has to change; that fallback keeps the old (over-vetoing, therefore
    fail-safe) behaviour rather than classifying nothing at all.
    """
    return inc.error or inc.detail


def _cron_job_id(inc: Incident) -> Optional[str]:
    """Extract the job id the watcher embedded in ``handoff`` ("cron job id <jid>").
    Robust to ids/names containing colons (unlike splitting ``inc.id``)."""
    prefix = "cron job id "
    if inc.handoff and inc.handoff.startswith(prefix):
        jid = inc.handoff[len(prefix):].strip()
        return jid or None
    return None


# --- fixes ------------------------------------------------------------------
# Each fix returns (ok, detail). Fixes are invoked ONLY via an explicit, approved
# `remediate apply` (Phase 1, gated) or a promoted auto-act past the guards
# (Phase 3) — never on import.

def _retry_cron(inc: Incident) -> Tuple[bool, str]:
    """Bounded fix: re-schedule the failed job for the next scheduler tick."""
    jid = _cron_job_id(inc)
    if not jid:
        return False, "could not resolve job id from incident"
    from cron.jobs import trigger_job
    job = trigger_job(jid)
    if job is None:
        return False, f"job '{jid}' not found"
    return True, f"re-scheduled job '{jid}' for next tick"


def _realign_shared_clone(inc: Incident) -> Tuple[bool, str]:
    """Destructive fix (gated-only): realign the job's shared clone to origin/main
    and re-pin the agent identity. Delegates the actual git work — and, crucially,
    the pre-fix cover-wipe safety net — to ``remediation.clone_safety``."""
    jid = _cron_job_id(inc)
    if not jid:
        return False, "could not resolve job id from incident"
    from cron.jobs import get_job
    job = get_job(jid)
    if job is None:
        return False, f"job '{jid}' not found"
    clone_dir = job.get("workdir")
    if not clone_dir:
        return False, (f"job '{jid}' has no workdir — shared-clone path unknown; "
                       f"refusing to reset blindly (escalate).")
    from remediation.clone_safety import realign_clone
    return realign_clone(str(clone_dir))


@dataclass(frozen=True)
class RemediationClass:
    name: str
    matches: Callable[[Incident], bool]       # does this incident belong here?
    fix: Callable[[Incident], Tuple[bool, str]]
    proposal: Callable[[Incident], str]       # human-readable action for the gated brief
    default_mode: str                          # "gated" | "auto"
    reversal: Optional[Callable[[Incident], Tuple[bool, str]]] = None
    rationale: str = ""                        # why Tier-0 (reversible/idempotent)
    auto_eligible: bool = True                 # may this class EVER be promoted to auto?


CRON_TRANSIENT_FAILURE = RemediationClass(
    name="cron-transient-failure",
    matches=lambda inc: inc.kind == "cron" and _looks_transient(_match_text(inc)),
    fix=_retry_cron,
    proposal=lambda inc: (
        f"retry cron job ({_cron_job_id(inc) or 'unknown'}) — transient error, "
        f"idempotent re-run, no reversal needed"
    ),
    default_mode="gated",
    reversal=None,
    rationale="bounded idempotent re-run; debounce caps it to one retry per failure occurrence",
)

SHARED_CLONE_BRANCH_CONFUSION = RemediationClass(
    name="shared-clone-branch-confusion",
    matches=lambda inc: inc.kind == "cron" and _looks_branch_confusion(_match_text(inc)),
    fix=_realign_shared_clone,
    proposal=lambda inc: (
        f"realign shared clone for cron job ({_cron_job_id(inc) or 'unknown'}): "
        f"checkout main + reset --hard origin/main + re-pin the *@agent.local identity. "
        f"DESTRUCTIVE — runs the pre-fix cover-wipe safety net first and REFUSES on any "
        f"uncommitted or unpushed work."
    ),
    default_mode="gated",
    auto_eligible=False,   # gated-only, FOREVER — see rationale
    reversal=None,
    rationale=(
        "The fix runs `git reset --hard origin/main` in a SHARED clone — the exact "
        "2026-06-22 BigLobster cover-wipe hazard (a blind git op deleted 48 images). "
        "Admitted to Tier-0 ONLY because remediation.clone_safety refuses unless the "
        "clone has nothing unique to lose (no uncommitted changes, no unpushed commits, "
        "no mass deletion), which reduces the reset to a non-destructive realignment. "
        "Gated-only and NEVER auto-promotable (auto_eligible=False): a human approves "
        "every execution."
    ),
)

# Registry order = match priority (first match wins in `classify`). Keep the
# non-destructive retry class FIRST: if an error ever carried both a transient and
# a branch-confusion marker, the harmless retry should win over the reset.
REGISTRY: List[RemediationClass] = [
    CRON_TRANSIENT_FAILURE,
    SHARED_CLONE_BRANCH_CONFUSION,
]


def classify(inc: Incident) -> Optional[RemediationClass]:
    """Return the first remediation class that recognises this incident, or None
    (None = no known bounded fix → it stays a plain human-handled incident)."""
    for rc in REGISTRY:
        try:
            if rc.matches(inc):
                return rc
        except Exception:
            continue
    return None
