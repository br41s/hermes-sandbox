"""Regression lock for the auditor liveness heartbeat (auditor/pending.py).

Hermetic: injects now + a tmp state_path, so no clock or cron data is touched.
Guards the configured behaviour — the incidents thread stays quiet except for one
heartbeat per HEARTBEAT_HOURS, and an escalation resets the clock (touch).
"""
import json
from datetime import timedelta

from auditor import pending
from auditor.pending import HEARTBEAT_HOURS


def test_first_run_emits_and_records(tmp_path):
    sp = tmp_path / "state.json"
    now = pending._now()
    line = pending.heartbeat(sp, now=now)
    assert line and "still running" in line
    assert json.loads(sp.read_text())["last_heartbeat_at"] == now.isoformat()


def test_silent_within_window(tmp_path):
    sp = tmp_path / "state.json"
    now = pending._now()
    pending.heartbeat(sp, now=now)
    soon = now + timedelta(hours=HEARTBEAT_HOURS - 1)
    assert pending.heartbeat(sp, now=soon) == ""
    # clock not advanced by a silent (not-due) call
    assert json.loads(sp.read_text())["last_heartbeat_at"] == now.isoformat()


def test_emits_again_after_window(tmp_path):
    sp = tmp_path / "state.json"
    now = pending._now()
    pending.heartbeat(sp, now=now)
    later = now + timedelta(hours=HEARTBEAT_HOURS)
    line = pending.heartbeat(sp, now=later)
    assert line
    assert json.loads(sp.read_text())["last_heartbeat_at"] == later.isoformat()


def test_touch_resets_clock_without_emitting(tmp_path):
    sp = tmp_path / "state.json"
    now = pending._now()
    out = pending.heartbeat(sp, now=now, touch_only=True)
    assert out == ""  # escalation already proved liveness — no extra line
    assert json.loads(sp.read_text())["last_heartbeat_at"] == now.isoformat()
    # and a heartbeat right after the touch stays silent (clock was reset)
    soon = now + timedelta(hours=1)
    assert pending.heartbeat(sp, now=soon) == ""


def test_heartbeat_state_coexists_with_seen(tmp_path):
    sp = tmp_path / "state.json"
    pending.mark_reviewed("o/r", 7, "abc", sp)
    pending.heartbeat(sp, now=pending._now())
    state = json.loads(sp.read_text())
    # neither writer clobbers the other's key
    assert state["seen"] == ["o/r#7@abc"]
    assert "last_heartbeat_at" in state
    # and a later mark preserves the heartbeat clock
    hb = state["last_heartbeat_at"]
    pending.mark_reviewed("o/r", 8, "def", sp)
    assert json.loads(sp.read_text())["last_heartbeat_at"] == hb
