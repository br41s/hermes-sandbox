"""Regression lock for the ChatMemo laptop-backup heartbeat signal.

ChatMemo's nightly backup runs on Brais's Mac. From May to October 2026 its
LaunchAgent pointed at a script that did not exist: every night it exited 127,
no dump was ever written, and nothing said so — a job that never starts cannot
send its own failure notification. These tests pin the outside check: the
backup rewrites a gist after each verified dump, and silence past the
threshold is a brief, as is a heartbeat that cannot be read.

Hermetic: injected heartbeat and clock; the network is never touched.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import incidents.sweep as sw
from incidents.sweep import LAPTOP_BACKUP_STALE_HOURS, laptop_backup_incidents

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)


def _beat(ago_hours):
    return {"job": "chatmemo-backup",
            "ok_at": (NOW - timedelta(hours=ago_hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "dump": "chatmemo-2026-10-07.dump", "bytes": 3765015}


def test_recent_backup_is_silent_and_a_stall_alerts_once():
    assert laptop_backup_incidents(heartbeat=_beat(20), now=NOW) == []
    assert laptop_backup_incidents(heartbeat=_beat(LAPTOP_BACKUP_STALE_HOURS - 1), now=NOW) == []

    stale = _beat(LAPTOP_BACKUP_STALE_HOURS + 1)
    out = laptop_backup_incidents(heartbeat=stale, now=NOW)
    assert len(out) == 1
    assert out[0].kind == "laptop_backup"
    assert "chatmemo-2026-10-07.dump" in out[0].detail
    # One stall is one brief: the id carries the last success, not the clock.
    later = laptop_backup_incidents(heartbeat=stale, now=NOW + timedelta(hours=5))
    assert later[0].id == out[0].id


@pytest.mark.parametrize("heartbeat", [{}, {"ok_at": "not a date"}, ["not", "a", "dict"]])
def test_unreadable_heartbeat_alerts_instead_of_reading_healthy(heartbeat):
    out = laptop_backup_incidents(heartbeat=heartbeat, now=NOW)
    assert len(out) == 1
    assert "unreadable" in out[0].detail


def test_outside_the_deployment_it_never_calls_github(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("network touched outside the deployment")

    monkeypatch.setattr(sw, "_fetch_gist_file", _boom)
    assert laptop_backup_incidents(in_deployment=False, now=NOW) == []


def test_refused_or_missing_gist_is_blind_not_silent(monkeypatch):
    def _refused(*a, **k):
        raise sw.DependencyAlertBlind(404, "gist gone")

    monkeypatch.setattr(sw, "_fetch_gist_file", _refused)
    out = laptop_backup_incidents(in_deployment=True, now=NOW)
    assert len(out) == 1 and "BLIND" in out[0].title

    monkeypatch.setattr(sw, "_fetch_gist_file", lambda *a, **k: None)  # transient
    assert laptop_backup_incidents(in_deployment=True, now=NOW) == []

    monkeypatch.setattr(sw, "_fetch_gist_file", lambda *a, **k: json.dumps(_beat(3)))
    assert laptop_backup_incidents(in_deployment=True, now=NOW) == []


def test_sweep_delivers_it_and_checks_by_default(tmp_path, monkeypatch):
    jobs = [{"id": "ok1", "name": "healthy", "last_error": None,
             "last_delivery_error": None,
             "last_run_at": datetime.now(timezone.utc).isoformat()}]
    stall = laptop_backup_incidents(heartbeat=_beat(80), now=NOW)
    out = sw.sweep(jobs=jobs, langfuse=[], checkout_drift=[], judge_liveness=[],
                   laptop_backup=stall, state_path=tmp_path / "a.json")
    assert "backup on the mac has not run" in out.lower()

    called = {"n": 0}

    def _spy(*a, **k):
        called["n"] += 1
        return []

    monkeypatch.setattr(sw, "laptop_backup_incidents", _spy)
    sw.sweep(jobs=jobs, langfuse=[], checkout_drift=[], judge_liveness=[],
             state_path=tmp_path / "b.json")
    assert called["n"] == 1
