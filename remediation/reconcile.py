"""Verification + promotion recommender (Phase 2).

Closes the apprenticeship loop. Each watcher tick:

  1. VERIFY — for every ``applied`` fix not yet resolved, check whether the job
     actually recovered. A fix counts as success only once the job has *re-run
     since the apply* AND is no longer failing. If it re-ran and still fails, the
     fix is marked ``failed`` and surfaced for escalation. If it hasn't re-run
     yet, verification stays pending (we never reward — or blame — a fix before
     its job has had a chance to run).

  2. RECOMMEND — when a gated class has accumulated K clean, verified runs, emit a
     one-line "promote?" suggestion to the CEO (thread 1904). Deduped via a
     ``recommended`` ledger event so it isn't repeated every tick.

Design choice (flag-worthy): success is JOB-HEALTH at the next tick, not mere
signature rotation. If a retried job immediately fails again — even for a new
reason — the fix did NOT restore health, so it does not count toward promotion.
Conservative on purpose: the track record only rewards fixes that actually worked.

The CEO still approves the promotion itself (`remediate promote <class>`); this
module only *recommends* — hybrid trust, never auto-promotion.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, UTC
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from remediation import ledger, modes

K_PROMOTION = 5               # clean verified gated runs before a promotion is suggested
RECOMMEND_COOLDOWN_HOURS = 24  # don't repeat the same promotion ask more often than this
_FAILING_WINDOW_HOURS = 26     # matches incidents CRON_FAILURE_WINDOW_HOURS


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


def _job_id_from_target(target: str) -> Optional[str]:
    """Applied entries store ``target`` = the incident handoff ("cron job id <jid>")."""
    prefix = "cron job id "
    if target and target.startswith(prefix):
        jid = target[len(prefix):].strip()
        return jid or None
    return None


def _is_failing_now(job: dict, now: datetime) -> bool:
    err = job.get("last_error") or job.get("last_delivery_error")
    if not err:
        return False
    ts = _parse_iso(job.get("last_run_at"))
    return ts is not None and (now - ts) <= timedelta(hours=_FAILING_WINDOW_HOURS)


def _unresolved_applied(entries: list[ledger.LedgerEntry]) -> list[ledger.LedgerEntry]:
    """``applied`` entries that have no later ``verified``/``failed`` for the same signature."""
    resolved = {e.signature for e in entries
                if e.event in (ledger.EVENT_VERIFIED, ledger.EVENT_FAILED)}
    return [e for e in entries if e.event == ledger.EVENT_APPLIED and e.signature not in resolved]


def verify_pending(jobs: list[dict], entries: list[ledger.LedgerEntry], *,
                   now: Optional[datetime] = None,
                   ) -> tuple[list[ledger.LedgerEntry], list[str]]:
    """Return (new ledger entries, escalation messages) for resolvable applied fixes.

    Pure: caller persists the new entries. Jobs that haven't re-run since the apply
    are left pending (no entry produced).
    """
    now = now or _now()
    by_id: dict[str, dict] = {str(j.get("id")): j for j in jobs if isinstance(j, dict)}
    writes: list[ledger.LedgerEntry] = []
    escalations: list[str] = []

    for e in _unresolved_applied(entries):
        jid = _job_id_from_target(e.target)
        if jid is None:
            continue  # unknown shape — leave pending rather than guess
        job = by_id.get(jid)
        applied_ts = _parse_iso(e.ts)

        if job is None:
            # Job no longer exists -> it isn't failing. Count the fix as success.
            writes.append(ledger.make_entry(
                e.cls, e.signature, e.target, e.mode, ledger.EVENT_VERIFIED,
                outcome=ledger.OUTCOME_SUCCESS, detail="job absent at verification", now=now))
            continue

        last_run = _parse_iso(job.get("last_run_at"))
        if applied_ts is not None and last_run is not None and last_run <= applied_ts:
            continue  # hasn't re-run since the fix — verify on a later tick

        if _is_failing_now(job, now):
            writes.append(ledger.make_entry(
                e.cls, e.signature, e.target, e.mode, ledger.EVENT_FAILED,
                outcome=ledger.OUTCOME_FAILURE, detail="job still failing after fix re-ran", now=now))
            escalations.append(
                f"⚠️ Remediation did NOT clear *{e.cls}* on job '{jid}' — fix re-ran but "
                f"the job is still failing. Needs a human look.")
        else:
            writes.append(ledger.make_entry(
                e.cls, e.signature, e.target, e.mode, ledger.EVENT_VERIFIED,
                outcome=ledger.OUTCOME_SUCCESS, detail="job healthy after fix", now=now))
    return writes, escalations


def clean_run_count(cls: str, entries: list[ledger.LedgerEntry]) -> int:
    """Distinct gated occurrences that verified successfully — the promotion metric."""
    return len({e.signature for e in entries
                if e.cls == cls and e.event == ledger.EVENT_VERIFIED
                and e.outcome == ledger.OUTCOME_SUCCESS and e.mode == modes.MODE_GATED})


def _recommended_recently(cls: str, entries: list[ledger.LedgerEntry], now: datetime) -> bool:
    cutoff = now - timedelta(hours=RECOMMEND_COOLDOWN_HOURS)
    for e in entries:
        if e.cls != cls or e.event != ledger.EVENT_RECOMMENDED:
            continue
        ts = _parse_iso(e.ts)
        if ts is not None and ts >= cutoff:
            return True
    return False


def promotion_recommendations(entries: list[ledger.LedgerEntry], *,
                              modes_path: Optional[Path] = None,
                              now: Optional[datetime] = None, k: int = K_PROMOTION,
                              ) -> tuple[list[ledger.LedgerEntry], list[str]]:
    """Suggest promotion for gated classes that have earned it (deduped)."""
    now = now or _now()
    from remediation.registry import REGISTRY
    writes: list[ledger.LedgerEntry] = []
    msgs: list[str] = []
    for rc in REGISTRY:
        if not rc.auto_eligible:
            continue  # gated-only class — never suggest promotion (destructive fix)
        if modes.is_auto(rc.name, path=modes_path):
            continue  # already promoted
        count = clean_run_count(rc.name, entries)
        if count < k or _recommended_recently(rc.name, entries, now):
            continue
        msgs.append(
            f"🎓 *{rc.name}* has {count} clean gated runs (≥{k}). Promote to auto?\n"
            f"_Approve: python -m remediation.cli promote {rc.name}_")
        writes.append(ledger.make_entry(
            rc.name, f"promote:{rc.name}", rc.name, modes.MODE_GATED,
            ledger.EVENT_RECOMMENDED, detail=f"{count} clean runs", now=now))
    return writes, msgs


def reconcile(jobs: list[dict], *, ledger_path: Optional[Path] = None,
              modes_path: Optional[Path] = None, now: Optional[datetime] = None,
              dry_run: bool = False) -> str:
    """One reconciliation pass. Returns text to deliver ("" = nothing to say).

    Verifies first, then recommends on the post-verification ledger so a run that
    just verified clean can tip a class over the K threshold in the same tick.
    """
    now = now or _now()
    entries = ledger.read(path=ledger_path)

    verify_writes, escalations = verify_pending(jobs, entries, now=now)
    entries = entries + verify_writes  # recommend against the updated view
    rec_writes, rec_msgs = promotion_recommendations(
        entries, modes_path=modes_path, now=now)

    if not dry_run:
        for entry in verify_writes + rec_writes:
            ledger.append(entry, path=ledger_path)

    return "\n\n".join(escalations + rec_msgs)
