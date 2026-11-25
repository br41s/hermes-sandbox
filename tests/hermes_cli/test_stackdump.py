"""Tests for the SIGUSR1 stack dumper.

The value of this thing is entirely in the moment nobody is watching: a cron
agent wedges, the watchdog kills it 20 minutes later, and the only question
that matters is what the threads were doing. So the tests exercise the real
path — a live process, a real signal, a real file — rather than asserting that
`faulthandler.register` was called with the right arguments.

The case that motivated it: four consecutive Infographic Engineer runs wedged
holding zero open sockets while the watchdog reported "waiting for
non-streaming API response". py-spy could not attach (ptrace_scope=2, no
CAP_SYS_PTRACE in the pod), so there was no way to see the stack at all.
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Bound once via getattr: a bare `signal.SIGUSR1` raises AttributeError at
# import time on Windows, which scripts/check-windows-footguns.py blocks.
SIGUSR1 = getattr(signal, "SIGUSR1", None)

pytestmark = pytest.mark.skipif(
    SIGUSR1 is None,
    reason="SIGUSR1 does not exist on Windows; the dumper is a no-op there",
)


def test_dump_path_follows_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.stackdump import dump_path

    p = dump_path(pid=4242)
    assert p == tmp_path / "logs" / "stackdump-4242.log"


def test_install_returns_a_path_and_creates_the_directory(tmp_path):
    from hermes_cli.stackdump import install_stack_dumper

    target = tmp_path / "nested" / "logs"
    path = install_stack_dumper(log_dir=target)
    assert path is not None
    assert path.parent.is_dir()
    assert "armed" in path.read_text(encoding="utf-8")


def test_install_never_raises_on_an_unwritable_directory(tmp_path):
    """A diagnostic that breaks startup is worse than no diagnostic."""
    from hermes_cli.stackdump import install_stack_dumper

    blocker = tmp_path / "logs"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    assert install_stack_dumper(log_dir=blocker / "deeper") is None


def test_a_wedged_process_dumps_its_threads_on_sigusr1(tmp_path):
    """The whole point: signal a stuck process, read its stack, it keeps running."""
    script = tmp_path / "wedged.py"
    script.write_text(
        "import sys, threading, time\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "from hermes_cli.stackdump import install_stack_dumper\n"
        f"install_stack_dumper(log_dir={str(tmp_path)!r})\n"
        "def worker_that_never_finishes():\n"
        "    ev = threading.Event()\n"
        "    ev.wait()\n"
        "t = threading.Thread(target=worker_that_never_finishes, name='wedged-worker')\n"
        "t.daemon = True\n"
        "t.start()\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "ready"
        dump = tmp_path / f"stackdump-{proc.pid}.log"

        os.kill(proc.pid, SIGUSR1)

        deadline = time.time() + 10
        body = ""
        while time.time() < deadline:
            if dump.exists():
                body = dump.read_text(encoding="utf-8", errors="replace")
                if "worker_that_never_finishes" in body:
                    break
            time.sleep(0.1)

        assert "worker_that_never_finishes" in body, (
            f"the wedged thread's frame is missing from the dump:\n{body}"
        )
        assert "Thread" in body, f"no thread header in the dump:\n{body}"
        # The process must survive being asked what it is doing.
        assert proc.poll() is None, "the dump killed the process"

        # Re-armed: a second signal produces a second dump, so an operator can
        # sample twice and tell "stuck" from "slow".
        os.kill(proc.pid, SIGUSR1)
        deadline = time.time() + 10
        while time.time() < deadline:
            again = dump.read_text(encoding="utf-8", errors="replace")
            if again.count("worker_that_never_finishes") >= 2:
                break
            time.sleep(0.1)
        assert again.count("worker_that_never_finishes") >= 2, (
            "the handler did not re-arm; only one dump was produced"
        )
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_dump_now_writes_without_a_signal(tmp_path):
    """The cron watchdog's path — it knows the run is wedged before it gives up."""
    from hermes_cli import stackdump

    out = tmp_path / "direct.log"
    with open(out, "w", encoding="utf-8") as fh:
        stackdump.dump_now(file=fh)
    body = out.read_text(encoding="utf-8")
    assert "test_dump_now_writes_without_a_signal" in body, body


def _armed_then(tmp_path, body: str, before: str = "") -> list[Path]:
    """Arm the dumper in a child that then runs *body*; the dump files it leaves behind."""
    logs = tmp_path / "logs"
    script = (f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r})\n"
              "import os, signal, time\nfrom pathlib import Path\n"
              f"logs = Path({str(logs)!r}); logs.mkdir()\n{before}\n"
              "from hermes_cli.stackdump import install_stack_dumper\n"
              f"install_stack_dumper(log_dir=logs)\n{body}\n")
    subprocess.run([sys.executable, "-c", script], check=True, timeout=60)
    return sorted(logs.iterdir())


def test_a_process_that_never_dumped_leaves_no_file(tmp_path):
    """Every CLI command arms the dumper: without this each one leaves a file in logs/ for good."""
    assert _armed_then(tmp_path, "") == []


def test_a_dump_outlives_the_process_that_wrote_it(tmp_path):
    left = _armed_then(tmp_path, "os.kill(os.getpid(), signal.SIGUSR1); time.sleep(0.2)")
    assert len(left) == 1 and "most recent call first" in left[0].read_text(encoding="utf-8")


def test_an_earlier_dump_under_the_same_pid_is_kept(tmp_path):
    left = _armed_then(tmp_path, "", before="(logs / f'stackdump-{os.getpid()}.log').write_text('old dump')")
    assert len(left) == 1 and left[0].read_text(encoding="utf-8").startswith("old dump")


def test_a_forked_child_does_not_discard_its_parents_file(tmp_path):
    body = ("pid = os.fork()\nif pid == 0: sys.exit(0)\nos.waitpid(pid, 0)\n"
            "assert (logs / f'stackdump-{os.getpid()}.log').exists()")
    assert _armed_then(tmp_path, body) == []
