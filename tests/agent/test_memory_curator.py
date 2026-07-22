"""Tests for agent/memory_curator.py — slice 1 read-only digest.

The LLM fork (`_run_extraction`) and the session DB (`SessionDB`) are
monkeypatched so tests run fully offline with no credentials and no real
session store.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest


@pytest.fixture
def mc_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME + freshly reloaded memory_curator module."""
    home = tmp_path / ".hermes"
    (home / "memory-curator").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    import agent.memory_curator as mc
    importlib.reload(mc)

    # Default: enabled, no real LLM, no real memory file.
    monkeypatch.setattr(mc, "_load_config", lambda: {"enabled": True})
    monkeypatch.setattr(mc, "_read_current_memory", lambda: "")
    monkeypatch.setattr(
        mc, "_run_extraction",
        lambda prompt: {"final": "### Rule\n- **Lesson:** use feature branches\n",
                        "summary": "1 lesson", "model": "m", "provider": "p", "error": None},
    )
    return {"home": home, "mc": mc}


class _FakeDB:
    """Minimal SessionDB stand-in."""

    def __init__(self, sessions: List[Dict[str, Any]], messages: Dict[str, List[Dict]]):
        self._sessions = sessions
        self._messages = messages

    def list_sessions_rich(self, **kwargs):
        return list(self._sessions)

    def get_messages_as_conversation(self, session_id, **kwargs):
        return list(self._messages.get(session_id, []))


def _install_fake_db(monkeypatch, sessions, messages):
    import hermes_state
    monkeypatch.setattr(
        hermes_state, "SessionDB", lambda *a, **k: _FakeDB(sessions, messages)
    )


# ---------------------------------------------------------------------------
# Config gates
# ---------------------------------------------------------------------------

def test_disabled_by_default(mc_env, monkeypatch):
    mc = mc_env["mc"]
    monkeypatch.setattr(mc, "_load_config", lambda: {})
    assert mc.is_enabled() is False
    assert mc.should_run_now() is False


def test_enabled_via_config(mc_env):
    assert mc_env["mc"].is_enabled() is True


def test_defaults(mc_env):
    mc = mc_env["mc"]
    assert mc.get_interval_hours() == 24 * 7
    assert mc.get_min_idle_hours() == 2
    assert mc.get_lookback_days() == 7
    assert mc.get_max_sessions() == 20


def test_config_overrides(mc_env, monkeypatch):
    mc = mc_env["mc"]
    monkeypatch.setattr(mc, "_load_config", lambda: {
        "enabled": True, "interval_hours": 12, "min_idle_hours": 0.5,
        "lookback_days": 3, "max_sessions": 5,
    })
    assert mc.get_interval_hours() == 12
    assert mc.get_min_idle_hours() == 0.5
    assert mc.get_lookback_days() == 3
    assert mc.get_max_sessions() == 5


# ---------------------------------------------------------------------------
# Interval / first-run gating
# ---------------------------------------------------------------------------

def test_first_run_defers_and_seeds(mc_env):
    mc = mc_env["mc"]
    assert mc.load_state()["last_run_at"] is None
    assert mc.should_run_now() is False           # first observation defers
    assert mc.load_state()["last_run_at"] is not None  # but seeds the clock


def test_runs_after_interval(mc_env):
    mc = mc_env["mc"]
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    st = mc.load_state(); st["last_run_at"] = old; mc.save_state(st)
    assert mc.should_run_now() is True


def test_paused_blocks_run(mc_env):
    mc = mc_env["mc"]
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    st = mc.load_state(); st["last_run_at"] = old; mc.save_state(st)
    mc.set_paused(True)
    assert mc.should_run_now() is False


def test_idle_gate_blocks(mc_env, monkeypatch):
    mc = mc_env["mc"]
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    st = mc.load_state(); st["last_run_at"] = old; mc.save_state(st)
    # Idle for 1 minute < 2h min_idle → no run.
    assert mc.maybe_run_memory_curator(idle_for_seconds=60.0) is None


# ---------------------------------------------------------------------------
# Orchestrator (read-only digest)
# ---------------------------------------------------------------------------

def test_digest_written_and_state_updated(mc_env, monkeypatch):
    mc = mc_env["mc"]
    now = datetime.now(timezone.utc)
    sessions = [{"id": "s1", "title": "T", "last_active": now.isoformat(),
                 "message_count": 4}]
    messages = {"s1": [
        {"role": "user", "content": "no, use a feature branch"},
        {"role": "assistant", "content": "understood, switching to a branch"},
        {"role": "tool", "content": "noise"},
    ]}
    _install_fake_db(monkeypatch, sessions, messages)

    captured = []
    res = mc.run_memory_digest(on_digest=captured.append, now=now, force=True)

    assert res is not None and res["sessions"] == 1
    assert res["digest_path"] and Path(res["digest_path"]).exists()
    assert "use feature branches" in Path(res["digest_path"]).read_text()
    # latest.md mirror exists
    assert (mc_env["home"] / "memory-curator" / "latest.md").exists()
    # State advanced
    st = mc.load_state()
    assert st["run_count"] == 1
    assert st["last_digest_path"] == res["digest_path"]
    assert captured and "1 session" in captured[0]


def test_transcript_skips_tool_and_nonstring(mc_env):
    mc = mc_env["mc"]
    db = _FakeDB([], {"s1": [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "image"}]},   # non-string skipped
        {"role": "tool", "content": "tool noise"},               # tool skipped
        {"role": "assistant", "content": "hi back"},
    ]})
    text = mc._session_transcript(db, "s1", 10000)
    assert "USER: hello" in text
    assert "hi back" in text
    assert "tool noise" not in text
    assert "image" not in text


def test_no_sessions_writes_nothing_new(mc_env, monkeypatch):
    mc = mc_env["mc"]
    _install_fake_db(monkeypatch, [], {})
    res = mc.run_memory_digest(now=datetime.now(timezone.utc), force=True)
    assert res["sessions"] == 0
    assert "NOTHING NEW" in Path(res["digest_path"]).read_text()


def test_lookback_filters_old_sessions(mc_env, monkeypatch):
    mc = mc_env["mc"]
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=30)).isoformat()
    sessions = [{"id": "old", "title": "", "last_active": old, "message_count": 2}]
    messages = {"old": [{"role": "user", "content": "stale"}]}
    _install_fake_db(monkeypatch, sessions, messages)
    res = mc.run_memory_digest(now=now, force=True)
    assert res["sessions"] == 0  # outside 7-day lookback


def test_force_bypasses_interval_gate(mc_env, monkeypatch):
    mc = mc_env["mc"]
    # No last_run_at → should_run_now would defer, but force runs anyway.
    _install_fake_db(monkeypatch, [], {})
    res = mc.run_memory_digest(now=datetime.now(timezone.utc), force=True)
    assert res is not None
