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
  * Runaway agent runs — an agent cron run that burned its whole iteration
    budget. These report ``last_status: ok`` (the loop exits cleanly at the cap),
    so every failure-shaped signal above stays silent while the job does no work.
  * New dependency advisories — open critical/high osv-scanner alerts from the
    repo's code-scanning API, grouped by package. The existing backlog is
    adopted as a baseline on first run and never reported; only packages whose
    advisory set CHANGES after that produce a brief. Reporting the standing
    queue every hour is what turns a security signal into muted noise.
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
RUNAWAY_WINDOW_HOURS = 26  # one daily cycle + slack, matching CRON_FAILURE_WINDOW_HOURS
RUNAWAY_DEFAULT_MAX_TURNS = 90  # run_agent's hard stop; see AIAgent(max_iterations=...)
RUNAWAY_FRACTION = 0.95  # a run this close to the cap did not choose to stop
_SEEN_CAP = 2000
_BLOCKED_CAP = 500  # cap on retained blocked-commit signal lines


@dataclass
class Incident:
    id: str          # stable dedup key
    kind: str        # "cron" | "langfuse"
    title: str
    detail: str      # human-facing body of the brief — PRESENTATION, not input
    handoff: str     # how to hand it to Claude Code for a proposed fix
    # The raw failure text on its own, for machines. ``detail`` renders the same
    # text with context glued around it (``when: <iso>\nerror: ...``), and
    # remediation.registry used to pattern-match that blob — which meant an
    # ISO-8601 microsecond field containing "401"/"403" tripped the hard-fault
    # veto and a real transient failure silently stopped classifying. Defaulted
    # so producers with no distinct error text need no change; matchers fall
    # back to ``detail`` when it is empty.
    error: str = ""


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
    """Flag jobs whose most recent run recorded an error within the window.

    Also covers runs killed mid-flight by a container restart. Those never
    reach ``mark_job_run``, so the scheduler's restart recovery stamps
    ``last_status='interrupted'`` plus ``last_interrupted_at`` WITHOUT
    advancing ``last_run_at`` (see ``cron.jobs.mark_job_interrupted``). The
    window and dedup key therefore have to read the interruption's own clock —
    keying off ``last_run_at`` would date the incident to the previous
    *successful* run and could age it straight out of the window.

    This is additive to :func:`cron_stale_incidents`, which is left untouched:
    the stall check still fires on the job's own schedule and stays the
    backstop for a drop that recovery never observed (e.g. the ledger row was
    pruned, or the process died before it could claim).
    """
    now = now or _now()
    out: List[Incident] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        err = job.get("last_error") or job.get("last_delivery_error")
        if not err:
            continue
        interrupted = job.get("last_status") == "interrupted"
        when = job.get("last_interrupted_at") if interrupted else job.get("last_run_at")
        if not _within(when, window_hours, now):
            continue
        jid = str(job.get("id") or job.get("name") or "unknown")
        if interrupted:
            err_kind = "interrupted by restart"
        else:
            err_kind = "agent error" if job.get("last_error") else "delivery error"
        err_text = str(err)[:500]
        detail = f"when: {when}\nerror: {err_text}"
        if interrupted:
            detail += (
                f"\nlast completed run: {job.get('last_run_at') or 'never'}\n"
                "this run was NOT retried — side effects may be partial; "
                f"inspect with `hermes cron runs {jid}` before re-running it"
            )
        out.append(Incident(
            id=f"cron:{jid}:{when}",
            kind="cron",
            title=f"Cron job '{job.get('name') or jid}' failed ({err_kind})",
            detail=detail,
            handoff=f"cron job id {jid}",
            error=err_text,
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
    to = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    # GET /observations is deprecated (served until 2026-11-16) in favor of
    # GET /v2/observations — same query params, response shape unchanged for
    # the core+basic fields this reads (traceId, statusMessage, name).
    url = f"{base}/api/public/v2/observations?level=ERROR&fromStartTime={frm}&toStartTime={to}&limit=50"
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


def _session_dbs(home: Optional[Path] = None) -> List[Path]:
    """Every session DB an agent cron run could have written to.

    A profile-scoped job runs under ``HERMES_HOME=<profile>``, so its session
    lands in that profile's ``state.db`` — NOT the default one the watcher runs
    under. Scanning only the default home would miss exactly the profile jobs
    (auditor, gap hunters, SEO) this signal exists to catch.
    """
    home = home or Path(os.getenv("HERMES_HOME") or (Path.home() / ".hermes"))
    dbs = [home / "state.db"]
    try:
        for prof in sorted((home / "profiles").iterdir()):
            if prof.is_dir():
                dbs.append(prof / "state.db")
    except OSError:
        pass
    return [d for d in dbs if d.exists()]


def _recent_runs(dbs: List[Path], *, since_epoch: float) -> tuple:
    """((session_id, started_at, api_call_count, model) rows, unreadable_dbs).

    Read-only and best-effort: a missing table, a locked DB or a schema change
    degrades to nothing rather than breaking the whole sweep. The watcher must
    never fail because one signal could not read its source.

    But "degrades to nothing" is precisely how a watcher goes silently blind —
    the failure mode this whole signal exists to catch. A read-only sqlite open
    still needs to create/attach the WAL ``-shm`` segment, so a permissions or
    ownership change on a profile's ``state.db`` would make every read fail
    while the sweep kept reporting "all clean" forever. So the unreadable paths
    are returned rather than swallowed, and the caller reports them.
    (Raised by the auditor reviewing PR #237 — the review this signal's own fix
    made possible.)
    """
    rows: List[tuple] = []
    unreadable: List[str] = []
    for db in dbs:
        try:
            import sqlite3
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                rows.extend(con.execute(
                    "SELECT id, started_at, api_call_count, model FROM sessions"
                    " WHERE id LIKE 'cron_%' AND started_at > ? AND ended_at IS NOT NULL",
                    (since_epoch,),
                ).fetchall())
            finally:
                con.close()
        except Exception as exc:
            unreadable.append(f"{db}: {type(exc).__name__}: {exc}")
            continue
    return rows, unreadable


def runaway_incidents(*, now: Optional[datetime] = None,
                      rows: Optional[List[tuple]] = None,
                      max_turns: int = RUNAWAY_DEFAULT_MAX_TURNS,
                      home: Optional[Path] = None) -> List[Incident]:
    """Agent cron runs that exhausted their iteration budget instead of finishing.

    This is the blind spot that let the auditor die unnoticed for four days
    (2026-09-09 to 2026-09-13). Its provider routing silently fell through to
    arbitrary third-party providers, and the agent stopped following its own
    protocol — re-running ``auditor.pending`` forty-plus times per run until the
    90-iteration hard stop. Every run still recorded ``last_status: ok``, because
    hitting the cap is a clean exit, so ``cron_failure_incidents`` saw nothing;
    the job kept running on schedule, so ``cron_stale_incidents`` saw nothing;
    the prompt never changed, so ``prompt_drift_incidents`` saw nothing. The
    review gate was simply gone, and the only symptom was the absence of work.

    Hitting the cap is the signal: an agent that finishes chooses to stop, and
    across ~200 healthy auditor runs the maximum was 60 calls against a cap of
    90. A run at the ceiling has stopped making progress by definition.

    ``rows`` and ``home`` are injectable so this is testable without a database.
    """
    now = now or _now()
    threshold = max(1, int(max_turns * RUNAWAY_FRACTION))
    out: List[Incident] = []
    if rows is None:
        since = (now - timedelta(hours=RUNAWAY_WINDOW_HOURS)).timestamp()
        dbs = _session_dbs(home)
        rows, unreadable = _recent_runs(dbs, since_epoch=since)
        if unreadable and not rows:
            # Every source failed: the signal is blind, not clean. Report that
            # rather than staying quiet, which is the failure this signal exists
            # to catch, one level up.
            out.append(Incident(
                id="runaway-blind:" + now.strftime("%Y-%m-%d"),
                kind="cron",
                title="Runaway-agent signal is blind — no session database could be read",
                detail=("unreadable sources:\n  " + "\n  ".join(unreadable[:5])
                        + "\n\nUntil this is fixed the watcher cannot tell a healthy "
                          "agent from one burning its whole iteration budget."),
                handoff=("The Hermes incident watcher cannot read any session DB. "
                         "Check ownership/permissions on $HERMES_HOME/state.db and "
                         "$HERMES_HOME/profiles/*/state.db for the sweep user."),
            ))

    for sid, _started, calls, model in rows:
        if not isinstance(calls, int) or calls < threshold:
            continue
        job_id = ""
        parts = str(sid).split("_")
        if len(parts) >= 2:
            job_id = parts[1]
        out.append(Incident(
            id=f"runaway:{sid}",
            kind="cron",
            title=f"Agent run burned its full iteration budget ({calls}/{max_turns})",
            detail=(
                f"session: {sid}\n"
                f"job: {job_id or 'unknown'}\n"
                f"model: {model or 'unknown'}\n"
                "The run exited at the iteration cap rather than finishing, so it "
                "reports success while having done little or no work. Check whether "
                "it repeated the same tool calls, and whether the model or provider "
                "routing changed."
            ),
            handoff=(
                f"Hermes cron run {sid} hit the {max_turns}-iteration cap. Pull its "
                "tool-call sequence from the session DB and find what it looped on."
            ),
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


JUDGE_LIVENESS_HOURS = 48


def _judge_liveness_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "incidents" / "judge-liveness.json"


def judge_liveness_incidents(
    path: Optional[Path] = None, *, now: Optional[datetime] = None
) -> List[Incident]:
    """Raise when the auditor's second-LLM judge has not SUCCEEDED recently.

    This is a liveness check, not an error check, and the distinction is the
    whole point. The judge shipped on 2026-06-24 wired through a pipe the cron
    approval gate refused, so it was never invoked at all for twelve weeks:
    nothing threw, nothing exited non-zero, and every content-tier PR
    auto-merged on the cheap orchestrator model alone. An error detector had
    nothing to detect. Only "it has not worked lately" catches that shape.

    ``auditor/llm.py::record_judge_success`` stamps the file on every verdict.
    A missing file therefore means the judge has never produced one, which is
    exactly the condition that went unnoticed — so it alerts rather than
    staying quiet.

    The dedup id carries the last-success stamp, so a NEW stall after a
    recovery is a new incident rather than being swallowed as already-seen.
    """
    path = path or _judge_liveness_path()
    now = now or datetime.now(timezone.utc)

    last_raw = "never"
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
        last_raw = str(rec.get("last_success_at") or "never")
    except Exception:
        pass

    if last_raw != "never":
        last = _parse_iso(last_raw)
        if last is None:
            last_raw = "never"
        else:
            age_h = (now - last).total_seconds() / 3600.0
            if age_h < JUDGE_LIVENESS_HOURS:
                return []
            detail_age = f"{age_h:.0f}h ago ({last_raw})"
    if last_raw == "never":
        detail_age = "never — no successful judge run has ever been recorded"

    return [Incident(
        id=f"judge_liveness:{last_raw}",
        kind="judge_liveness",
        title="Auditor judge has not run — PR reviews are unaided",
        detail=(
            f"last successful judge verdict: {detail_age}\n"
            f"threshold: {JUDGE_LIVENESS_HOURS}h\n"
            "Every auditor verdict since then is the orchestrator model alone, "
            "including content-tier PRs that AUTO-MERGE."
        ),
        handoff=(
            "the auditor's second-LLM gate is not running — check "
            "`python -m auditor.llm --tier content --repo <r> --number <n>`; "
            "exit 4 means the credential is not resolving"
        ),
    )]


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


# --- dependency alerts -------------------------------------------------------
# Reachability tiers. The 242-alert backlog that prompted this signal was ~60%
# build tooling: the Docusaurus docs site and the Electron desktop app both
# carry lockfiles that never enter the production image (the Dockerfile installs
# root/web/ui-tui only, and `website/` is not copied at all). Reporting those at
# the same volume as a core runtime CVE is how a queue gets muted, so the brief
# says which tier an alert is in rather than pretending they are equivalent.
DEP_LOCKFILE_TIERS = {
    "uv.lock": "runtime (production image)",
    "package-lock.json": "runtime (web/ui-tui in image; apps/desktop is NOT)",
    "website/package-lock.json": "build-only (docs site, never in the image)",
}
DEP_SEVERITIES = ("critical", "high")
DEP_ROLLUP_THRESHOLD = 6  # more new packages than this in one sweep -> one rollup


# ── deploy drift ─────────────────────────────────────────────────────────────
# Production runs whatever image the Zeabur service tag points at; `main` moves
# on every merge. Nothing reconciles the two — `scripts/deploy.sh` is run by
# hand — so the gap is invisible until someone thinks to look. On 2026-09-22 it
# had reached 11 commits, one of which was a real fix that had been sitting
# undeployed. See tasks/deploy-automation-plan.md, "Half 1 (revised)", for why
# this is a signal rather than an auto-deploy.

DEPLOY_DRIFT_GRACE_HOURS = 6  # a same-day merge-then-deploy is not drift
# Paths a deploy cannot change the behaviour of. Deliberately TIGHT: under-
# filtering costs one unnecessary alert, over-filtering costs a missed deploy.
# `*.md` is NOT here — skill SKILL.md and AGENTS.md are read by the runtime —
# and neither is `.prompt`, which is the whole subject of Half 2.
_DEPLOY_INERT_PREFIXES = (".github/", "tasks/", "tests/")
_BUILD_SHA_FILE = Path("/opt/hermes/.hermes_build_sha")


def _deploy_drift_blind(reason: str, detail: str, *,
                        now: Optional[datetime] = None) -> Incident:
    """One incident saying the drift signal cannot see, not that all is well.

    Same reasoning as ``_blind_incident``: a producer that returns [] on every
    failure is indistinguishable from a healthy one with nothing to report,
    which is how the auditor judge went twelve weeks without running. The id
    carries the UTC date so this repeats daily until someone acts, rather than
    once ever.
    """
    now = now or _now()
    return Incident(
        id=f"deploy-drift-blind:{reason}:{now.strftime('%Y-%m-%d')}",
        kind="deploy",
        title=f"Deploy-drift signal is BLIND ({reason})",
        detail=(f"reason: {reason}\n{detail}\n"
                "Until this clears, 'no deploy drift' means 'cannot tell', not "
                "'production is current'."),
        handoff=("check the deploy-drift detector in incidents/sweep.py — "
                 "run `scripts/deploy.sh --status` by hand meanwhile"),
    )


def _fetch_deploy_compare(repo: str, base: str, token: str) -> Optional[dict]:
    """``base...main`` from the compare API.

    Three outcomes, and the difference is the point:
      * dict — the API answered.
      * None — transient (network, timeout, 5xx). Stay quiet; the next sweep is
               an hour away.
      * raise DependencyAlertBlind — refused (401/403/404). Never self-heals,
               so it must surface rather than become a reassuring silence.

    One call. ``files`` comes back as the AGGREGATE diff across the range, not
    per-commit; a per-commit breakdown would cost one request per commit, which
    is the wrong shape for an hourly sweep.
    """
    import urllib.error
    import urllib.request

    url = f"https://api.github.com/repos/{repo}/compare/{base}...main"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    # The repo is public, so compare needs no credential; a token only buys
    # rate limit. Unauthenticated is 60/h per egress IP, and this is one call
    # an hour.
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 (api.github.com)
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code in _BLIND_STATUSES:
            detail = f"compare {base}...main"
            if not token and (exc.headers or {}).get("X-RateLimit-Remaining") == "0":
                detail += (" — unauthenticated rate limit (60/h per IP) is spent; "
                           "set HERMES_DEPLOY_DRIFT_GITHUB_TOKEN")
            raise DependencyAlertBlind(exc.code, detail) from exc
        return None
    except Exception:
        return None


def _read_build_sha(path: Optional[Path] = None) -> str:
    """The commit the running image was built from, stamped by the Dockerfile.

    The watcher runs INSIDE the pod, so this is a local file read — no
    `zeabur service exec`, and no Zeabur credential anywhere in this signal.
    Absent on images built before HERMES_GIT_SHA was wired; report that rather
    than guessing "up to date", which is the failure this whole check exists
    to prevent.
    """
    path = path or _BUILD_SHA_FILE
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def deploy_drift_incidents(*, compare: Optional[dict] = None,
                           running_sha: Optional[str] = None,
                           repo: Optional[str] = None,
                           token: Optional[str] = None,
                           now: Optional[datetime] = None,
                           grace_hours: Optional[float] = None,
                           build_sha_path: Optional[Path] = None) -> List[Incident]:
    """Flag production running meaningfully behind ``main``.

    Fires only when ALL of these hold, so a normal merge-then-deploy is silent:
      * the aggregate range diff touches a path outside ``_DEPLOY_INERT_PREFIXES``
        — of the 11 founding commits, 10 were workflow-only;
      * the oldest undeployed commit is older than ``grace_hours``.

    Dedup is on the RUNNING sha, so one stale deployment is one alert that goes
    quiet when you deploy — not a fresh alert per subsequent merge.
    """
    now = now or _now()
    if grace_hours is None:
        try:
            grace_hours = float(os.environ.get("HERMES_DEPLOY_DRIFT_GRACE_HOURS")
                                or DEPLOY_DRIFT_GRACE_HOURS)
        except ValueError:
            grace_hours = DEPLOY_DRIFT_GRACE_HOURS

    if running_sha is None:
        running_sha = _read_build_sha(build_sha_path)
    if not running_sha:
        # A missing stamp is only alarming INSIDE the deployment. On a laptop or
        # in CI there is no /opt/hermes and no deployed image to be behind, so
        # the honest answer is "not applicable", not "blind" — otherwise every
        # developer's test run grows a spurious incident.
        sha_file = build_sha_path or _BUILD_SHA_FILE
        if not sha_file.parent.is_dir():
            return []
        return [_deploy_drift_blind(
            "no-build-sha",
            f"{sha_file} is missing or empty — the image predates the "
            "HERMES_GIT_SHA build-arg, or the file was not stamped.",
            now=now)]

    if compare is None:
        repo = repo or (os.environ.get("HERMES_DEPLOY_DRIFT_REPO")
                        or os.environ.get("GITHUB_REPOSITORY")
                        or "br41s/hermes-sandbox").strip()
        # A token is optional: the repo is public. And inside the watcher only
        # HERMES_DEPLOY_DRIFT_GITHUB_TOKEN can arrive at all — it runs as a
        # no-agent cron script, and the runner strips GITHUB_TOKEN / GH_TOKEN
        # from every script's env (tools/environments/local.py
        # _ALWAYS_STRIP_KEYS). Requiring a token is what kept this signal
        # BLIND (no-token) on every sweep since it shipped.
        token = token if token is not None else (
            os.environ.get("HERMES_DEPLOY_DRIFT_GITHUB_TOKEN")
            or os.environ.get("GITHUB_TOKEN")
            or os.environ.get("GH_TOKEN") or "").strip()
        try:
            compare = _fetch_deploy_compare(repo, running_sha, token)
        except DependencyAlertBlind as blind:
            return [_deploy_drift_blind(
                f"api-{blind.status}",
                f"the compare API refused: HTTP {blind.status} for "
                f"{repo} {running_sha}...main"
                + (f" ({blind.detail})" if blind.detail else "") + ".",
                now=now)]
        if compare is None:
            return []   # transient; try again next hour

    # GitHub's own verdict on the relationship, rather than inferring it.
    status = str(compare.get("status") or "")
    if status in ("behind", "diverged"):
        return [_deploy_drift_blind(
            "not-an-ancestor",
            f"production is serving {running_sha}, which is '{status}' relative to "
            "main — it is running code that is NOT on main. Do not deploy over "
            "this before working out what it is.",
            now=now)]

    ahead_by = compare.get("ahead_by") or 0
    if status == "identical" or not ahead_by:
        return []

    # `files` is the aggregate range diff. GitHub omits it entirely past ~300
    # files; treat a missing list as runtime-relevant rather than assuming the
    # drift is inert — silence is the dangerous direction here.
    files = compare.get("files")
    if files is None:
        runtime_files = None
    else:
        runtime_files = [f for f in files
                         if not str(f.get("filename") or "").startswith(_DEPLOY_INERT_PREFIXES)]
        if not runtime_files:
            return []

    commits = compare.get("commits") or []
    oldest = None
    for c in commits:
        ts = _parse_iso((((c.get("commit") or {}).get("committer") or {}).get("date")))
        if ts is not None and (oldest is None or ts < oldest):
            oldest = ts
    if oldest is not None and (now - oldest) < timedelta(hours=grace_hours):
        return []

    n_runtime = "unknown (too many to list)" if runtime_files is None else len(runtime_files)
    age = "unknown" if oldest is None else f"{(now - oldest).total_seconds() / 3600:.1f}h"
    return [Incident(
        id=f"deploy-drift:{running_sha}",
        kind="deploy",
        title=f"Production is {ahead_by} commit(s) behind main",
        detail=(f"running: {running_sha}\n"
                f"behind by: {ahead_by} commit(s)\n"
                f"runtime files changed: {n_runtime}\n"
                f"oldest undeployed commit: {age} ago\n"
                "(commits touching only .github/, tasks/ or tests/ do not count "
                "as runtime-relevant and never raise this on their own)"),
        handoff=("deploy it: `cd <repo> && git pull --ff-only origin main && "
                 "scripts/deploy.sh` — the image is already built and published "
                 "by Actions, so this is a tag move"),
    )]


def _dep_alert_lockfiles(alert: dict) -> List[str]:
    """Lockfile paths an osv-scanner alert names in its Affected Packages table.

    osv-scanner raises one alert per (advisory x lockfile), and the only place
    the lockfile appears is inside the rendered markdown of `rule.help`. There
    is no structured field for it, so this parses the table it documents.
    """
    help_text = (alert.get("rule") or {}).get("help") or ""
    out: List[str] = []
    for line in help_text.splitlines():
        cell = line.strip().strip("|").split("|")[0].strip()
        if cell.startswith("lockfile:"):
            path = cell.split("/github/workspace/")[-1].strip()
            if path and path not in out:
                out.append(path)
    return out


def _dep_alert_packages(alert: dict) -> List[tuple]:
    """(package, version) pairs from the alert's Affected Packages table."""
    help_text = (alert.get("rule") or {}).get("help") or ""
    out: List[tuple] = []
    in_table = False
    for line in help_text.splitlines():
        if line.startswith("### Affected Packages"):
            in_table = True
            continue
        if in_table and line.startswith("#"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if in_table and len(cells) == 3 and cells[0].startswith("lockfile:"):
            pair = (cells[1], cells[2])
            if pair not in out:
                out.append(pair)
    return out


class DependencyAlertBlind(Exception):
    """The alerts API refused us, and will keep refusing until someone acts.

    Distinct from a network blip on purpose. A transient failure resolves
    itself and deserves silence; a 401/403/404 is a standing condition that
    makes the whole signal report "nothing new" forever while the queue grows
    behind it. That is indistinguishable from healthy, which is how the
    auditor judge went twelve weeks without running.
    """

    def __init__(self, status: int, detail: str = ""):
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")


# HTTP statuses that mean "someone must change something", not "try again".
_BLIND_STATUSES = {401, 403, 404}


def _fetch_dependency_alerts(repo: str, token: str) -> Optional[List[dict]]:
    """Every OPEN code-scanning alert, following pagination.

    Three outcomes, and the difference between them is the whole point:
      * list  — the API answered.
      * None  — a transient failure (network, timeout, 5xx). Stay quiet; the
                next sweep is an hour away and it will probably work.
      * raise DependencyAlertBlind — the API refused us (401/403/404). This
                never fixes itself, so it has to be reported as an incident
                rather than swallowed into a reassuring silence.

    Pagination is not optional: an unpaginated query caps at 30 and would have
    reported 30 of the 242 alerts as the whole truth.
    """
    import urllib.error
    import urllib.request

    alerts: List[dict] = []
    for page in range(1, 21):  # 20 pages x 100 = 2000 alert ceiling
        url = (f"https://api.github.com/repos/{repo}/code-scanning/alerts"
               f"?state=open&per_page=100&page={page}")
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 (api.github.com)
                batch = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code in _BLIND_STATUSES:
                detail = ""
                try:
                    detail = (json.loads(exc.read().decode()) or {}).get("message", "")
                except Exception:
                    pass
                raise DependencyAlertBlind(exc.code, detail) from None
            return None
        except Exception:
            return None
        if not isinstance(batch, list) or not batch:
            break
        alerts.extend(batch)
        if len(batch) < 100:
            break
    return alerts


def _blind_incident(blind: "DependencyAlertBlind", repo: str, *,
                    now: Optional[datetime] = None) -> Incident:
    """One incident saying the signal cannot see, not that there is nothing.

    The id carries the UTC date, so this repeats once a day until someone
    fixes it rather than once ever. A single alert months ago is not a
    functioning signal, and being substantive output it also resets the
    heartbeat clock — otherwise the 24h "all clean" heartbeat would keep
    reassuring everyone while the watcher is blind.
    """
    now = now or _now()
    if blind.status == 404:
        why = ("code scanning may be disabled on the repo, or the token cannot see it. "
               "Check the Security tab still has an osv-scanner category.")
    else:
        why = ("the token lacks the `Code scanning alerts: Read` permission. "
               "Grant it on the fine-grained PAT, or set "
               "HERMES_DEP_ALERT_GITHUB_TOKEN to one that has it "
               "(the detector prefers that variable).")
    return Incident(
        id=f"depalert-blind:{blind.status}:{now.strftime('%Y-%m-%d')}",
        kind="dependency",
        title=f"Dependency alert signal is BLIND (HTTP {blind.status})",
        detail=(f"repo: {repo}\n"
                f"status: {blind.status}\n"
                f"api said: {blind.detail or '(no message)'}\n"
                f"why: {why}\n"
                f"Until this is fixed the dependency signal reports nothing new "
                f"every hour regardless of what is actually in the queue."),
        handoff=("fix the token permission, then confirm with: "
                 "python -c \"from incidents.sweep import dependency_alert_incidents as d; "
                 "print(len(d()))\" — a working signal returns a non-zero count "
                 "while critical/high alerts are open"),
    )


def dependency_alert_incidents(*, alerts: Optional[List[dict]] = None,
                               repo: Optional[str] = None,
                               token: Optional[str] = None,
                               now: Optional[datetime] = None) -> List[Incident]:
    """Open critical/high dependency advisories, grouped by package.

    Best-effort in the same sense as ``langfuse_error_incidents``: returns []
    on a missing token or any API problem rather than raising, because this
    signal must never be able to take the rest of the sweep down with it.

    Grouped by (lockfile, package) rather than per alert. osv-scanner raises one
    alert per (advisory x lockfile), so a single package routinely produces a
    dozen: Pillow 12.2.0 alone accounted for 13 of the 242 open alerts. Per-alert
    briefs would deliver that as thirteen Telegram messages about one pin.

    The incident id carries the package's full advisory set, so a package that
    picks up a NEW advisory re-reports once with its current state, while an
    unchanged package stays silent forever.
    """
    now = now or _now()
    if alerts is None:
        repo = repo or (os.environ.get("HERMES_DEP_ALERT_REPO")
                        or os.environ.get("GITHUB_REPOSITORY")
                        or "br41s/hermes-sandbox").strip()
        token = token or (os.environ.get("HERMES_DEP_ALERT_GITHUB_TOKEN")
                          or os.environ.get("GITHUB_TOKEN")
                          or os.environ.get("GH_TOKEN") or "").strip()
        if not token and not _BUILD_SHA_FILE.parent.is_dir():
            # A laptop or CI: no deployment, and no reason to expect a token.
            # "Not applicable", same rule as the deploy-drift build stamp.
            return []
        if not token:
            # Loud, not []: this used to return nothing, which inside the
            # watcher was permanent — it runs as a no-agent cron script and
            # the runner strips GITHUB_TOKEN / GH_TOKEN from every script's
            # env, so only HERMES_DEP_ALERT_GITHUB_TOKEN can ever arrive. An
            # empty answer read as "no new alerts".
            return [Incident(
                id=f"depalert-blind:no-token:{now.strftime('%Y-%m-%d')}",
                kind="dependency",
                title="Dependency alert signal is BLIND (no token)",
                detail=(f"repo: {repo}\n"
                        "No token reached the watcher. Cron scripts never see "
                        "GITHUB_TOKEN / GH_TOKEN (the runner strips them), so set "
                        "HERMES_DEP_ALERT_GITHUB_TOKEN — a PAT with "
                        "`Code scanning alerts: Read` on this repo.\n"
                        "Until then the dependency signal reports nothing new "
                        "every hour regardless of what is actually in the queue."),
                handoff=("set HERMES_DEP_ALERT_GITHUB_TOKEN in the Zeabur env, "
                         "redeploy, and confirm the next sweep drops this alert"),
            )]
        try:
            alerts = _fetch_dependency_alerts(repo, token)
        except DependencyAlertBlind as blind:
            return [_blind_incident(blind, repo, now=now)]
        if alerts is None:
            return []

    # (lockfile, package, version) -> {advisory id: severity}
    grouped: dict = {}
    for alert in alerts:
        rule = alert.get("rule") or {}
        sev = (rule.get("security_severity_level") or "").lower()
        if sev not in DEP_SEVERITIES:
            continue
        advisory = rule.get("id") or f"alert-{alert.get('number')}"
        lockfiles = _dep_alert_lockfiles(alert) or ["unknown"]
        packages = _dep_alert_packages(alert) or [("unknown", "?")]
        for lockfile in lockfiles:
            for pkg, version in packages:
                grouped.setdefault((lockfile, pkg, version), {})[advisory] = sev

    out: List[Incident] = []
    for (lockfile, pkg, version), advisories in sorted(grouped.items()):
        ids = sorted(advisories)
        worst = "critical" if "critical" in advisories.values() else "high"
        tier = DEP_LOCKFILE_TIERS.get(lockfile, "unknown reachability")
        shown = ", ".join(ids[:6]) + (f" (+{len(ids) - 6} more)" if len(ids) > 6 else "")
        out.append(Incident(
            # The advisory set is part of the id on purpose: an unchanged
            # package never re-reports, a package that gains an advisory
            # reports once more with its full current state.
            id=f"depalert:{lockfile}:{pkg}:{','.join(ids)}",
            kind="dependency",
            title=f"{worst.upper()} dependency advisory — {pkg} {version} ({lockfile})",
            detail=(f"package: {pkg} {version}\n"
                    f"lockfile: {lockfile}\n"
                    f"reachability: {tier}\n"
                    f"advisories ({len(ids)}): {shown}"),
            handoff=(f"triage {pkg} {version} in {lockfile} against ops/security/dependency-alert-triage.md — "
                     f"classify as accepted / unreachable / patchable / real exposure. "
                     f"If accepted, record it in osv-scanner.toml with the reason "
                     f"rather than leaving it to alert again"),
        ))
    return out


def _dep_rollup(incidents: List[Incident]) -> List[Incident]:
    """Collapse a large batch into one brief.

    A single osv-scanner run publishes every alert for a lockfile at once, so
    'new since last run' can still be dozens of packages — a dependency bump
    that shifts a whole transitive tree, or Advanced Security being switched on
    (which is how 242 alerts appeared in one morning). Delivering those
    individually is the failure mode this signal exists to avoid.
    """
    if len(incidents) <= DEP_ROLLUP_THRESHOLD:
        return incidents
    crit = [i for i in incidents if i.title.startswith("CRITICAL")]
    names = ", ".join(sorted({i.detail.splitlines()[0].split(": ", 1)[1] for i in incidents})[:12])
    return [Incident(
        id="depalert-rollup:" + str(hash(tuple(sorted(i.id for i in incidents))) & 0xFFFFFFFF),
        kind="dependency",
        title=f"{len(incidents)} packages with new critical/high dependency advisories",
        detail=(f"critical: {len(crit)}\npackages: {names}"
                f"{' …' if len(incidents) > 12 else ''}\n"
                f"Reported as one brief — a batch this size is a lockfile-wide shift "
                f"or a scanner/config change, not {len(incidents)} separate decisions."),
        handoff=("open the Security tab (Code Scanning > osv-scanner) and triage the batch "
                 "against ops/security/dependency-alert-triage.md — group by package, not by alert"),
    )]


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
          checkout_drift: Optional[List[Incident]] = None,
          deploy_drift: Optional[List[Incident]] = None,
          judge_liveness: Optional[List[Incident]] = None,
          dependency_alerts: Optional[List[Incident]] = None,
          state_path: Optional[Path] = None,
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
    dd = deploy_drift if deploy_drift is not None else deploy_drift_incidents(now=now)
    jl = judge_liveness if judge_liveness is not None else judge_liveness_incidents(now=now)

    # Dependency advisories are handled apart from the other signals because
    # they need two things none of the others do: a baseline (the standing
    # backlog is not news) and a rollup (one scanner run publishes a whole
    # lockfile's alerts at once).
    da_raw = (dependency_alerts if dependency_alerts is not None
              else dependency_alert_incidents(now=now))
    # A "signal is blind" incident is never backlog, so it must not be eligible
    # for baselining — otherwise the very first sweep on a fresh state adopts it
    # as part of the starting queue and the watcher goes quiet about being
    # unable to see. It is also exempt from the rollup: it is one fact about the
    # watcher itself, not one of N packages.
    da_blind = [i for i in da_raw if i.id.startswith("depalert-blind:")]
    da_raw = [i for i in da_raw if not i.id.startswith("depalert-blind:")]
    da_new = [i for i in da_raw if i.id not in seen]
    baseline_established = False
    if da_new and not state.get("dep_alerts_baselined"):
        # First run ever: adopt whatever is already open as the starting point
        # and say nothing. Without this the first sweep delivers the entire
        # backlog — 242 alerts, the exact outcome this signal exists to prevent.
        for i in da_new:
            seen.add(i.id)
            seen_list.append(i.id)
        state["dep_alerts_baselined"] = True
        baseline_established = True
        da_new = []
    da_covered = [i.id for i in da_new]
    da_new = _dep_rollup(da_new) + [i for i in da_blind if i.id not in seen]

    incidents = (cron_failure_incidents(jobs, now=now)
                 + cron_stale_incidents(jobs, now=now)
                 + prompt_drift_incidents(jobs)
                 + runaway_incidents(now=now)
                 + list(lf) + list(bc) + list(cd) + list(dd) + list(jl))
    new = [i for i in incidents if i.id not in seen] + da_new

    incident_text = ""
    if new:
        incident_text = "\n\n".join(_format_brief(i) for i in new)
        for i in new:
            seen.add(i.id)
            seen_list.append(i.id)
        # A rollup brief speaks for ids that are not in `new` themselves;
        # retire them too or the next sweep rolls the same batch up again.
        for alert_id in da_covered:
            if alert_id not in seen:
                seen.add(alert_id)
                seen_list.append(alert_id)

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

    # `baseline_established` is in the save condition on purpose: the baseline
    # run deliberately produces no output, and if it were not persisted the
    # next sweep would re-derive the whole backlog as new.
    if not dry_run and (output or baseline_established):
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
