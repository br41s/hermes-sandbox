"""Tests for hermes_cli/memory_curator.py — the read-only CLI shell.

Argparse wiring + dispatch only; the digest engine is exercised in
tests/agent/test_memory_curator.py.
"""

from __future__ import annotations

import argparse
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "memory-curator").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    import agent.memory_curator as mc
    importlib.reload(mc)
    monkeypatch.setattr(mc, "_load_config", lambda: {"enabled": False})

    import hermes_cli.memory_curator as cli
    importlib.reload(cli)

    parser = argparse.ArgumentParser(prog="hermes memory-curator")
    cli.register_cli(parser)
    return {"home": home, "mc": mc, "cli": cli, "parser": parser}


def test_status_runs_clean(cli_env, capsys):
    args = cli_env["parser"].parse_args(["status"])
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert "enabled:" in out and "interval:" in out


def test_show_without_digest_returns_1(cli_env, capsys):
    args = cli_env["parser"].parse_args(["show"])
    assert args.func(args) == 1
    assert "No digest yet" in capsys.readouterr().out


def test_run_disabled_without_force_returns_1(cli_env, capsys):
    args = cli_env["parser"].parse_args(["run"])
    assert args.func(args) == 1
    assert "disabled" in capsys.readouterr().out


def test_run_force_invokes_engine(cli_env, monkeypatch, capsys):
    mc = cli_env["mc"]
    called = {}

    def fake_run(*, on_digest=None, force=False):
        called["force"] = force
        if on_digest:
            on_digest("scanned 3 session(s) — new lessons proposed")
        return {"summary": "ok", "digest_path": str(cli_env["home"] / "d.md"),
                "sessions": 3}

    monkeypatch.setattr(mc, "run_memory_digest", fake_run)
    args = cli_env["parser"].parse_args(["run", "--force"])
    assert args.func(args) == 0
    assert called["force"] is True
    assert "new lessons proposed" in capsys.readouterr().out


def test_show_prints_latest(cli_env, capsys):
    (cli_env["home"] / "memory-curator" / "latest.md").write_text(
        "# Memory digest\n\nhello", encoding="utf-8"
    )
    args = cli_env["parser"].parse_args(["show"])
    assert args.func(args) == 0
    assert "hello" in capsys.readouterr().out


def test_bare_command_prints_help(cli_env):
    # No subcommand → default func prints help and returns 0.
    args = cli_env["parser"].parse_args([])
    assert args.func(args) == 0
