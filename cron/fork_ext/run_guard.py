"""Per-job run guard for cron runs (fork-owned).

Moved out of ``cron/scheduler.py`` so the fork's code does not sit inside
upstream's file. ``cron.scheduler.run_job`` is a one-line call into
:func:`guarded_run_job`, and upstream's own ``run_job`` body lives on there as
``_run_job_impl``:

- ``_job_run_lock`` — non-blocking per-``job_id`` flock, so two triggers of the
  same job (tick + webhook burst) never both run it;
- ``guarded_run_job`` — takes that lock, refuses a record that still carries the
  retired fork ``profile`` field, then runs a job from a profile's own store
  under ``_satellite_store_context``.

``_get_hermes_home`` is looked up on ``cron.scheduler`` at call time, so patches
of that attribute still apply.
"""

import logging
from contextlib import contextmanager
from typing import Callable

from cron.fork_ext.profile_scope import _refuse_legacy_profile_record, _satellite_store_context

try:
    import fcntl
except ImportError:
    fcntl = None

# Same logger as before the move, so agent.log lines keep their
# ``cron.scheduler`` name.
logger = logging.getLogger("cron.scheduler")


@contextmanager
def _job_run_lock(job_id: str):
    """Non-blocking per-job exclusion lock. Yields True if acquired (run the
    job), False if another run of the SAME job_id already holds it (skip — the
    in-flight run will do the work).

    Why: the scheduler tick and the webhook direct-trigger both reach run_job(),
    and the ``.tick.lock`` only serializes ticks — a webhook path bypasses it.
    A burst of PR webhook events (opened + synchronize + ...) plus the periodic
    poll can launch several concurrent runs of one job. For the auditor that
    means two runs each list the same PR as pending (neither has marked it yet,
    since the mark happens mid-review) and both post a review — the duplicate
    reviews seen on FinView #206. A plain file flock is enough: same-host,
    same-user processes, auto-released on close or process death (no stale
    lock). Keyed under the DEFAULT hermes home and acquired before the run's
    profile context. Degrades to a no-op (runs anyway) where POSIX
    flock is unavailable (Windows/dev) or the lock dir can't be created."""
    import cron.scheduler as sched

    if fcntl is None:
        yield True
        return
    lock_dir = sched._get_hermes_home() / "cron"
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        fh = open(lock_dir / f".job-{job_id}.lock", "w", encoding="utf-8")
    except OSError:
        yield True
        return
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            yield False
            return
        try:
            yield True
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        fh.close()


def guarded_run_job(job: dict, impl: Callable, **kwargs) -> tuple:
    """Run ``impl(job, **kwargs)`` under the per-job lock and, for a job from a
    profile's own store, that profile's identity context.

    Serialized per job_id: if another run of this same job is already in
    flight (a concurrent webhook trigger or the periodic tick), this returns a
    SILENT success (no output → no delivery, no error alert) instead of
    launching a second concurrent run that would re-review the same PRs. See
    ``_job_run_lock`` for the full rationale (duplicate auditor reviews).

    A record still carrying the retired ``profile`` field raises
    ``LegacyProfileJobError`` (stage 3 step 2) instead of running under the
    default identity. Every keyword is forwarded to the impl untouched —
    today that is ``defer_agent_teardown`` (upstream's delivery-ordering fix,
    #58720); from v2026.8.31 ``run_one_job`` also passes ``extra_prompt``,
    ``execution_id`` and ``cancel_event``, and a wrapper that named its
    keywords would reject every run with a TypeError.
    """
    import cron.scheduler as sched

    job_id = job["id"]
    with _job_run_lock(job_id) as acquired:
        if not acquired:
            logger.info(
                "Job '%s': another run already in progress — skipping this "
                "trigger (silent, the in-flight run handles it)", job_id,
            )
            return True, "", "", None
        _refuse_legacy_profile_record(job)
        with _satellite_store_context(job_id):
            return impl(job, **kwargs)
