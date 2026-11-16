"""Tests for the Telegram approval check: only a user message of the owner's
chat in the gateway's own state.db counts as "the owner said so".
"""
from __future__ import annotations

import sqlite3

import pytest

from plugins.shorts import approval

CARPETA = "2026-11-14_es-seo"


def _db(tmp_path, rows):
    """A state.db with only the columns the approval query uses; ``rows`` are
    (session_id, source, chat_id, role, content, timestamp)."""
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, chat_id TEXT)")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
    for sid, source, chat, role, content, ts in rows:
        conn.execute("INSERT OR IGNORE INTO sessions (id, source, chat_id) VALUES (?, ?, ?)", (sid, source, chat))
        conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                     (sid, role, content, ts))
    conn.commit()
    conn.close()
    return path


def _find(db, target=CARPETA, chat_id="42", after=100.0):
    return approval.find(approval.phrase("publica", target), chat_id=chat_id, after=after, db_path=db)


def test_the_first_match_after_the_mark_is_found(tmp_path):
    db = _db(tmp_path, [
        ("s1", "telegram", "42", "user", f"publica {CARPETA}", 200.0),
        ("s2", "telegram", "42", "user", f"publica {CARPETA} otra vez", 300.0),
    ])
    hit = _find(db)
    assert hit == {"text": f"publica {CARPETA}", "at": 200.0, "session_id": "s1"}


def test_a_match_before_the_mark_is_ignored(tmp_path):
    db = _db(tmp_path, [("s1", "telegram", "42", "user", f"publica {CARPETA}", 50.0)])
    assert _find(db) is None


def test_only_user_messages_count(tmp_path):
    db = _db(tmp_path, [
        ("s1", "telegram", "42", "assistant", f"publica {CARPETA}", 200.0),
        ("s1", "telegram", "42", "tool", f"publica {CARPETA}", 300.0),
    ])
    assert _find(db) is None


def test_another_chat_is_ignored(tmp_path):
    db = _db(tmp_path, [("s1", "telegram", "99", "user", f"publica {CARPETA}", 200.0)])
    assert _find(db) is None
    assert _find(db, chat_id="99") is not None


def test_another_source_is_ignored(tmp_path):
    db = _db(tmp_path, [("s1", "cron", "42", "user", f"publica {CARPETA}", 200.0)])
    assert _find(db) is None


def test_a_longer_folder_name_does_not_match_a_prefix(tmp_path):
    db = _db(tmp_path, [
        ("s1", "telegram", "42", "user", f"publica {CARPETA}-local", 200.0),
        ("s2", "telegram", "42", "user", f"publica {CARPETA}", 300.0),
    ])
    hit = _find(db)
    assert hit["at"] == 300.0 and hit["session_id"] == "s2"
    assert _find(db, target=f"{CARPETA}-local")["at"] == 200.0


def test_a_name_prefix_and_capitalisation_do_not_get_in_the_way(tmp_path):
    db = _db(tmp_path, [("s1", "telegram", "42", "user", f"[Brais Natural] Publica {CARPETA}", 200.0)])
    assert _find(db)["text"] == f"[Brais Natural] Publica {CARPETA}"


def test_a_missing_database_is_no_approval(tmp_path):
    assert _find(tmp_path / "nope.db") is None


def test_a_corrupted_database_is_no_approval(tmp_path):
    path = tmp_path / "state.db"
    path.write_bytes(b"this is not a sqlite database")
    assert _find(path) is None


def test_the_default_db_is_the_profile_state_db(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path, raising=False)
    _db(tmp_path, [("s1", "telegram", "42", "user", f"publica {CARPETA}", 200.0)])
    hit = approval.find(approval.phrase("publica", CARPETA), chat_id="42", after=100.0)
    assert hit["session_id"] == "s1"
