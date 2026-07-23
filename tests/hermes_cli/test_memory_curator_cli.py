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


def _stub_engine(mc, monkeypatch, home, called):
    def fake_run(*, on_digest=None, force=False):
        called["force"] = force
        if on_digest:
            on_digest("scanned 3 session(s) — new lessons proposed")
        return {"summary": "ok", "digest_path": str(home / "d.md"), "sessions": 3}
    monkeypatch.setattr(mc, "run_memory_digest", fake_run)


def test_run_force_invokes_engine(cli_env, monkeypatch, capsys):
    called = {}
    _stub_engine(cli_env["mc"], monkeypatch, cli_env["home"], called)
    args = cli_env["parser"].parse_args(["run", "--force"])
    assert args.func(args) == 0
    assert called["force"] is True
    assert "new lessons proposed" in capsys.readouterr().out


def test_run_enabled_without_force_still_runs_now(cli_env, monkeypatch, capsys):
    """A manual `run` (enabled, no --force) runs now, bypassing the interval —
    the engine is invoked with force=True. Guards the sibling `hermes curator
    run` contract against a regression to interval-gated behavior."""
    mc = cli_env["mc"]
    monkeypatch.setattr(mc, "_load_config", lambda: {"enabled": True})
    called = {}
    _stub_engine(mc, monkeypatch, cli_env["home"], called)
    args = cli_env["parser"].parse_args(["run"])
    assert args.func(args) == 0
    assert called["force"] is True  # runs now, not gated by the schedule


def test_run_disabled_with_force_invokes_engine(cli_env, monkeypatch):
    """--force lets you preview even when disabled."""
    mc = cli_env["mc"]  # fixture already stubs _load_config → enabled False
    called = {}
    _stub_engine(mc, monkeypatch, cli_env["home"], called)
    args = cli_env["parser"].parse_args(["run", "--force"])
    assert args.func(args) == 0
    assert called["force"] is True


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


def test_apply_without_ids_or_all_returns_1(cli_env, capsys):
    args = cli_env["parser"].parse_args(["apply"])
    assert args.func(args) == 1
    assert "proposal ids" in capsys.readouterr().out


def test_apply_forwards_ids(cli_env, monkeypatch, capsys):
    mc = cli_env["mc"]
    seen = {}

    def fake_apply(ids=None, *, apply_all=False):
        seen["ids"], seen["all"] = ids, apply_all
        return {"applied": ["p1"], "skipped": [], "errors": []}

    monkeypatch.setattr(mc, "apply_proposals", fake_apply)
    args = cli_env["parser"].parse_args(["apply", "p1", "p3"])
    assert args.func(args) == 0
    assert seen == {"ids": ["p1", "p3"], "all": False}
    assert "wrote to memory: p1" in capsys.readouterr().out


def test_apply_all_flag(cli_env, monkeypatch):
    mc = cli_env["mc"]
    seen = {}
    monkeypatch.setattr(
        mc, "apply_proposals",
        lambda ids=None, *, apply_all=False: (
            seen.update(all=apply_all, ids=ids),
            {"applied": ["p1"], "skipped": [], "errors": []})[1],
    )
    args = cli_env["parser"].parse_args(["apply", "--all"])
    assert args.func(args) == 0
    assert seen["all"] is True and seen["ids"] is None


def test_apply_reports_errors_returns_1(cli_env, monkeypatch, capsys):
    mc = cli_env["mc"]
    monkeypatch.setattr(
        mc, "apply_proposals",
        lambda ids=None, *, apply_all=False: {
            "applied": [], "skipped": [], "errors": ["p1: over cap"]},
    )
    args = cli_env["parser"].parse_args(["apply", "p1"])
    assert args.func(args) == 1
    assert "over cap" in capsys.readouterr().out


def test_revert_success_and_failure(cli_env, monkeypatch, capsys):
    mc = cli_env["mc"]
    monkeypatch.setattr(mc, "revert_last", lambda: {"reverted": "p1", "target": "user"})
    args = cli_env["parser"].parse_args(["revert"])
    assert args.func(args) == 0
    assert "reverted p1" in capsys.readouterr().out

    monkeypatch.setattr(mc, "revert_last", lambda: {"reverted": None, "error": "empty"})
    args = cli_env["parser"].parse_args(["revert"])
    assert args.func(args) == 1
