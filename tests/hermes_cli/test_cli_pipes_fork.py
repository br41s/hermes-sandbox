"""Fork: a hermes subcommand whose reader goes away (``| head``) exits quietly, like a Unix tool."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hermes_cli.fork_ext.cli_pipes import BROKEN_PIPE_EXIT, quiet_broken_pipe

_REPO = Path(__file__).resolve().parents[2]


def test_real_closed_pipe_leaves_no_traceback():
    """The interpreter-shutdown flush is the part that still prints unless stdout is redirected."""
    writer = textwrap.dedent("""
        from hermes_cli.fork_ext.cli_pipes import quiet_broken_pipe
        with quiet_broken_pipe():
            for i in range(200000):
                print(f"line {i}")
    """)
    proc = subprocess.Popen([sys.executable, "-c", writer], cwd=_REPO, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    assert proc.stdout.readline() == b"line 0\n"
    proc.stdout.close()  # the reader leaves, as `head -1` does
    _, err = proc.communicate(timeout=30)

    assert proc.returncode == BROKEN_PIPE_EXIT
    assert err == b"", err.decode(errors="replace")


def test_other_errors_still_raise():
    with pytest.raises(ValueError):
        with quiet_broken_pipe():
            raise ValueError("not a pipe problem")


def test_hermes_subcommand_dispatch_goes_through_it(monkeypatch, tmp_path):
    import hermes_cli.cron as cron_cli
    import hermes_cli.main as main_mod

    def _reader_gone(*_a, **_k):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cron_cli, "cron_runs", _reader_gone)
    monkeypatch.setattr(sys, "argv", ["hermes", "cron", "runs"])
    # dup2 onto the real stdout fd would detach pytest's capture; the redirect is covered above.
    monkeypatch.setattr("hermes_cli.fork_ext.cli_pipes.os.dup2", lambda *_a: None)

    with pytest.raises(SystemExit) as exc:
        main_mod.main()

    assert exc.value.code == BROKEN_PIPE_EXIT


def test_the_devnull_descriptor_is_closed_after_the_redirect(monkeypatch):
    import hermes_cli.fork_ext.cli_pipes as cli_pipes

    opened, closed = [], []
    real_open = cli_pipes.os.open
    monkeypatch.setattr(cli_pipes.os, "open", lambda *a: opened.append(real_open(*a)) or opened[-1])
    monkeypatch.setattr(cli_pipes.os, "dup2", lambda *_a: None)  # keep pytest's stdout attached
    real_close = cli_pipes.os.close
    monkeypatch.setattr(cli_pipes.os, "close", lambda fd: closed.append(fd) or real_close(fd))

    with pytest.raises(SystemExit):
        with quiet_broken_pipe():
            raise BrokenPipeError(32, "Broken pipe")

    assert opened and closed == opened
