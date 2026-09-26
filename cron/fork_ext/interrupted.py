"""Mark a cron job's record interrupted after restart recovery (fork-owned).

Moved verbatim out of ``cron/jobs.py`` so the fork's code does not sit inside
upstream's file. The one caller is the restart-recovery hook in
``cron/scheduler_provider.py``.

Upstream (v2026.9.24) has no equivalent of this function: its shutdown path
(``mark_running_jobs_interrupted``) records an interrupted run through
``mark_job_run(job_id, False, reason, ...)``, which DOES advance
``last_run_at`` — exactly what the docstring below explains this function
avoids. Keep the two apart when merging.

``cron.jobs`` is imported lazily, inside the function: it is the jobs store
this writes to, and resolving ``_jobs_lock`` / ``load_jobs`` / ``save_jobs``
as ``cron.jobs`` attributes at call time keeps every test that repoints
``cron.jobs.CRON_DIR`` / ``JOBS_FILE`` (or patches those functions) applying
here, just as it did when this lived in that module.
"""

from datetime import datetime
from typing import Optional


def mark_job_interrupted(job_id: str, *, reason: str, at: str,
                         not_before: Optional[str] = None) -> bool:
    """Record that a run died mid-flight, without claiming it ever completed.

    Called from the scheduler provider's restart-recovery hook for each attempt
    the ledger proved abandoned (see ``cron.executions``). A killed run never
    reaches :func:`cron.jobs.mark_job_run`, so ``last_status`` otherwise keeps
    pointing at an OLDER success — the exact failure mode that hid the
    2026-09-17 11:12 run of the Shoroban product-sheet job behind a
    healthy-looking ``ok``.

    Deliberately narrow. It writes ``last_status`` / ``last_error`` /
    ``last_interrupted_at`` and NOTHING else:

    * ``last_run_at`` is NOT advanced. ``incidents.sweep.cron_stale_incidents``
      derives a job's stall deadline from it as "last COMPLETED run"; stamping
      it here would reset that deadline and silence the only detector that
      caught this incident. An interrupted attempt is not a run.
    * ``next_run_at`` / ``repeat.completed`` / ``state`` are untouched — the
      tick already advanced the schedule, and a finite one-shot already burned
      its dispatch via ``claim_dispatch``. Re-deriving them here would either
      double-count or re-arm a job whose side effects may have run.
    * ``run_claim`` / ``fire_claim`` are left alone. Both carry their own TTL
      and expire on their own; clearing them would re-open a fire for a run
      that may have already written to a live client site.

    ``not_before`` is the attempt's ``claimed_at``. A completed run that is
    newer than the attempt supersedes it, so the stale interruption is dropped
    rather than overwriting a good result — this happens whenever recovery runs
    against ledger rows left by an older boot. Timestamps are compared as
    instants, not strings: both are local-offset ISO, so a server timezone
    change between boots would invert a naive lexicographic compare. If either
    stamp is unparseable the interruption is applied anyway — failing toward
    visibility is the whole point of this function.

    Returns True if the job record was updated.
    """
    from cron import jobs as _jobs

    with _jobs._jobs_lock():
        jobs = _jobs.load_jobs()
        for job in jobs:
            if job["id"] != job_id:
                continue
            if not_before and job.get("last_run_at"):
                try:
                    superseded = (
                        _jobs._ensure_aware(datetime.fromisoformat(str(job["last_run_at"])))
                        > _jobs._ensure_aware(datetime.fromisoformat(str(not_before)))
                    )
                except (TypeError, ValueError):
                    superseded = False
                if superseded:
                    return False  # a later run already completed; keep its result
            job["last_status"] = "interrupted"
            job["last_error"] = reason
            job["last_interrupted_at"] = at
            _jobs.save_jobs(jobs)
            return True
    return False


def recover_interrupted_records() -> list:
    """Run upstream's ledger recovery and return the rows it just marked.

    ``cron.executions.recover_interrupted_executions`` returns only a count;
    the restart hook (``CronScheduler.recover_interrupted``) needs the rows to
    mark each job interrupted. Calling upstream's function unchanged (looked up
    on its module, so tests that patch it still apply) and reading the fresh
    ``unknown`` rows back keeps ``cron/executions.py`` identical to upstream.
    """
    from cron import executions
    from hermes_time import now as _hermes_now

    started = _hermes_now().isoformat()
    count = executions.recover_interrupted_executions()
    if not count:
        return []
    rows = executions.list_executions(limit=max(200, count * 4))
    return [
        row for row in rows
        if row.get("status") == "unknown"
        and (row.get("finished_at") or "") >= started
        and (row.get("error") or "").startswith("Scheduler restarted after")
    ][:count]
