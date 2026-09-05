"""Hermes incident watcher — hourly sweep (Phase 0).

Detects failures from signals Hermes already produces and prints one brief per
NEW incident to stdout. Designed to run as a Hermes ``no_agent`` cron job whose
stdout is delivered to the incidents Telegram thread.

Signals:
  * Failed cron jobs — the scheduler records ``last_error`` / ``last_delivery_error``
    (+ ``last_run_at``) on each job record.
  * Silently stalled cron jobs — enabled recurring jobs whose own schedule says a
    run should have completed by now (+ grace) but ``last_run_at`` never advanced.
    Catches aborts that record no error (approval stalls, killed agents).
  * Prompt drift — jobs carrying a ``prompt_source`` field whose live prompt no
    longer matches the repo ``.prompt`` file (they are independent by design;
    editing one side silently diverges the other). Opt-in per job.
  * Errored Langfuse traces — best-effort via the public read API (ERROR-level
    observations grouped by trace). Degrades to nothing if the API/keys are absent.
  * Blocked agent commits — the git-guard pre-commit hook appends a JSON line to
    ``blocked-commits.jsonl`` when it blocks a mass-deletion commit.
  * Site-checkout drift — docker/cont-init.d/03-biglobster-config section 6b
    appends a JSON line to ``checkout-drift.jsonl`` when a BigLobster site
    checkout is both dirty and carries local commits origin/main doesn't have
    (never auto-resolved, so it needs a human look).

Output behaviour (matches the configured policy):
  * new incidents found            -> print brief(s)   (delivered)
  * nothing found                  -> print nothing     (cron treats empty stdout as silent)
  * nothing found AND >24h silent  -> print one "all clean" heartbeat + reset the clock

State: ``$HERMES_HOME/incidents/state.json`` -> {"seen": [...], "last_heartbeat_at": iso}
Dedup is by stable incident id, so a failure is reported once (until it recurs at
a new run), and the heartbeat clock resets on any output.

CLI:
    python -m incidents.sweep            # normal sweep
    python -m incidents.sweep --dry-run  # detect + print, do NOT touch state
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

HEARTBEAT_HOURS = 24
CRON_FAILURE_WINDOW_HOURS = 26  # a failure stays "current" until the job runs again
STALE_GRACE_HOURS = 1  # slack past the expected next run before a job counts as stalled
LANGFUSE_WINDOW_HOURS = 2
_SEEN_CAP = 2000
_BLOCKED_CAP = 500  # cap on retained blocked-commit signal lines


@dataclass
class Incident:
    id: str          # stable dedup key
    kind: str        # "cron" | "langfuse"
    title: str
    detail: str
    handoff: str     # how to hand it to Claude Code for a proposed fix


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _state_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "incidents" / "state.json"


def _blocked_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "incidents" / "blocked-commits.jsonl"


def _checkout_drift_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "incidents" / "checkout-drift.jsonl"


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _within(iso: Optional[str], hours: int, now: datetime) -> bool:
    ts = _parse_iso(iso)
    return ts is not None and (now - ts) <= timedelta(hours=hours)


def cron_failure_incidents(jobs: List[dict], *, now: Optional[datetime] = None,
                           window_hours: int = CRON_FAILURE_WINDOW_HOURS) -> List[Incident]:
    """Flag jobs whose most recent run recorded an error within the window."""
    now = now or _now()
    out: List[Incident] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        err = job.get("last_error") or job.get("last_delivery_error")
        if not err:
            continue
        last_run = job.get("last_run_at")
        if not _within(last_run, window_hours, now):
            continue
        jid = str(job.get("id") or job.get("name") or "unknown")
        err_kind = "agent error" if job.get("last_error") else "delivery error"
        out.append(Incident(
            id=f"cron:{jid}:{last_run}",
            kind="cron",
            title=f"Cron job '{job.get('name') or jid}' failed ({err_kind})",
            detail=f"when: {last_run}\nerror: {str(err)[:500]}",
            handoff=f"cron job id {jid}",
        ))
    return out


def cron_stale_incidents(jobs: List[dict], *, now: Optional[datetime] = None,
                         grace_hours: float = STALE_GRACE_HOURS) -> List[Incident]:
    """Flag enabled recurring jobs that silently stopped completing runs.

    Closes the watcher's known blind spot: a run that aborts before
    ``mark_job_run`` (approval stall, killed agent, scheduler wedge) leaves
    ``last_error`` empty, so :func:`cron_failure_incidents` never fires.
    Health is judged by outcome instead — the job's own schedule says when it
    should have completed a run; if that moment is more than ``grace_hours``
    in the past and ``last_run_at`` hasn't advanced, the job is stalled.

    Uses ``cron.jobs.compute_next_run`` so interval and cron schedules are
    handled by the same logic the scheduler itself uses. One-shot jobs are
    skipped (they auto-delete / have their own recovery path). Degrades to
    [] if cron.jobs is unavailable, per the sweep's best-effort philosophy.
    """
    now = now or _now()
    try:
        from cron.jobs import compute_next_run
    except Exception:
        return []

    out: List[Incident] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if not job.get("enabled", True) or job.get("state") == "paused":
            continue
        schedule = job.get("schedule")
        if not isinstance(schedule, dict) or schedule.get("kind") == "once":
            continue
        base = job.get("last_run_at") or job.get("created_at")
        if not base:
            continue
        try:
            expected_next = _parse_iso(compute_next_run(schedule, base))
        except Exception:
            continue
        if expected_next is None:
            continue
        overdue = now - expected_next
        if overdue <= timedelta(hours=grace_hours):
            continue
        jid = str(job.get("id") or job.get("name") or "unknown")
        last_run = job.get("last_run_at")
        out.append(Incident(
            id=f"cron-stale:{jid}:{base}",
            kind="cron",
            title=f"Cron job '{job.get('name') or jid}' silently stalled",
            detail=(
                f"expected a completed run by: {expected_next.isoformat()}\n"
                f"last completed run: {last_run or 'never'}\n"
                "no error was recorded — the run likely aborted before "
                "finishing (approval stall, killed agent, or scheduler wedge)"
            ),
            handoff=f"cron job id {jid} (silent stall — check scheduler logs, not last_error)",
        ))
    return out


def prompt_drift_incidents(jobs: List[dict], *,
                           repo_root: Optional[Path] = None) -> List[Incident]:
    """Flag agent jobs whose live prompt has drifted from its repo source.

    Repo ``.prompt`` files and live job prompts (jobs.json on the volume) are
    independent — editing one without the other has already caused silent
    divergence (infographic cron, 2026-06). Opt-in per job: set
    ``prompt_source`` on the job record to the repo-relative path of its
    ``.prompt`` file (e.g. ``onsite-seo/seo-agent.prompt``) and the watcher
    compares content each sweep. Jobs without the field are skipped, so
    rollout is a runtime field-set, not a migration.

    Dedup id includes both content hashes — a drift alerts once, then again
    only if either side changes again.
    """
    import hashlib

    root = repo_root or Path(__file__).resolve().parent.parent
    out: List[Incident] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        source = job.get("prompt_source")
        if not source or not isinstance(source, str):
            continue
        jid = str(job.get("id") or job.get("name") or "unknown")
        name = job.get("name") or jid
        path = root / source
        try:
            repo_text = path.read_text(encoding="utf-8")
        except OSError:
            out.append(Incident(
                id=f"prompt-drift:{jid}:missing:{source}",
                kind="cron",
                title=f"Cron job '{name}' prompt source missing",
                detail=f"prompt_source: {source}\nfile not found under {root}",
                handoff=f"cron job id {jid} — fix its prompt_source path or restore the file",
            ))
            continue
        live_text = job.get("prompt") or ""
        if repo_text.strip() == live_text.strip():
            continue
        repo_hash = hashlib.sha256(repo_text.strip().encode()).hexdigest()[:12]
        live_hash = hashlib.sha256(live_text.strip().encode()).hexdigest()[:12]
        out.append(Incident(
            id=f"prompt-drift:{jid}:{repo_hash}:{live_hash}",
            kind="cron",
            title=f"Cron job '{name}' prompt drifted from repo source",
            detail=(
                f"prompt_source: {source}\n"
                f"repo sha256: {repo_hash}  live sha256: {live_hash}\n"
                "repo .prompt and live jobs.json prompt no longer match — "
                "update BOTH sides (they are independent by design)"
            ),
            handoff=f"cron job id {jid} — diff {source} against the live job prompt",
        ))
    return out


def langfuse_error_incidents(*, now: Optional[datetime] = None,
                             window_hours: int = LANGFUSE_WINDOW_HOURS) -> List[Incident]:
    """Best-effort: ERROR-level Langfuse observations grouped by trace.

    Returns [] on any problem (missing keys, network, schema) — the cron signal
    carries Phase 0 on its own. Refine against the live API as real error traces
    appear.
    """
    now = now or _now()
    pub = (os.environ.get("HERMES_LANGFUSE_PUBLIC_KEY") or os.environ.get("LANGFUSE_PUBLIC_KEY") or "").strip()
    sec = (os.environ.get("HERMES_LANGFUSE_SECRET_KEY") or os.environ.get("LANGFUSE_SECRET_KEY") or "").strip()
    base = (os.environ.get("HERMES_LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com").strip().rstrip("/")
    if not (pub and sec):
        return []

    import base64
    import urllib.request

    frm = (now - timedelta(hours=window_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{base}/api/public/observations?level=ERROR&fromStartTime={frm}&limit=50"
    token = base64.b64encode(f"{pub}:{sec}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (trusted Langfuse host)
            payload = json.loads(resp.read().decode())
    except Exception:
        return []

    by_trace: dict[str, dict] = {}
    for obs in (payload.get("data") or []):
        tid = obs.get("traceId")
        if tid and tid not in by_trace:
            by_trace[tid] = obs

    out: List[Incident] = []
    for tid, obs in by_trace.items():
        msg = obs.get("statusMessage") or obs.get("name") or "error-level observation"
        out.append(Incident(
            id=f"trace:{tid}",
            kind="langfuse",
            title=f"Langfuse error trace {tid[:12]}…",
            detail=f"signal: {str(msg)[:300]}",
            handoff=f"trace-id {tid}",
        ))
    return out


def blocked_commit_incidents(path: Optional[Path] = None) -> List[Incident]:
    """Read the git-guard signal file and surface each blocked agent commit.

    The managed pre-commit hook (scripts/git-guard/pre-commit) appends one JSON
    line per blocked commit to ``$HERMES_HOME/incidents/blocked-commits.jsonl``.
    This is the alert path for the 2026-06-22 cover-wipe class: the commit is
    blocked locally AND reported here so the failure is visible, not silent.

    Best-effort: a malformed or missing file yields []. Dedup is by the existing
    seen-state (stable id per signal), so a block is reported once.
    """
    path = path or _blocked_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []

    out: List[Incident] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except Exception:
            continue
        ts = str(rec.get("ts") or "unknown")
        repo = str(rec.get("repo") or rec.get("cwd") or "unknown")
        reason = str(rec.get("reason") or "agent commit blocked by git guard")
        cwd = str(rec.get("cwd") or "")
        out.append(Incident(
            id=f"blocked:{ts}:{cwd}:{reason[:40]}",
            kind="blocked_commit",
            title="Blocked agent commit (git guard)",
            detail=f"when: {ts}\nrepo: {repo}\nreason: {reason}",
            handoff=f"blocked commit in {repo} — review what the agent tried to delete/break",
        ))
    return out


def checkout_drift_incidents(path: Optional[Path] = None) -> List[Incident]:
    """Read the site-checkout drift signal and surface each one.

    docker/cont-init.d/03-biglobster-config section 6b appends one JSON line to
    ``$HERMES_HOME/incidents/checkout-drift.jsonl`` when a BigLobster site
    checkout is both dirty (uncommitted changes to a tracked file) AND carries
    local commits origin/main doesn't have. That combination is never
    auto-resolved (it might be real unmerged work), so it would otherwise sit
    as a silent, non-fatal boot warning indefinitely — exactly what happened
    2026-08-12 through 2026-09-05: a single-file sync of a real SOUL.md commit
    into all four checkouts, without advancing their branch pointers, left
    every one of them permanently dirty and blocked `pull --ff-only` for a
    month before anyone noticed. This is the alert path so it gets surfaced
    the same boot it happens, not discovered a month later.

    Best-effort: a malformed or missing file yields []. Dedup is by the
    existing seen-state (stable id per signal), so a drift is reported once
    per checkout per ahead-count (the id changes if the count changes).
    """
    path = path or _checkout_drift_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []

    out: List[Incident] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except Exception:
            continue
        ts = str(rec.get("ts") or "unknown")
        checkout = str(rec.get("checkout") or "unknown")
        ahead = rec.get("ahead")
        reason = str(rec.get("reason") or "dirty checkout with local commits ahead of origin/main")
        out.append(Incident(
            id=f"checkout-drift:{checkout}:{ahead}",
            kind="checkout_drift",
            title=f"Site checkout '{checkout}' diverged and stopped pulling",
            detail=f"when: {ts}\ncheckout: {checkout}\nahead: {ahead}\nreason: {reason}",
            handoff=(
                f"inspect $HERMES_HOME/checkouts/{checkout} — confirm whether its "
                f"local commits already landed upstream (check for a merged PR with "
                f"the same content) before resetting it to origin/main"
            ),
        ))
    return out


def _prune_blocked(path: Path, cap: int = _BLOCKED_CAP) -> None:
    """Keep the signal file bounded. Reported ids persist in seen-state, so
    trimming the oldest lines never re-surfaces an already-reported block."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return
    if len(lines) <= cap:
        return
    try:
        path.write_text("\n".join(lines[-cap:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def _remediation_hint(inc: Incident) -> str:
    """If this incident maps to a known remediation class, append the proposed
    (gated) fix + the approve command. Pure/disk-free: ``classify`` only reads the
    incident. Lazy import keeps the watcher independent of the remediation package
    and avoids any import cycle. Returns "" when there is no known fix.

    Phase 1 is gated-only — every proposal waits for an explicit ``remediate apply``.
    Phase 3 will branch here on ``modes.is_auto`` to auto-act past the guards.
    """
    try:
        from remediation.registry import classify
        rc = classify(inc)
    except Exception:
        return ""
    if rc is None:
        return ""
    return (
        f"\n🔧 *Proposed remediation* ({rc.name}): {rc.proposal(inc)}\n"
        f"_To approve, run: python -m remediation.cli apply {inc.id}_"
    )


def _format_brief(inc: Incident) -> str:
    return (
        f"🔴 *Incident* — {inc.title}\n"
        f"{inc.detail}\n"
        f"_To get a proposed fix, hand this to Claude Code: {inc.handoff}_"
        f"{_remediation_hint(inc)}"
    )


def _heartbeat_line(now: datetime) -> str:
    return (
        f"✅ Hermes incident watcher: still running, no new incidents in the last "
        f"{HEARTBEAT_HOURS}h (as of {now.strftime('%Y-%m-%d %H:%M UTC')})."
    )


def _heartbeat_due(last_hb: Optional[str], now: datetime) -> bool:
    ts = _parse_iso(last_hb)
    return ts is None or (now - ts) >= timedelta(hours=HEARTBEAT_HOURS)


def _reconcile_text(jobs: List[dict], *, now: datetime, dry_run: bool,
                    ledger_path: Optional[Path], modes_path: Optional[Path]) -> str:
    """Run the remediation reconcile pass (verify prior fixes + recommend
    promotions). Best-effort: any failure degrades to "" so the watcher's core
    incident signal is never blocked by the remediation layer."""
    try:
        from remediation.reconcile import reconcile
        return reconcile(jobs, now=now, dry_run=dry_run,
                         ledger_path=ledger_path, modes_path=modes_path)
    except Exception:
        return ""


def sweep(*, now: Optional[datetime] = None, jobs: Optional[List[dict]] = None,
          langfuse: Optional[List[Incident]] = None,
          blocked: Optional[List[Incident]] = None,
          checkout_drift: Optional[List[Incident]] = None, state_path: Optional[Path] = None,
          dry_run: bool = False, ledger_path: Optional[Path] = None,
          modes_path: Optional[Path] = None) -> str:
    """Run one sweep. Returns the text to deliver ("" = stay silent)."""
    now = now or _now()
    state_path = state_path or _state_path()
    state = _load_state(state_path)
    seen_list: list = list(state.get("seen", []))
    seen = set(seen_list)
    last_hb = state.get("last_heartbeat_at")

    if jobs is None:
        try:
            from cron.jobs import load_jobs
            jobs = load_jobs()
        except Exception:
            jobs = []
    lf = langfuse if langfuse is not None else langfuse_error_incidents(now=now)
    bc = blocked if blocked is not None else blocked_commit_incidents()
    cd = checkout_drift if checkout_drift is not None else checkout_drift_incidents()

    incidents = (cron_failure_incidents(jobs, now=now)
                 + cron_stale_incidents(jobs, now=now)
                 + prompt_drift_incidents(jobs)
                 + list(lf) + list(bc) + list(cd))
    new = [i for i in incidents if i.id not in seen]

    incident_text = ""
    if new:
        incident_text = "\n\n".join(_format_brief(i) for i in new)
        for i in new:
            seen.add(i.id)
            seen_list.append(i.id)

    # Remediation reconcile: verify prior gated fixes against current job health
    # and surface promotion recommendations. Escalations/recommendations are
    # substantive output and reset the heartbeat clock just like incidents.
    remediation_text = _reconcile_text(
        jobs, now=now, dry_run=dry_run, ledger_path=ledger_path, modes_path=modes_path)

    substantive = "\n\n".join(t for t in (incident_text, remediation_text) if t)
    output = ""
    if substantive:
        output = substantive
        last_hb = now.isoformat()
    elif _heartbeat_due(last_hb, now):
        output = _heartbeat_line(now)
        last_hb = now.isoformat()

    if not dry_run and output:
        state["seen"] = seen_list[-_SEEN_CAP:]
        state["last_heartbeat_at"] = last_hb
        _save_state(state_path, state)
    if not dry_run and blocked is None:
        _prune_blocked(_blocked_path())
    if not dry_run and checkout_drift is None:
        _prune_blocked(_checkout_drift_path())
    return output


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m incidents.sweep")
    parser.add_argument("--dry-run", action="store_true",
                        help="detect + print, do NOT update state")
    args = parser.parse_args(argv)
    out = sweep(dry_run=args.dry_run)
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
