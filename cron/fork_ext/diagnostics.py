"""Diagnostics for cron agent runs that hit the inactivity timeout (fork-owned).

Moved out of ``cron/scheduler.py`` so the fork's code does not sit inside
upstream's file. ``cron.scheduler`` re-imports every name below, so
``from cron.scheduler import abandoned_agent_threads`` (``hermes_cli/cron.py``)
and patches of ``cron.scheduler.abandoned_agent_threads`` keep working. The
counter itself lives here, and it is the only copy.
"""

import logging
import threading

# Same logger as before the move, so agent.log lines keep their
# ``cron.scheduler`` name and tests patching ``cron.scheduler.logger`` still
# see these calls.
logger = logging.getLogger("cron.scheduler")


_ABANDONED_AGENT_THREADS = 0
_ABANDONED_LOCK = threading.Lock()


def abandoned_agent_threads() -> int:
    """How many agent threads this process gave up on but could not stop.

    An inactivity timeout ends the *wait*, not the work. The agent runs in a
    ThreadPoolExecutor in THIS process and Python cannot kill a running
    thread; ``shutdown(cancel_futures=True)`` only drops QUEUED futures. The
    wedged thread therefore keeps its whole heap AND keeps the interpreter
    alive, because ``concurrent.futures`` registers an atexit hook that joins
    every worker — so a normal ``sys.exit`` blocks forever.

    Measured 2026-09-22: four wedged Infographic Engineer runs left four
    ``hermes cron run`` processes alive, ~280 MB each, the oldest 1h32m, on a
    7.6 GB container down to 213 MB free.

    A process that owns itself (``hermes cron run``) uses this to decide it
    must hard-exit rather than return. A long-lived scheduler cannot, and
    leaks the thread instead — which is why this is a count, not a boolean.

    This is also why the ``_cron_pool.shutdown(wait=False, cancel_futures=True)``
    in ``_run_job_impl``'s ``finally`` must not be read as a kill: on an
    inactivity timeout the agent thread survives it, holding its full heap
    (three such orphans accumulated on 2026-09-22 before anyone noticed), and
    only ``_exit_hard_if_threads_abandoned`` in ``hermes_cli/cron.py`` gets
    the process out.
    """
    with _ABANDONED_LOCK:
        return _ABANDONED_AGENT_THREADS


def _note_abandoned_agent_thread() -> int:
    global _ABANDONED_AGENT_THREADS
    with _ABANDONED_LOCK:
        _ABANDONED_AGENT_THREADS += 1
        return _ABANDONED_AGENT_THREADS


def _dump_stuck_agent_stack(job_id: str, idle_secs: float) -> None:
    """Dump every thread's stack when a job hits the inactivity timeout.

    Capture what the agent is actually stuck on BEFORE we give up on it. The
    message this timeout produces names the last recorded *activity*, which is
    not the same as what the thread is blocked on and has already sent one
    investigation down the wrong path: on 2026-09-22 it reported "waiting for
    non-streaming API response" four times while the process held zero open
    sockets and the request had completed minutes earlier.

    The agent runs in _cron_pool, a thread in THIS process, so a plain
    faulthandler dump covers it. An external profiler does not work here: the
    Zeabur pod has ptrace_scope=2 and no CAP_SYS_PTRACE, so py-spy is refused
    even as root.

    Never raises.
    """
    try:
        from hermes_cli.stackdump import dump_now, dump_path
        dump_now()
        logger.error(
            "Job '%s': inactivity timeout after %.0fs — "
            "all-thread stack dumped to %s",
            job_id, idle_secs, dump_path(),
        )
    except Exception:
        logger.exception(
            "Job '%s': inactivity timeout after %.0fs "
            "(stack dump unavailable)", job_id, idle_secs,
        )
