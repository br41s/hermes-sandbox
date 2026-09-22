"""A wedged agent must not leave the runner process alive forever.

THE BUG

``hermes cron run`` executes the agent in THIS process. When the inactivity
watchdog gives up it calls ``shutdown(wait=False, cancel_futures=True)``, which
only drops QUEUED futures — Python cannot kill a thread that is already
running. So the wedged thread survives, and because ``concurrent.futures``
registers an atexit hook that joins every worker, the interpreter cannot exit
either. The run is recorded, the failure is delivered to Telegram, and the
process then sits there holding its whole heap.

Measured 2026-09-22: four wedged Infographic Engineer runs left four processes
alive at ~280 MB each, the oldest 1h32m, on a 7.6 GB container that was down to
213 MB free. They had to be killed by hand.

WHY THE INTERESTING TEST SPAWNS PROCESSES

The failure is "the interpreter never exits". That cannot be asserted in-process
— the only honest check is to start a real Python, wedge it the same way, and
watch whether it terminates. The first test below deliberately reproduces the
HANG, so that if some future Python or executor change makes exit non-blocking,
we find out by that test failing rather than by trusting a comment.
"""
import subprocess
import sys
import textwrap

import pytest

WEDGE = """
import concurrent.futures, threading, time
pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
never = threading.Event()
pool.submit(never.wait)          # the agent thread that wedges
time.sleep(0.3)                  # let the worker actually start
pool.shutdown(wait=False, cancel_futures=True)   # what the watchdog does
print("shutdown-returned", flush=True)
"""


def _run(body: str, timeout: float):
    proc = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(body)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        proc.communicate(timeout=timeout)
        return proc.returncode
    except subprocess.TimeoutExpired:
        return None  # still alive == hung
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_shutdown_does_not_stop_a_running_agent_thread():
    """The bug itself. If this ever passes quickly, the fix may be unnecessary."""
    rc = _run(WEDGE + "\n", timeout=6)
    assert rc is None, (
        "the process exited on its own, so ThreadPoolExecutor no longer blocks "
        "interpreter shutdown on a running worker. Re-check whether the hard "
        "exit in hermes_cli/cron.py is still needed."
    )


def test_hard_exit_terminates_a_wedged_runner():
    """The fix: os._exit gets out even with a live worker thread."""
    rc = _run(WEDGE + "\nimport os, sys\nsys.stdout.flush()\nos._exit(0)\n", timeout=15)
    assert rc == 0, f"the wedged process did not exit cleanly (rc={rc})"


# ---------------------------------------------------------------- the counter

def test_counter_starts_at_zero_and_increments():
    from cron import scheduler

    before = scheduler.abandoned_agent_threads()
    scheduler._note_abandoned_agent_thread()
    assert scheduler.abandoned_agent_threads() == before + 1
    scheduler._note_abandoned_agent_thread()
    assert scheduler.abandoned_agent_threads() == before + 2


# ------------------------------------------------------------------- the CLI

def test_cli_returns_normally_when_nothing_was_abandoned(monkeypatch):
    """The overwhelmingly common path must not hard-exit."""
    from cron import scheduler
    from hermes_cli import cron as cli

    monkeypatch.setattr(scheduler, "abandoned_agent_threads", lambda: 0)
    assert cli._exit_hard_if_threads_abandoned(0) == 0
    assert cli._exit_hard_if_threads_abandoned(3) == 3


def test_cli_hard_exits_when_a_thread_was_abandoned(monkeypatch):
    from cron import scheduler
    from hermes_cli import cron as cli

    monkeypatch.setattr(scheduler, "abandoned_agent_threads", lambda: 2)

    called = {}

    def fake_exit(code):
        called["code"] = code
        raise SystemExit(code)  # stand in for os._exit, which is unmockable-ish

    monkeypatch.setattr(cli.os, "_exit", fake_exit)
    with pytest.raises(SystemExit):
        cli._exit_hard_if_threads_abandoned(0)
    assert called["code"] == 0, "the caller's exit code must be preserved"


def test_cli_survives_a_broken_scheduler_import(monkeypatch):
    """A diagnostic helper must never turn into the reason a run fails."""
    from hermes_cli import cron as cli

    import builtins

    real_import = builtins.__import__

    def boom(name, *a, **kw):
        if name == "cron.scheduler":
            raise ImportError("simulated")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", boom)
    assert cli._exit_hard_if_threads_abandoned(7) == 7
