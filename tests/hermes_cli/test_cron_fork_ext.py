"""The fork's ``hermes cron`` extensions, reached through upstream's call sites.

The extensions live in ``cron/fork_ext/cli.py`` and
``hermes_cli/subcommands/cron_fork_ext.py``; ``hermes_cli/cron.py`` and
``hermes_cli/subcommands/cron.py`` only call into them. These tests drive the
upstream entry points, so a call site dropped during an upstream merge fails
here rather than silently losing a flag or the wedged-run reap.
"""

import argparse
from argparse import Namespace
from types import SimpleNamespace

import pytest

from cron.fork_ext import cli as fork_cli
from cron.fork_ext.prompt_sync import prompt_sha
from hermes_cli import cron as cron_cli
from hermes_cli.subcommands.cron import build_cron_parser


def _parser():
    parser = argparse.ArgumentParser(prog="hermes")
    build_cron_parser(parser.add_subparsers(dest="command"), cmd_cron=lambda a: None)
    return parser


# ------------------------------------------------------------------ parser


def test_fork_flags_parse_through_upstream_parser():
    p = _parser()
    ns = p.parse_args(["cron", "create", "30m", "--profile", "grow-shop"])
    assert ns.profile == "grow-shop"

    ns = p.parse_args(["cron", "edit", "j", "--profile", "", "--prompt-source", "a/b.prompt",
                       "--no-progress-ping"])
    assert (ns.profile, ns.prompt_source, ns.progress_ping) == ("", "a/b.prompt", False)
    assert p.parse_args(["cron", "edit", "j", "--progress-ping"]).progress_ping is True
    assert p.parse_args(["cron", "edit", "j"]).progress_ping is None
    with pytest.raises(SystemExit):
        p.parse_args(["cron", "edit", "j", "--progress-ping", "--no-progress-ping"])

    for alias in ("sync-prompt", "sync_prompt"):
        ns = p.parse_args(["cron", alias, "j", "--prompt-source", "x.prompt"])
        assert (ns.cron_command, ns.job_id, ns.prompt_source) == (alias, "j", "x.prompt")


def test_sync_prompt_keeps_its_place_between_remove_and_status():
    help_text = _parser()._subparsers._group_actions[0].choices["cron"].format_help()
    assert help_text.index("remove (rm, delete)") < help_text.index("sync-prompt (sync_prompt)")
    assert help_text.index("sync-prompt (sync_prompt)") < help_text.index("status ")


# ---------------------------------------------------------------- dispatch


def test_sync_prompt_dispatches_to_the_tool_action(monkeypatch, capsys):
    calls = []

    def fake_api(**kwargs):
        calls.append(kwargs)
        return {"success": True, "changed": True, "message": "synced it"}

    monkeypatch.setattr(cron_cli, "_cron_api", fake_api)
    rc = cron_cli.cron_command(Namespace(cron_command="sync_prompt", job_id="j", prompt_source=None))
    assert rc == 0
    assert calls == [{"action": "sync_prompt", "job_id": "j", "prompt_source": None}]
    assert "Synced: synced it" in capsys.readouterr().out


def test_sync_prompt_failure_returns_1(monkeypatch, capsys):
    monkeypatch.setattr(cron_cli, "_cron_api", lambda **kw: {"success": False, "error": "nope"})
    assert cron_cli.cron_command(Namespace(cron_command="sync-prompt", job_id="j")) == 1
    assert "Failed to sync prompt: nope" in capsys.readouterr().out


def test_run_reaps_before_printing_and_again_after(monkeypatch, capsys):
    """The reap must come BEFORE the first print (a blocked stdout would strand it)."""
    events = []
    monkeypatch.setattr(cron_cli, "_cron_api", lambda **kw: {"success": True, "job": {"name": "n"}})
    monkeypatch.setattr(
        fork_cli, "exit_hard_if_threads_abandoned",
        lambda rc: events.append(("reap", rc, capsys.readouterr().out)) or rc,
    )
    assert cron_cli.cron_command(Namespace(cron_command="run", job_id="j")) == 0
    assert events[0] == ("reap", 0, ""), "reaped after output was already written"
    assert events[1][:2] == ("reap", 0)
    assert "Triggered job: n (j)" in events[1][2]


def test_non_run_actions_never_reap(monkeypatch):
    monkeypatch.setattr(cron_cli, "_cron_api", lambda **kw: {"success": True, "job": {}})
    monkeypatch.setattr(fork_cli, "exit_hard_if_threads_abandoned",
                        lambda rc: pytest.fail("reaped on a non-run action"))
    assert cron_cli.cron_command(Namespace(cron_command="pause", job_id="j")) == 0


# ------------------------------------------------------------ create / edit


def test_edit_passes_fork_fields_and_prints_them_in_order(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr("cron.jobs.resolve_job_ref", lambda ref: {"id": ref})

    def fake_api(**kwargs):
        seen.update(kwargs)
        return {"success": True, "job": {
            "job_id": "j", "name": "n", "schedule": "s", "no_agent": True,
            "progress_ping": False, "workdir": "/w", "profile": "p", "prompt_source": "a.prompt",
        }}

    monkeypatch.setattr(cron_cli, "_cron_api", fake_api)
    args = SimpleNamespace(job_id="j", profile="p", progress_ping=False, prompt_source="a.prompt")
    assert cron_cli.cron_edit(args) == 0
    assert (seen["profile"], seen["progress_ping"], seen["prompt_source"]) == ("p", False, "a.prompt")
    out = capsys.readouterr().out.splitlines()
    tail = out[out.index("  Mode: no-agent (script stdout delivered directly)"):]
    assert tail == [
        "  Mode: no-agent (script stdout delivered directly)",
        "  Kickoff ping: off (silent on start)",
        "  Workdir: /w",
        "  Profile: p",
        "  Prompt source: a.prompt",
    ]


def test_create_passes_profile_and_prints_it_after_workdir(monkeypatch, capsys):
    seen = {}

    def fake_api(**kwargs):
        seen.update(kwargs)
        return {"success": True, "job_id": "j", "name": "n", "schedule": "s",
                "next_run_at": "t", "job": {"workdir": "/w", "profile": "p"}}

    monkeypatch.setattr(cron_cli, "_cron_api", fake_api)
    monkeypatch.setattr(cron_cli, "_warn_if_gateway_not_running", lambda: None)
    args = SimpleNamespace(schedule="30m", prompt="x", profile="p")
    assert cron_cli.cron_create(args) == 0
    assert seen["profile"] == "p"
    assert seen["progress_ping"] is None and seen["prompt_source"] is None
    out = capsys.readouterr().out
    assert out.index("  Workdir: /w") < out.index("  Profile: p") < out.index("  Next run: t")


# -------------------------------------------------------------------- list


def _list_output(monkeypatch, capsys, job):
    monkeypatch.setattr("cron.jobs.list_jobs", lambda include_disabled=False: [job])
    monkeypatch.setattr(cron_cli, "_warn_if_gateway_not_running", lambda: None)
    cron_cli.cron_list()
    return capsys.readouterr().out


def test_list_shows_profile_and_normal_last_run(monkeypatch, capsys):
    out = _list_output(monkeypatch, capsys, {
        "id": "j", "profile": "p", "workdir": "/w", "last_status": "ok", "last_run_at": "T1",
    })
    assert out.index("Workdir:   /w") < out.index("Profile:   p") < out.index("Last run:  T1")
    assert "Interrupted" not in out


def test_list_interrupted_run_prints_both_clocks_once(monkeypatch, capsys):
    out = _list_output(monkeypatch, capsys, {
        "id": "j", "last_status": "interrupted", "last_run_at": "T1",
        "last_interrupted_at": "T2", "last_error": "should not show",
    })
    assert out.count("Last run:") == 1
    assert "Last run:  T1  (completed)" in out
    assert "T2 — killed before it finished; not retried" in out
    assert "inspect: hermes cron runs j" in out
    assert "should not show" not in out


# ------------------------------------------------------------ prompt sync


def test_script_and_tool_share_one_baseline_hash():
    from scripts import sync_prompt_drift

    assert sync_prompt_drift._prompt_sha is prompt_sha
