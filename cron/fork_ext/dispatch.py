"""Webhook entry points onto upstream's cron pools, and the profile-store test (fork-owned).

Stage 3 step 5 (``ops/multiplex-stage3-plan.md``) retired the fork's single-thread lane:
every due job now runs on upstream's per-profile parallel pool, which boot sizes to 1 on
every named profile (``boot_reconcile.PROFILE_OVERRIDES``), so a profile's jobs stay
serial while profiles run side by side. What is left here:

- ``in_profile_store`` — whether this context runs a profile's OWN store (the identity
  path in ``profile_scope`` and the scheduler's ``.env`` guard key on it);
- ``dispatch_job_async`` — the fork's ``trigger_cron_job_id`` webhook: enqueue on the
  job's pool, never inline in the gateway event loop;
- ``run_event_job`` — upstream's ``cron_job`` webhook route: a profile-store job goes to
  that profile's pool, so it queues behind the profile's tick jobs instead of
  overlapping them on a worker thread of its own.
"""

import contextvars
import logging
from typing import Callable

# Same logger as before the move, so agent.log lines keep their
# ``cron.scheduler`` name.
logger = logging.getLogger("cron.scheduler")


def in_profile_store() -> bool:
    """True while this context runs under a profile home other than the launch one.

    The multiplex ticker (``_profile_cron_scope``) and a routed webhook both install
    the profile's home override before they resolve or run a job, so the active home
    names the store the job came from. Fails closed: if either home cannot be
    resolved, the run is treated as a profile-store run (identity tripwire, no
    process-env ``.env`` load).
    """
    try:
        from hermes_constants import get_hermes_home, get_routing_process_hermes_home

        return get_hermes_home().resolve() != get_routing_process_hermes_home().resolve()
    except Exception as exc:
        # Visible on purpose: a home that keeps failing to resolve puts every run
        # through the profile-store path, which otherwise reads as odd identity errors.
        logger.warning("in_profile_store: could not resolve the active or launch home "
                       "(%s); treating the run as a profile-store run", type(exc).__name__)
        return True


def dispatch_job_async(job: dict) -> dict:
    """Enqueue a job on the pool ``tick`` uses for its store, fire-and-forget, and
    return immediately without running it inline.

    The webhook direct-trigger (``trigger_cron_job_id``) used to run ``run_one_job``
    INLINE in the gateway event loop, beside tick's runs of the same profile: that
    once leaked one profile's identity into another's ``gh``/``git`` (biglobster
    content PRs authored as ``hermes-auditor``) and blocked the event loop for the
    whole multi-minute run. Identity no longer lives in ``os.environ``, but a
    profile's jobs must still not overlap: its pool is sized 1
    (``boot_reconcile.PROFILE_OVERRIDES``), so a webhook run queues behind the
    profile's tick runs.

    Honors the same in-flight dedup guard as tick (``try_register_running_job``): a job
    already running (from a tick or a prior trigger) is not re-dispatched.
    Fire-and-forget — the caller does not wait for the run. Returns
    ``{"queued": bool, "reason": str | None}``.
    """
    import cron.scheduler as sched

    job_id = job.get("id")
    if not job_id:
        return {"queued": False, "reason": "job has no id"}
    if sched._interpreter_shutting_down():
        return {"queued": False, "reason": "interpreter shutting down"}

    # The active store's pool, exactly as tick picks it (sized by that profile's config).
    pool = sched._get_parallel_pool(sched._resolve_max_parallel_workers())

    # Upstream's single dedupe owner (v2026.8.31): also makes the run visible
    # to the gateway shutdown drain and the stale in-flight sweep.
    if not sched.try_register_running_job(job_id):
        return {"queued": False, "reason": "already running"}

    # The home the claim was registered under: the worker's ``finally`` runs outside ``ctx``, so
    # release under it explicitly (upstream v2026.9.24's _submit_with_guard does the same).
    claim_home = sched._get_hermes_home()
    ctx = contextvars.copy_context()

    def _run_and_release(j=job, c=ctx, home=claim_home):
        try:
            return c.run(sched.run_one_job, j)
        finally:
            sched.release_running_job(j["id"], home=home)

    try:
        pool.submit(_run_and_release)
        return {"queued": True, "reason": None}
    except Exception as submit_err:
        sched.release_running_job(job_id, home=claim_home)
        logger.error("dispatch_job_async: job '%s' not dispatched: %s", job_id, submit_err)
        return {"queued": False, "reason": f"dispatch failed: {submit_err}"}


async def run_event_job(job_ref: str, fire: Callable, *args):
    """Await ``fire(*args)`` for upstream's webhook ``cron_job`` route.

    Upstream's ``_handle_cron_trigger`` runs ``execute_job_for_event`` through
    ``asyncio.to_thread``, a fresh worker per event. In a profile's own store that
    would overlap the profile's tick jobs, so there the event goes to the profile's
    pool (sized 1), behind them. Only WHERE it runs changes: ``fire`` still resolves,
    claims, dedupes, injects the event context and delivers when the pool reaches it,
    the same way tick's own jobs claim when they start, so a tick that fires the same
    job first wins the claim and the event reports "already being fired" as upstream
    would. The launch store keeps upstream's ``to_thread``.
    """
    import asyncio

    if not in_profile_store():
        return await asyncio.to_thread(fire, *args)
    import cron.scheduler as sched

    # to_thread copies the caller's context (the routed profile's scope); a bare
    # executor.submit does not, so carry it explicitly.
    ctx = contextvars.copy_context()
    pool = sched._get_parallel_pool(sched._resolve_max_parallel_workers())
    return await asyncio.wrap_future(pool.submit(ctx.run, fire, *args))
