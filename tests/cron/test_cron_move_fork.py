"""Fork: ``hermes cron move`` (stage 3 step 0f, ``cron/fork_ext/move.py``).

Real stores in the per-test HERMES_HOME. The invariant under test is the one the
plan states for every instant of a move: at most one runnable record per id
across all stores, with the id, schedule phase, executions, output and notepad
carried over, and ``remove_job`` never called.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

import cron.jobs as cron_jobs
from cron.fork_ext import move as mv


@pytest.fixture
def stores(monkeypatch):
    from hermes_cli.profiles import get_profile_dir

    default = mv._default_home()
    shop = get_profile_dir("grow-shop")
    shop.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cron_jobs, "remove_job",
                        lambda *a, **k: pytest.fail("a move must never call remove_job"))
    return default, shop


def _make(home, name="shop-sweeper", **fields):
    with mv._in_home(home):
        job = cron_jobs.create_job(prompt="sweep", schedule="every 1h", name=name)
    if fields:
        _edit(home, job["id"], **fields)
    return job["id"]


def _edit(home, jid, **fields):
    def fn(jobs):
        for j in jobs:
            if j["id"] == jid:
                j.update(fields)
    mv._rewrite(home, fn)


def _get(home, jid):
    return next((j for j in mv._load(home) if j["id"] == jid), None)


def _future(minutes):
    return (cron_jobs._hermes_now() + timedelta(minutes=minutes)).isoformat()


def _runnable(home, jid):
    job = _get(home, jid)
    return job is not None and cron_jobs.is_job_runnable(job)


def _seed_state(home, jid):
    from cron import executions, notepad

    with mv._in_home(home):
        row = executions.create_execution(jid, source="tick")
        executions.mark_execution_running(row["id"])
        executions.finish_execution(row["id"], success=True)
        notepad.set_note(jid, "cursor", "42")
        out = cron_jobs.get_cron_output_dir() / jid
    out.mkdir(parents=True, exist_ok=True)
    (out / "last.md").write_text("output", encoding="utf-8")
    return row["id"]


def test_dry_run_changes_nothing(stores, capsys):
    default, shop = stores
    jid = _make(default, profile="grow-shop", next_run_at=_future(60))
    before = _get(default, jid)
    assert mv.move([jid], to_profile="grow-shop") == 0
    assert "dry run: nothing changed" in capsys.readouterr().out
    assert _get(default, jid) == before
    assert not (shop / "cron" / "jobs.json").exists()


def test_apply_moves_the_record_and_its_state(stores):
    default, shop = stores
    nxt = _future(60)
    jid = _make(default, profile="grow-shop", next_run_at=nxt, last_run_at="2026-09-30T10:00:00+00:00",
                last_status="ok", prompt_source="gap-hunter/x.prompt")
    exec_id = _seed_state(default, jid)
    original = _get(default, jid)

    assert mv.move([jid], to_profile="grow-shop", apply=True) == 0

    assert _get(default, jid) is None
    moved = _get(shop, jid)
    assert "profile" not in moved and mv.MOVE_KEY not in moved
    for key in ("id", "next_run_at", "last_run_at", "last_status", "repeat", "prompt_source",
                "schedule", "enabled", "state"):
        assert moved.get(key) == original.get(key), key
    assert _runnable(shop, jid)

    from cron import executions, notepad
    with mv._in_home(shop):
        assert executions.get_execution(exec_id)["status"] == "completed"
        assert notepad.get_note(jid, "cursor") == "42"
        assert (cron_jobs.get_cron_output_dir() / jid / "last.md").read_text() == "output"
    # Copied, not moved: the source home keeps its copy as the rollback.
    with mv._in_home(default):
        assert executions.get_execution(exec_id) is not None


def test_steps_run_in_the_planned_order(stores, monkeypatch):
    default, shop = stores
    jid = _make(default, profile="grow-shop", next_run_at=_future(60))
    _seed_state(default, jid)
    events = []
    real_rewrite, real_insert = mv._rewrite, mv._insert

    def rewrite(home, fn):
        before = {j["id"]: j for j in mv._load(home)}
        real_rewrite(home, fn)
        after = {j["id"]: j for j in mv._load(home)}
        side = "source" if home == default else "target"
        if jid in before and jid not in after:
            events.append(f"{side}:delete")
        elif jid not in before and jid in after:
            events.append(f"{side}:write")
        elif after.get(jid, {}).get("paused_reason") == "moving to grow-shop":
            events.append(f"{side}:pause")

    def insert(module, table, rows, home):
        events.append(f"copy:{table}")
        return real_insert(module, table, rows, home)

    monkeypatch.setattr(mv, "_rewrite", rewrite)
    monkeypatch.setattr(mv, "_insert", insert)
    assert mv.move([jid], to_profile="grow-shop", apply=True) == 0
    assert events == ["source:pause", "target:write", "copy:executions",
                      "copy:cron_notepad", "source:delete"]


def test_crash_between_pause_and_target_write_is_finished_by_a_rerun(stores, monkeypatch):
    default, shop = stores
    jid = _make(default, profile="grow-shop", next_run_at=_future(60))
    real = mv._rewrite

    def crash_on_target(home, fn):
        if home == shop:
            raise RuntimeError("container restarted")
        real(home, fn)

    monkeypatch.setattr(mv, "_rewrite", crash_on_target)
    with pytest.raises(RuntimeError):
        mv.move([jid], to_profile="grow-shop", apply=True)
    # Source paused, visibly, target absent: no runnable record anywhere.
    assert _get(default, jid)["paused_reason"] == "moving to grow-shop"
    assert not _runnable(default, jid) and _get(shop, jid) is None

    monkeypatch.setattr(mv, "_rewrite", real)
    assert mv.move([jid], to_profile="grow-shop", apply=True) == 0
    assert _get(default, jid) is None
    # The target carries the PRE-move state, not the move's pause.
    assert _runnable(shop, jid) and not _get(shop, jid).get("paused_reason")


def test_crash_between_target_write_and_source_delete_never_leaves_two_runnable(stores, monkeypatch):
    default, shop = stores
    jid = _make(default, profile="grow-shop", next_run_at=_future(60))
    real = mv._rewrite
    calls = []

    def crash_on_delete(home, fn):
        calls.append(home)
        if len(calls) == 3:
            raise RuntimeError("container restarted")
        real(home, fn)

    monkeypatch.setattr(mv, "_rewrite", crash_on_delete)
    with pytest.raises(RuntimeError):
        mv.move([jid], to_profile="grow-shop", apply=True)
    assert _get(default, jid) is not None and _get(shop, jid) is not None
    assert [_runnable(default, jid), _runnable(shop, jid)] == [False, True]

    monkeypatch.setattr(mv, "_rewrite", real)
    assert mv.move([jid], to_profile="grow-shop", apply=True) == 0
    assert _get(default, jid) is None and _runnable(shop, jid)


def test_a_paused_job_stays_paused_after_the_move(stores):
    default, shop = stores
    jid = _make(default, profile="grow-shop", next_run_at=_future(60))
    with mv._in_home(default):
        cron_jobs.pause_job(jid, reason="owner said so")
    assert mv.move([jid], to_profile="grow-shop", apply=True) == 0
    moved = _get(shop, jid)
    assert not cron_jobs.is_job_runnable(moved) and moved["paused_reason"] == "owner said so"


def test_moving_back_restores_the_profile_field(stores):
    default, shop = stores
    jid = _make(default, profile="grow-shop", next_run_at=_future(60))
    assert mv.move([jid], to_profile="grow-shop", apply=True) == 0
    assert mv.move([jid], from_profile="grow-shop", to_default=True, apply=True) == 0
    assert _get(shop, jid) is None
    assert _get(default, jid)["profile"] == "grow-shop" and _runnable(default, jid)


def test_an_already_moved_job_is_a_no_op(stores, capsys):
    default, shop = stores
    jid = _make(default, profile="grow-shop", next_run_at=_future(60))
    assert mv.move([jid], to_profile="grow-shop", apply=True) == 0
    assert mv.move([jid], to_profile="grow-shop", apply=True) == 0
    assert "already in the grow-shop store" in capsys.readouterr().out


# ── refusals ─────────────────────────────────────────────────────────────────


def _refused(capsys, *args, **kwargs):
    assert mv.move(*args, **kwargs) == 1
    return capsys.readouterr().out


def test_refuses_a_job_whose_next_run_is_too_close(stores, capsys):
    default, _ = stores
    jid = _make(default, profile="grow-shop", next_run_at=_future(5))
    assert "under 10 minutes away" in _refused(capsys, [jid], to_profile="grow-shop", apply=True)
    assert cron_jobs.is_job_runnable(_get(default, jid))


def test_refuses_a_job_with_a_live_fire_claim(stores, capsys):
    default, _ = stores
    jid = _make(default, profile="grow-shop", next_run_at=_future(60),
                fire_claim={"at": cron_jobs._hermes_now().isoformat(), "by": "elsewhere:1"})
    assert "live fire_claim" in _refused(capsys, [jid], to_profile="grow-shop", apply=True)


def test_refuses_a_job_with_a_running_execution(stores, capsys):
    from cron import executions

    default, _ = stores
    jid = _make(default, profile="grow-shop", next_run_at=_future(60))
    with mv._in_home(default):
        executions.mark_execution_running(executions.create_execution(jid, source="tick")["id"])
    assert "claimed/running execution" in _refused(capsys, [jid], to_profile="grow-shop", apply=True)


def test_refuses_when_the_target_already_has_the_id(stores, capsys):
    default, shop = stores
    jid = _make(default, profile="grow-shop", next_run_at=_future(60))
    clash = dict(_get(default, jid))
    clash.pop("profile")
    mv._rewrite(shop, lambda jobs: jobs.append(clash))
    assert "already has this id" in _refused(capsys, [jid], to_profile="grow-shop", apply=True)
    assert _runnable(default, jid)


def test_refuses_to_split_a_context_from_edge(stores, capsys):
    default, shop = stores
    reader = _make(default, name="reader", profile="grow-shop", next_run_at=_future(60))
    source = _make(default, name="source", profile="grow-shop", next_run_at=_future(60))
    _edit(default, reader, context_from=[source])
    assert "not moving with it" in _refused(capsys, [reader], to_profile="grow-shop", apply=True)
    assert "move them together" in _refused(capsys, [source], to_profile="grow-shop", apply=True)
    assert mv.move([reader, source], to_profile="grow-shop", apply=True) == 0
    assert _get(shop, reader) and _get(shop, source)


def test_refuses_a_webhook_triggered_job_until_the_route_is_disabled(stores, capsys):
    default, shop = stores
    jid = _make(default, profile="grow-shop", next_run_at=_future(60))
    (default / "config.yaml").write_text(
        "platforms:\n  webhook:\n    extra:\n      routes:\n"
        f"        pr:\n          trigger_cron_job_id: {jid}\n", encoding="utf-8")
    assert "pause does not stop" in _refused(capsys, [jid], to_profile="grow-shop", apply=True)
    assert mv.move([jid], to_profile="grow-shop", apply=True, webhook_route_disabled=True) == 0


def test_refuses_a_job_that_belongs_to_another_profile(stores, capsys):
    default, _ = stores
    jid = _make(default, profile="auditor", next_run_at=_future(60))
    assert "its profile is auditor" in _refused(capsys, [jid], to_profile="grow-shop", apply=True)


@pytest.mark.parametrize("kwargs, message", [
    ({"to_profile": "nope"}, "does not exist"),
    ({"to_default": True}, "needs --from-profile"),
    ({}, "exactly one of"),
    ({"to_profile": "grow-shop", "to_default": True}, "exactly one of"),
])
def test_refuses_bad_arguments(stores, capsys, kwargs, message):
    assert message in _refused(capsys, ["x"], **kwargs)


def test_the_cli_dispatches_move(stores, capsys):
    from hermes_cli.cron import _CRON_SUBCOMMANDS
    from cron.fork_ext.cli import cron_move

    assert _CRON_SUBCOMMANDS["move"] is cron_move


def test_a_job_that_vanishes_mid_move_is_refused_not_a_traceback(stores, monkeypatch, capsys):
    default, shop = stores
    jid = _make(default, profile="grow-shop", next_run_at=_future(60))
    real_load = mv._load
    calls = {"n": 0}

    def load(home):
        jobs = real_load(home)
        if home == default:
            calls["n"] += 1
            if calls["n"] > 1:  # after _check's read: someone deleted it mid-move
                return [j for j in jobs if j["id"] != jid]
        return jobs

    monkeypatch.setattr(mv, "_load", load)
    assert mv.move([jid], to_profile="grow-shop", apply=True) == 1
    assert "left the default store while it was being paused" in capsys.readouterr().out
    assert _get(shop, jid) is None
