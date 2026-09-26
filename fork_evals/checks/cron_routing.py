"""Safety net #4 — cron jobs deliver to an explicit destination, not a frozen DM.

Why this exists: after the profiles migration, scheduled jobs kept posting to the
old private DM instead of the team forum thread. Root cause: ``deliver="origin"``
*freezes* routing to wherever the job was created. The fix was to pin an explicit
forum destination (platform + chat_id + thread_id). This net flags jobs that have
slid back into the frozen-origin footgun so a misroute is caught before it ships
output to the wrong place (silently).

Reuses the scheduler's real ``_resolve_origin`` validation rather than
re-implementing it. Telegram chat-id sign convention: a *positive* chat_id is a
private DM; negative is a group/supergroup (forum). Forum delivery needs a
``thread_id`` or it lands in the wrong topic.
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from cron.scheduler import _resolve_origin


def _effective_deliver(job: dict) -> str:
    """Mirror create_job's default: origin if present, else local."""
    deliver = job.get("deliver")
    if deliver:
        return deliver
    return "origin" if job.get("origin") else "local"


def routing_hazard(job: dict) -> Optional[str]:
    """Return a one-line hazard description, or None if routing is safe."""
    if _effective_deliver(job) != "origin":
        return None  # explicit platform / local delivery — not a frozen-origin risk

    resolved = _resolve_origin(job)
    if resolved is None:
        return "delivers to 'origin' but origin is incomplete (no platform/chat_id) — will fail or fall back"

    platform = resolved.get("platform")
    chat_id = resolved.get("chat_id")
    thread_id = resolved.get("thread_id")

    if platform == "telegram":
        try:
            is_dm = int(str(chat_id)) > 0
        except (TypeError, ValueError):
            is_dm = False
        if is_dm:
            return (
                f"frozen 'origin' delivery to a Telegram private DM (chat_id={chat_id}) — "
                "pin an explicit forum destination instead"
            )
        if thread_id in (None, "", 0, "0"):
            return (
                f"frozen 'origin' delivery to a Telegram group (chat_id={chat_id}) with no "
                "thread/topic — may land in the wrong forum topic"
            )
    return None


def audit_cron_routing(jobs: Optional[Iterable[dict]] = None) -> List[Tuple[str, str]]:
    """Return (job_id, hazard) for every live cron job with a routing hazard."""
    if jobs is None:
        try:
            from cron.jobs import load_jobs
            jobs = load_jobs()
        except Exception:
            return []
    flagged: List[Tuple[str, str]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        hazard = routing_hazard(job)
        if hazard:
            flagged.append((str(job.get("id") or job.get("name") or "<unknown>"), hazard))
    return flagged


def audit_summary(jobs: Optional[Iterable[dict]] = None) -> str:
    """Summary used as eval-case output.

    Reports the population it examined, not just the verdict. "OK" on its own
    is indistinguishable from "audited nothing" — which is what a reader (or a
    judge) has to be able to tell apart, since ``load_jobs`` failing and every
    job being clean both used to render as the same green line.
    """
    if jobs is None:
        try:
            from cron.jobs import load_jobs
            jobs = list(load_jobs())
        except Exception as exc:  # noqa: BLE001 — a failed audit is not a pass
            return (f"FAIL: could not load the live cron jobs "
                    f"({type(exc).__name__}) — nothing was audited.")
    else:
        jobs = [j for j in jobs if isinstance(j, dict)]

    flagged = audit_cron_routing(jobs)
    if flagged:
        return "FAIL: " + "; ".join(f"job {jid}: {hz}" for jid, hz in flagged)

    modes: dict[str, int] = {}
    for job in jobs:
        mode = _effective_deliver(job)
        modes[mode] = modes.get(mode, 0) + 1
    breakdown = ", ".join(f"{m}={n}" for m, n in sorted(modes.items())) or "none"
    if not jobs:
        return ("OK: audited 0 live cron jobs — none were present, so this run "
                "demonstrates nothing about live routing.")
    return (f"OK: audited {len(jobs)} live cron jobs, 0 with a routing hazard. "
            f"Effective delivery mode per job: {breakdown}. No job uses frozen "
            f"'origin' delivery to a private DM or to a group without a thread.")
