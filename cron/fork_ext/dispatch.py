"""The fork's sequential cron lane (fork-owned).

Every cron job that sets ``profile`` or ``workdir`` runs on ONE single-thread
executor, one at a time, across ticks and across entry points (the tick and
the webhook trigger).

History: a profile run used to load its ``.env`` (its ``GITHUB_TOKEN`` among
it) into ``os.environ`` and a workdir run wrote ``TERMINAL_CWD``, so two
overlapping runs leaked one identity into the other. Neither is true any
more — the profile ``.env`` is the run's secret scope
(``cron/fork_ext/profile_scope.py``, #338) and v2026.8.31 scopes the workdir
per task — but the lane stays: it keeps profile runs in the order and pacing
production has always had, and widening it is a separate decision (CLAUDE.md,
"One long agent run starves every other agent").

Upstream deleted its own sequential pool and dispatches every due job in
parallel (``parallel_jobs = due_jobs``); the fork re-anchors one call in
upstream's ``tick``:

    parallel_jobs = _fork_submit_sequential(parallel_jobs, _submit_with_guard, _all_futures, _results, sync)

and ``tests/cron/test_sequential_dispatch_fork.py`` fails if it is lost.

Collaborators that stay in ``cron.scheduler`` (``run_one_job``, the running-job
guard, the parallel pool, ``_interpreter_shutting_down``) are looked up on that
module at call time, so existing ``cron.scheduler`` monkeypatches still apply.
"""

import atexit
import concurrent.futures
import contextvars
import logging
import threading
from typing import Callable, Optional

# Same logger as before the move, so agent.log lines keep their
# ``cron.scheduler`` name.
logger = logging.getLogger("cron.scheduler")

_sequential_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
# Guards lazy creation. The tick thread and the webhook path (gateway event
# loop) can both reach get_sequential_executor(); without the lock a first-use
# race could build TWO single-thread executors, i.e. two concurrent workers.
_sequential_executor_lock = threading.Lock()


def is_sequential(job: dict) -> bool:
    """True for a job the fork runs on the single-thread lane: any job with a
    ``profile`` or a ``workdir``. Neither mutates process-global state any more
    (see the module docstring); the lane is kept by policy, and widening it is
    a separate decision. Jobs with neither field go to upstream's parallel pool.
    """
    return bool((job.get("workdir") or "").strip() or (job.get("profile") or "").strip())


def get_sequential_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Return (or create) the persistent single-thread executor.

    A single worker guarantees env-mutating jobs never overlap, even across
    ticks: a job queued by a newer tick (or a webhook trigger) waits for the
    previous one to finish rather than corrupting its ``os.environ`` state.
    Thread names keep the ``cron-seq`` prefix they had on upstream's pool.
    """
    global _sequential_executor
    with _sequential_executor_lock:
        if _sequential_executor is None:
            _sequential_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="cron-seq",
            )
        return _sequential_executor


def shutdown_sequential_executor() -> None:
    """Shut the executor down (waiting for the running job, keeping queued
    ones) and forget it, so the next use creates a fresh one.

    ``cron.scheduler._shutdown_parallel_pool`` calls this, exactly as it used
    to shut down upstream's sequential pool — tests rely on that call to drain
    the lane between cases. Also registered with ``atexit`` on its own, so the
    lane is still drained at exit if a merge ever drops that call."""
    global _sequential_executor
    with _sequential_executor_lock:
        executor, _sequential_executor = _sequential_executor, None
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=False)


atexit.register(shutdown_sequential_executor)


def submit_sequential_jobs(
    due_jobs: list,
    submit: Callable,
    futures: list,
    results: list,
    sync: bool,
) -> list:
    """Dispatch ``tick``'s profile/workdir jobs onto the sequential lane.

    ``submit`` is ``tick``'s own ``_submit_with_guard(job, pool)``, so the
    in-flight dedup guard, execution record and shutdown handling are exactly
    upstream's. Futures go into ``futures``; in async mode each dispatched job
    is optimistically counted in ``results`` (as ``tick`` does for its own
    passes). Returns the remaining parallel-safe jobs, in their original order.
    """
    sequential_jobs = [j for j in due_jobs if is_sequential(j)]
    if sequential_jobs:
        executor = get_sequential_executor()
        for job in sequential_jobs:
            fut = submit(job, executor)
            if fut is None:
                continue
            futures.append(fut)
            if not sync:
                results.append(True)  # optimistically counted
    return [j for j in due_jobs if not is_sequential(j)]


def dispatch_job_async(job: dict) -> dict:
    """Enqueue a job on the SAME lanes ``tick`` uses, fire-and-forget, and
    return immediately without running it inline.

    Why this exists: a job with a profile (or workdir) mutates process-global
    state inside ``run_job`` — most importantly the profile's
    ``GITHUB_TOKEN``/``GH_TOKEN`` in ``os.environ``. ``tick`` keeps those jobs
    on the single-thread SEQUENTIAL lane so only one runs at a time. But the
    webhook direct-trigger used to run ``run_one_job`` INLINE (in the gateway
    event loop), concurrently with a tick-dispatched profile job — the two then
    raced on ``os.environ`` and one profile's identity leaked into the other's
    ``gh``/``git`` subprocess. That is how biglobster content PRs got authored
    as ``hermes-auditor`` (so the auditor skipped its own PR): a content job's
    ``gh pr create`` inherited the auditor's leaked token while the auditor ran
    from a PR webhook. Routing webhook runs through the same sequential lane
    serializes them with tick's profile jobs, so no two identities mutate the
    env at once — and it also stops the multi-minute run from blocking the
    event loop.

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

    # Same partition rule as tick: profile/workdir jobs are env-mutating and
    # MUST run on the single-thread sequential lane; everything else is
    # parallel-safe.
    pool = (
        get_sequential_executor()
        if is_sequential(job)
        else sched._get_parallel_pool(sched._parallel_pool_max_workers)
    )

    # Upstream's single dedupe owner (v2026.8.31): also makes the run visible
    # to the gateway shutdown drain and the stale in-flight sweep.
    if not sched.try_register_running_job(job_id):
        return {"queued": False, "reason": "already running"}

    ctx = contextvars.copy_context()

    def _run_and_release(j=job, c=ctx):
        try:
            return c.run(sched.run_one_job, j)
        finally:
            sched.release_running_job(j["id"])

    try:
        pool.submit(_run_and_release)
        return {"queued": True, "reason": None}
    except Exception as submit_err:
        sched.release_running_job(job_id)
        logger.error("dispatch_job_async: job '%s' not dispatched: %s", job_id, submit_err)
        return {"queued": False, "reason": f"dispatch failed: {submit_err}"}
