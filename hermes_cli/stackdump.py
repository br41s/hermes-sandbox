"""Dump every thread's stack on SIGUSR1, so a wedged agent can be diagnosed.

WHY THIS EXISTS

On 2026-09-22 the Infographic Engineer wedged four times in a row. The cron
inactivity watchdog killed each run at 1200s and reported

    TimeoutError: ... idle for 1201s ... last activity: waiting for
    non-streaming API response

which is wrong, and wrong in the direction that wastes a day. The agent was not
waiting on the API: ``agent.log`` showed the request completing normally
(``OpenAI client closed (request_complete)``, 313s), and the stalled process
held **zero open sockets** with ~10 live threads at 0.2% CPU. It had the
response and then blocked on something internal.

There was no way to find out what. ``py-spy`` cannot attach in the Zeabur
container: ``/proc/sys/kernel/yama/ptrace_scope`` is 2 and the pod's ``CapEff``
does not include ``CAP_SYS_PTRACE``, so an external profiler is refused even as
root. An in-process dumper is the only thing that works here, and it has to be
installed BEFORE the process wedges — which is the whole point of registering it
unconditionally at startup rather than reaching for a tool afterwards.

HOW TO USE IT

    kill -USR1 <pid>          # the process keeps running
    cat $HERMES_HOME/logs/stackdump-<pid>.log

The handler is re-armed after every signal, so you can sample a few seconds
apart and see whether the process is moving or genuinely stuck.

COST

A registered signal handler. No thread, no polling, no measurable overhead —
which is why this is always on instead of behind a flag nobody sets before the
incident they needed it for.
"""
from __future__ import annotations

import faulthandler
import os
import signal
import sys
from pathlib import Path
from typing import Optional

# Held for the process lifetime: faulthandler writes to the file descriptor
# whenever the signal arrives, so closing this would turn the dump into a
# crash at exactly the moment we need it.
_DUMP_FILE = None


def dump_path(log_dir: Optional[Path] = None, pid: Optional[int] = None) -> Path:
    """Where this process's stack dumps land."""
    pid = os.getpid() if pid is None else pid
    if log_dir is None:
        home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        log_dir = Path(home) / "logs"
    return Path(log_dir) / f"stackdump-{pid}.log"


def install_stack_dumper(log_dir: Optional[Path] = None) -> Optional[Path]:
    """Register SIGUSR1 -> dump all thread stacks. Returns the path, or None.

    Never raises. A diagnostic that can break startup is worse than no
    diagnostic, and this runs on every CLI invocation.

    Returns None where it cannot work: Windows (no SIGUSR1), or an unwritable
    log directory.
    """
    global _DUMP_FILE

    # SIGUSR1 does not exist on Windows; `signal.SIGUSR1` would raise
    # AttributeError at import. scripts/check-windows-footguns.py enforces this
    # getattr form.
    sigusr1 = getattr(signal, "SIGUSR1", None)
    if sigusr1 is None:
        return None

    try:
        path = dump_path(log_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Line-buffered append: a dump must survive the process being killed
        # immediately afterwards, which is the normal case here — the watchdog
        # is usually about to fire.
        handle = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        return None

    try:
        handle.write(f"\n=== stack dumper armed: pid {os.getpid()} ===\n")
        faulthandler.register(sigusr1, file=handle, all_threads=True, chain=False)
        # A hard crash should land in the same file rather than vanishing into
        # a container's stderr.
        faulthandler.enable(file=handle, all_threads=True)
    except (OSError, RuntimeError, ValueError):
        try:
            handle.close()
        except OSError:
            pass
        return None

    _DUMP_FILE = handle
    return path


def dump_now(file=None) -> None:
    """Dump this process's threads immediately, without a signal.

    For code that has detected a stall itself — the cron watchdog is the
    obvious caller, since it knows a run is wedged before it kills it.
    """
    try:
        faulthandler.dump_traceback(file=file or _DUMP_FILE or sys.stderr,
                                    all_threads=True)
    except (OSError, RuntimeError, ValueError):
        pass
