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
    _stub_json = (
        '```json\n{"proposals": [{"target": "user", "title": "branches", '
        '"lesson": "use feature branches", "evidence": "s1", '
        '"entry": "Use a feature branch per change"}]}\n```'
    )
    monkeypatch.setattr(
        mc, "_run_extraction",
        lambda prompt: {"final": _stub_json, "summary": "1 lesson",
                        "model": "m", "provider": "p", "error": None},
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

    assert res is not None and res["sessions"] == 1 and res["proposals"] == 1
    assert res["digest_path"] and Path(res["digest_path"]).exists()
    assert "Use a feature branch per change" in Path(res["digest_path"]).read_text()
    # proposals.json is the source of truth for apply
    data = mc.load_proposals()
    assert len(data["proposals"]) == 1
    assert data["proposals"][0]["target"] == "user"
    assert data["proposals"][0]["applied"] is False
    # latest.md mirror exists
    assert (mc_env["home"] / "memory-curator" / "latest.md").exists()
    # State advanced
    st = mc.load_state()
    assert st["run_count"] == 1 and st["pending_proposals"] == 1
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


# ---------------------------------------------------------------------------
# Proposal parsing (pure function)
# ---------------------------------------------------------------------------

def test_parse_proposals_fenced_and_coerces_target(mc_env):
    mc = mc_env["mc"]
    text = ('prose ```json\n{"proposals":[{"target":"memory","entry":"a"},'
            '{"target":"bogus","entry":"b"}]}\n``` trailing')
    props = mc._parse_proposals(text)
    assert [p["id"] for p in props] == ["p1", "p2"]
    assert props[1]["target"] == "memory"  # invalid target coerced to memory


def test_parse_proposals_skips_empty_and_garbage(mc_env):
    mc = mc_env["mc"]
    props = mc._parse_proposals(
        '{"proposals":[{"target":"user","entry":""},{"target":"user","entry":"keep"}]}'
    )
    assert len(props) == 1 and props[0]["entry"] == "keep"
    assert mc._parse_proposals("no json here") == []
    assert mc._parse_proposals("") == []


# ---------------------------------------------------------------------------
# Apply / revert (write path) — real MemoryStore in the isolated HERMES_HOME
# ---------------------------------------------------------------------------

def _seed(mc, home, proposals):
    (home / "memories").mkdir(parents=True, exist_ok=True)
    mc._persist_run(proposals, {"sessions": 1})


def _props():
    return [
        {"id": "p1", "target": "user", "title": "t1", "lesson": "l1",
         "evidence": "s1", "entry": "Use a feature branch per change", "applied": False},
        {"id": "p2", "target": "memory", "title": "t2", "lesson": "l2",
         "evidence": "s2", "entry": "Cron store is centralized", "applied": False},
    ]


def test_apply_selected_writes_and_records(mc_env):
    mc, home = mc_env["mc"], mc_env["home"]
    _seed(mc, home, _props())
    report = mc.apply_proposals(["p1"])
    assert report["applied"] == ["p1"] and not report["errors"]

    from tools.memory_tool import get_memory_dir
    assert "Use a feature branch per change" in (get_memory_dir() / "USER.md").read_text()

    data = mc.load_proposals()
    by_id = {p["id"]: p for p in data["proposals"]}
    assert by_id["p1"]["applied"] is True and by_id["p2"]["applied"] is False

    ledger = mc._digest_dir() / "applied.jsonl"
    assert ledger.exists() and "p1" in ledger.read_text()
    assert mc.load_state()["applied_by_target"]["user"] == 1


def test_apply_all_writes_both(mc_env):
    mc, home = mc_env["mc"], mc_env["home"]
    _seed(mc, home, _props())
    report = mc.apply_proposals(apply_all=True)
    assert sorted(report["applied"]) == ["p1", "p2"]

    from tools.memory_tool import get_memory_dir
    assert "Cron store is centralized" in (get_memory_dir() / "MEMORY.md").read_text()


def test_apply_is_idempotent(mc_env):
    mc, home = mc_env["mc"], mc_env["home"]
    _seed(mc, home, _props())
    mc.apply_proposals(["p1"])
    again = mc.apply_proposals(["p1"])
    assert again["applied"] == []
    assert any("already applied" in s for s in again["skipped"])


def test_apply_unknown_id_errors(mc_env):
    mc, home = mc_env["mc"], mc_env["home"]
    _seed(mc, home, _props())
    report = mc.apply_proposals(["p9"])
    assert report["applied"] == []
    assert any("unknown proposal id" in e for e in report["errors"])


def test_apply_no_proposals(mc_env):
    mc = mc_env["mc"]
    report = mc.apply_proposals(apply_all=True)
    assert report["applied"] == [] and report["errors"]


def test_revert_last_removes_entry(mc_env):
    mc, home = mc_env["mc"], mc_env["home"]
    _seed(mc, home, _props())
    mc.apply_proposals(["p1"])
    from tools.memory_tool import get_memory_dir
    assert "Use a feature branch per change" in (get_memory_dir() / "USER.md").read_text()

    res = mc.revert_last()
    assert res["reverted"] == "p1" and res["target"] == "user"
    assert "Use a feature branch per change" not in (get_memory_dir() / "USER.md").read_text()
    # second revert finds nothing (the entry was already undone)
    assert mc.revert_last()["reverted"] is None


def test_revert_without_ledger(mc_env):
    mc = mc_env["mc"]
    assert mc.revert_last()["reverted"] is None


def test_apply_revert_reapply_roundtrip(mc_env):
    """revert is a true inverse: the proposal can be applied again afterwards."""
    mc, home = mc_env["mc"], mc_env["home"]
    from tools.memory_tool import get_memory_dir
    _seed(mc, home, _props())

    assert mc.apply_proposals(["p1"])["applied"] == ["p1"]
    assert mc.load_state()["applied_by_target"]["user"] == 1

    assert mc.revert_last()["reverted"] == "p1"
    # flag cleared + count decremented
    p1 = next(p for p in mc.load_proposals()["proposals"] if p["id"] == "p1")
    assert p1["applied"] is False
    assert mc.load_state()["applied_by_target"].get("user", 0) == 0
    # latest.md no longer shows the applied checkmark
    assert "✅ applied" not in (mc._digest_dir() / "latest.md").read_text()

    # re-apply now succeeds (was the bug: skipped as "already applied")
    again = mc.apply_proposals(["p1"])
    assert again["applied"] == ["p1"] and not again["errors"]
    assert "Use a feature branch per change" in (get_memory_dir() / "USER.md").read_text()
    assert mc.load_state()["applied_by_target"]["user"] == 1
