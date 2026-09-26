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
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

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
        "exit in cron/fork_ext/cli.py is still needed."
    )


def test_hard_exit_terminates_a_wedged_runner():
    """The fix: os._exit gets out even with a live worker thread."""
    rc = _run(WEDGE + "\nimport os, sys\nsys.stdout.flush()\nos._exit(0)\n", timeout=15)
    assert rc == 0, f"the wedged process did not exit cleanly (rc={rc})"


# ---------------------------------------------------------------- the counter

def test_counter_starts_at_zero_and_increments():
    from cron.fork_ext import diagnostics

    before = diagnostics.abandoned_agent_threads()
    diagnostics._note_abandoned_agent_thread()
    assert diagnostics.abandoned_agent_threads() == before + 1
    diagnostics._note_abandoned_agent_thread()
    assert diagnostics.abandoned_agent_threads() == before + 2


# ------------------------------------------------------------------- the CLI

def test_cli_returns_normally_when_nothing_was_abandoned(monkeypatch):
    """The overwhelmingly common path must not hard-exit."""
    from cron import scheduler
    from cron.fork_ext import cli

    monkeypatch.setattr(scheduler, "abandoned_agent_threads", lambda: 0)
    assert cli.exit_hard_if_threads_abandoned(0) == 0
    assert cli.exit_hard_if_threads_abandoned(3) == 3


def test_cli_hard_exits_when_a_thread_was_abandoned(monkeypatch):
    from cron import scheduler
    from cron.fork_ext import cli

    monkeypatch.setattr(scheduler, "abandoned_agent_threads", lambda: 2)

    called = {}

    def fake_exit(code):
        called["code"] = code
        raise SystemExit(code)  # stand in for os._exit, which is unmockable-ish

    monkeypatch.setattr(cli.os, "_exit", fake_exit)
    with pytest.raises(SystemExit):
        cli.exit_hard_if_threads_abandoned(0)
    assert called["code"] == 0, "the caller's exit code must be preserved"


def test_cli_survives_a_broken_scheduler_import(monkeypatch):
    """A diagnostic helper must never turn into the reason a run fails."""
    from cron.fork_ext import cli

    import builtins

    real_import = builtins.__import__

    def boom(name, *a, **kw):
        if name == "cron.scheduler":
            raise ImportError("simulated")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", boom)
    assert cli.exit_hard_if_threads_abandoned(7) == 7


# ------------------------------------ the reap must beat a blocked stdout ---

def test_exit_happens_even_when_stdout_is_a_full_unread_pipe(tmp_path):
    """The reap must run BEFORE anything prints.

    A wedged run often leaves stdout as a pipe with no reader — an operator's
    `zeabur service exec` that dropped, a closed terminal. Once the pipe buffer
    fills, print() blocks forever, and on 2026-09-22 that stranded the very
    process the hard exit exists to reap: the run was already recorded failed,
    and the main thread sat in process_bootstrap.write() on the "Triggered job:"
    line, holding 278 MB for 22 minutes.

    Here the child fills the pipe and then prints, with nobody draining it. It
    must still exit.
    """
    script = tmp_path / "blocked_stdout.py"
    script.write_text(
        "import os, sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        # Fill the OS pipe buffer so any FURTHER write blocks. The fill itself
        # must not block, hence non-blocking until EAGAIN, then back to
        # blocking so fd 1 behaves exactly like a stranded terminal.
        "os.set_blocking(1, False)\n"
        "try:\n"
        "    while True:\n"
        "        os.write(1, b'x' * 65536)\n"
        "except BlockingIOError:\n"
        "    pass\n"
        "os.set_blocking(1, True)\n"
        "from cron.fork_ext import cli\n"
        "import cron.scheduler as sched\n"
        "sched._note_abandoned_agent_thread()\n"
        "cli.exit_hard_if_threads_abandoned(0)\n"
        "os._exit(99)\n",  # only reached if the helper failed to exit
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise AssertionError(
            "the process hung on a full stdout pipe — the reap ran too late, "
            "or its own print blocked"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
    assert proc.returncode == 0, (
        f"expected the helper's os._exit(0), got {proc.returncode} "
        f"({'fell through to the sentinel' if proc.returncode == 99 else 'unexpected'})"
    )
