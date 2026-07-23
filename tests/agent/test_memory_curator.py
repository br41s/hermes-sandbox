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


# ---------------------------------------------------------------------------
# Consolidation / eviction (slice 3)
# ---------------------------------------------------------------------------

def _seed_memory(mc, target, entries):
    """Seed store entries through MemoryStore so format/drift stay consistent."""
    store = mc._memory_store()
    for e in entries:
        store.add(target, e)


def test_run_consolidation_proposes_evictions(mc_env, monkeypatch):
    mc = mc_env["mc"]
    _seed_memory(mc, "user", ["Keep this durable preference", "PR 241 status waiting"])
    monkeypatch.setattr(mc, "_run_extraction", lambda prompt: {
        "final": '```json\n{"proposals":[{"action":"evict","target":"user",'
                 '"title":"transient","reason":"transient task state",'
                 '"entry":"PR 241 status waiting"}]}\n```',
        "model": "m", "provider": "p", "error": None})
    res = mc.run_consolidation()
    assert res["proposals"] == 1
    data = mc.load_proposals()
    assert data["proposals"][0]["action"] == "evict"
    assert data["meta"]["mode"] == "consolidation"
    assert "consolidation" in Path(res["digest_path"]).read_text().lower()


def test_consolidation_drops_invented_targets(mc_env, monkeypatch):
    mc = mc_env["mc"]
    _seed_memory(mc, "user", ["Real entry A"])
    monkeypatch.setattr(mc, "_run_extraction", lambda prompt: {
        "final": '{"proposals":[{"action":"evict","target":"user",'
                 '"entry":"Ghost entry not in the store"}]}',
        "model": "m", "provider": "p", "error": None})
    res = mc.run_consolidation()
    assert res["proposals"] == 0  # invented eviction target filtered out


def test_consolidation_empty_memory(mc_env):
    mc = mc_env["mc"]
    res = mc.run_consolidation()
    assert res["proposals"] == 0 and "empty" in res["summary"]


def test_apply_evict_removes_then_revert_readds(mc_env):
    mc = mc_env["mc"]
    from tools.memory_tool import get_memory_dir
    _seed_memory(mc, "user", ["Durable pref", "Transient PR status"])
    props = [{"id": "p1", "action": "evict", "target": "user", "title": "t",
              "lesson": "", "evidence": "", "reason": "transient",
              "entry": "Transient PR status", "applied": False}]
    mc._persist_run(props, {"mode": "consolidation", "entries": 2})

    rep = mc.apply_proposals(["p1"])
    assert rep["applied"] == ["p1"] and not rep["errors"]
    assert "Transient PR status" not in (get_memory_dir() / "USER.md").read_text()
    # evictions never bump the add-count
    assert mc.load_state()["applied_by_target"].get("user", 0) == 0

    # revert re-adds the evicted entry
    r = mc.revert_last()
    assert r["reverted"] == "p1"
    assert "Transient PR status" in (get_memory_dir() / "USER.md").read_text()


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


# ---------------------------------------------------------------------------
# Merge (slice 3b) — compose N entries into 1, restore N on revert
# ---------------------------------------------------------------------------

def test_parse_merge_reads_sources(mc_env):
    mc = mc_env["mc"]
    props = mc._parse_proposals(
        '{"proposals":[{"action":"merge","target":"user",'
        '"sources":["a","b"],"entry":"ab merged"}]}'
    )
    assert len(props) == 1
    assert props[0]["action"] == "merge"
    assert props[0]["sources"] == ["a", "b"]


def test_consolidation_proposes_merge(mc_env, monkeypatch):
    mc = mc_env["mc"]
    _seed_memory(mc, "user",
                 ["User is direct and Spanish", "User prefers Spanish, direct style"])
    monkeypatch.setattr(mc, "_run_extraction", lambda prompt: {"final":
        '```json\n{"proposals":[{"action":"merge","target":"user","title":"profile",'
        '"reason":"overlap",'
        '"sources":["User is direct and Spanish","User prefers Spanish, direct style"],'
        '"entry":"User: direct, Spanish"}]}\n```',
        "model": "m", "provider": "p", "error": None})
    res = mc.run_consolidation()
    assert res["proposals"] == 1
    assert mc.load_proposals()["proposals"][0]["action"] == "merge"


def test_consolidation_drops_merge_with_missing_source(mc_env, monkeypatch):
    mc = mc_env["mc"]
    _seed_memory(mc, "user", ["Real A", "Real B"])
    monkeypatch.setattr(mc, "_run_extraction", lambda prompt: {"final":
        '{"proposals":[{"action":"merge","target":"user",'
        '"sources":["Real A","Ghost C"],"entry":"merged"}]}',
        "model": "m", "provider": "p", "error": None})
    assert mc.run_consolidation()["proposals"] == 0


def test_consolidation_drops_merge_single_source(mc_env, monkeypatch):
    mc = mc_env["mc"]
    _seed_memory(mc, "user", ["Real A"])
    monkeypatch.setattr(mc, "_run_extraction", lambda prompt: {"final":
        '{"proposals":[{"action":"merge","target":"user",'
        '"sources":["Real A"],"entry":"merged"}]}',
        "model": "m", "provider": "p", "error": None})
    assert mc.run_consolidation()["proposals"] == 0  # needs >=2 sources


def test_apply_merge_then_revert_roundtrip(mc_env):
    mc = mc_env["mc"]
    from tools.memory_tool import get_memory_dir
    _seed_memory(mc, "user", [
        "User is direct and Spanish",
        "User prefers Spanish, direct style",
        "Keep me untouched",
    ])
    props = [{"id": "p1", "action": "merge", "target": "user", "title": "profile",
              "lesson": "", "evidence": "", "reason": "overlap",
              "sources": ["User is direct and Spanish",
                          "User prefers Spanish, direct style"],
              "entry": "User: direct, Spanish", "applied": False}]
    mc._persist_run(props, {"mode": "consolidation", "entries": 3})

    rep = mc.apply_proposals(["p1"])
    assert rep["applied"] == ["p1"] and not rep["errors"]
    txt = (get_memory_dir() / "USER.md").read_text()
    assert "User: direct, Spanish" in txt
    assert "User is direct and Spanish" not in txt
    assert "Keep me untouched" in txt            # untouched
    assert mc.load_state()["applied_by_target"].get("user", 0) == 0  # merge != add

    r = mc.revert_last()
    assert r["reverted"] == "p1"
    txt2 = (get_memory_dir() / "USER.md").read_text()
    assert "User: direct, Spanish" not in txt2
    assert "User is direct and Spanish" in txt2
    assert "User prefers Spanish, direct style" in txt2


def test_apply_merge_missing_source_no_mutation(mc_env):
    mc = mc_env["mc"]
    from tools.memory_tool import get_memory_dir
    _seed_memory(mc, "user", ["Real A", "Real B"])
    props = [{"id": "p1", "action": "merge", "target": "user", "title": "t",
              "lesson": "", "evidence": "", "reason": "",
              "sources": ["Real A", "Ghost"], "entry": "merged", "applied": False}]
    mc._persist_run(props, {"mode": "consolidation", "entries": 2})
    rep = mc.apply_proposals(["p1"])
    assert rep["applied"] == []
    assert any("source not found" in e for e in rep["errors"])
    txt = (get_memory_dir() / "USER.md").read_text()
    assert "Real A" in txt and "Real B" in txt and "merged" not in txt


def test_apply_merge_rollback_on_cap(mc_env, monkeypatch):
    mc = mc_env["mc"]
    from tools.memory_tool import get_memory_dir, MemoryStore
    _seed_memory(mc, "user", ["aaaa", "bbbb"])
    # Tiny store so the merged entry blows the cap → add fails → rollback.
    monkeypatch.setattr(
        mc, "_memory_store",
        lambda: MemoryStore(memory_char_limit=50, user_char_limit=12),
    )
    props = [{"id": "p1", "action": "merge", "target": "user", "title": "t",
              "lesson": "", "evidence": "", "reason": "",
              "sources": ["aaaa", "bbbb"], "entry": "x" * 100, "applied": False}]
    mc._persist_run(props, {"mode": "consolidation", "entries": 2})
    rep = mc.apply_proposals(["p1"])
    assert rep["applied"] == []
    assert any("rolled back" in e for e in rep["errors"])
    txt = (get_memory_dir() / "USER.md").read_text()
    assert "aaaa" in txt and "bbbb" in txt        # restored by rollback


# ---------------------------------------------------------------------------
# Auto-graduation (slice 4)
# ---------------------------------------------------------------------------

def _add_prop(entry, target="user"):
    return [{"id": "p1", "action": "add", "target": target, "title": "", "lesson": "",
             "evidence": "", "reason": "", "sources": [], "entry": entry, "applied": False}]


def test_graduation_config_defaults(mc_env):
    mc = mc_env["mc"]
    assert mc.get_graduation_k() == 5
    assert mc.is_auto_apply_enabled() is False
    assert mc.get_graduatable_actions() == ["add"]


def test_graduation_counts_and_is_graduated(mc_env):
    mc = mc_env["mc"]
    for i in range(5):
        mc._persist_run(_add_prop(f"lesson number {i}"), {"sessions": 1})
        assert mc.apply_proposals(["p1"])["applied"] == ["p1"]
    assert mc.load_state()["graduation"]["add:user"] == 5
    assert mc.is_graduated("add", "user") is True
    assert mc.is_graduated("add", "memory") is False  # separate class, no approvals


def test_below_k_not_graduated(mc_env):
    mc = mc_env["mc"]
    for i in range(3):
        mc._persist_run(_add_prop(f"lesson {i}"), {"sessions": 1})
        mc.apply_proposals(["p1"])
    assert mc.is_graduated("add", "user") is False  # 3 < K(5)


def test_evict_and_merge_never_graduate(mc_env):
    mc = mc_env["mc"]
    st = mc.load_state()
    st["graduation"] = {"evict:user": 99, "merge:user": 99}
    mc.save_state(st)
    assert mc.is_graduated("evict", "user") is False
    assert mc.is_graduated("merge", "user") is False


def test_revert_resets_graduation(mc_env):
    mc = mc_env["mc"]
    for i in range(5):
        mc._persist_run(_add_prop(f"lesson {i}"), {"sessions": 1})
        mc.apply_proposals(["p1"])
    assert mc.is_graduated("add", "user") is True
    mc.revert_last()  # revert the last add → trust withdrawn
    assert mc.load_state()["graduation"]["add:user"] == 0
    assert mc.is_graduated("add", "user") is False


def _digest_env(mc, monkeypatch, auto_apply):
    cfg = {"enabled": True}
    if auto_apply:
        cfg["auto_apply"] = True
    monkeypatch.setattr(mc, "_load_config", lambda: cfg)
    now = datetime.now(timezone.utc)
    _install_fake_db(monkeypatch,
                     [{"id": "s1", "title": "T", "last_active": now.isoformat()}],
                     {"s1": [{"role": "user", "content": "x"}]})
    return now


def test_digest_auto_applies_graduated_class(mc_env, monkeypatch):
    mc = mc_env["mc"]
    from tools.memory_tool import get_memory_dir
    now = _digest_env(mc, monkeypatch, auto_apply=True)
    st = mc.load_state(); st["graduation"] = {"add:user": 5}; mc.save_state(st)
    res = mc.run_memory_digest(now=now, force=True)  # fixture stub → add:user proposal
    assert res["auto_applied"] == ["p1"]
    assert "Use a feature branch per change" in (get_memory_dir() / "USER.md").read_text()
    assert "auto-applied" in mc.load_state()["last_run_summary"]


def test_digest_no_autoapply_when_switch_off(mc_env, monkeypatch):
    mc = mc_env["mc"]
    now = _digest_env(mc, monkeypatch, auto_apply=False)   # default off
    st = mc.load_state(); st["graduation"] = {"add:user": 5}; mc.save_state(st)
    res = mc.run_memory_digest(now=now, force=True)
    assert res["auto_applied"] == []


def test_digest_no_autoapply_when_not_graduated(mc_env, monkeypatch):
    mc = mc_env["mc"]
    now = _digest_env(mc, monkeypatch, auto_apply=True)    # on, but no graduation
    res = mc.run_memory_digest(now=now, force=True)
    assert res["auto_applied"] == []


# ---------------------------------------------------------------------------
# Telegram notification (Option A)
# ---------------------------------------------------------------------------

def test_notify_telegram_off_without_config(mc_env, monkeypatch):
    mc = mc_env["mc"]
    calls = []
    monkeypatch.setattr(mc, "_http_post_json", lambda *a, **k: calls.append(a) or 200)
    # no token, no chat_id → no send
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert mc._notify_telegram("hi") is False
    assert calls == []


def test_notify_telegram_needs_both_token_and_chat(mc_env, monkeypatch):
    mc = mc_env["mc"]
    calls = []
    monkeypatch.setattr(mc, "_http_post_json", lambda url, payload, **k: calls.append(payload) or 200)
    # token but no chat_id
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setattr(mc, "_load_config", lambda: {"enabled": True})
    assert mc._notify_telegram("hi") is False and calls == []
    # token + chat_id → sends
    monkeypatch.setattr(mc, "_load_config",
                        lambda: {"enabled": True, "telegram_chat_id": "-100123",
                                 "telegram_thread_id": "1904"})
    assert mc._notify_telegram("hi") is True
    assert calls and calls[0]["chat_id"] == "-100123"
    assert calls[0]["message_thread_id"] == "1904"
    assert calls[0]["text"] == "hi"


def test_build_telegram_message(mc_env):
    mc = mc_env["mc"]
    props = [
        {"id": "p1", "action": "add", "title": "Lesson A", "entry": "a"},
        {"id": "p2", "action": "evict", "title": "Stale B", "entry": "b"},
    ]
    msg = mc._build_telegram_message(props, ["p1"], "digest")
    assert "2 propuesta" in msg
    assert "[p1] Lesson A ✅auto" in msg
    assert "🗑 [p2] Stale B" in msg
    assert "hermes memory-curator show" in msg


def test_digest_notify_only_when_flag_and_proposals(mc_env, monkeypatch):
    mc = mc_env["mc"]
    sent = []
    monkeypatch.setattr(mc, "_notify_telegram", lambda text: sent.append(text) or True)
    now = datetime.now(timezone.utc)
    _install_fake_db(monkeypatch,
                     [{"id": "s1", "title": "T", "last_active": now.isoformat()}],
                     {"s1": [{"role": "user", "content": "x"}]})
    # notify=False (CLI default) → no send even with a proposal
    mc.run_memory_digest(now=now, force=True, notify=False)
    assert sent == []
    # notify=True (tick path) → send
    mc.run_memory_digest(now=now, force=True, notify=True)
    assert len(sent) == 1 and "propuesta" in sent[0]


def test_digest_notify_silent_when_no_proposals(mc_env, monkeypatch):
    mc = mc_env["mc"]
    sent = []
    monkeypatch.setattr(mc, "_notify_telegram", lambda text: sent.append(text) or True)
    _install_fake_db(monkeypatch, [], {})   # no sessions → no proposals
    mc.run_memory_digest(now=datetime.now(timezone.utc), force=True, notify=True)
    assert sent == []
