"""Integration tests for the CRM dashboard plugin API.

Exercises the FastAPI router directly (auth middleware is the host's job) and
the read-only resolution against a synthetic gateway ``sessions.json``.
"""

from __future__ import annotations

import importlib
import json

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Per-profile store + a fake HERMES_HOME so sessions.json resolves locally.
    monkeypatch.setenv("HERMES_CRM_DB", str(tmp_path / "crm.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    # Reload modules so they pick up the patched env / fresh db path cache.
    from plugins.crm import crm_db
    importlib.reload(crm_db)
    api = importlib.import_module("plugins.crm.dashboard.plugin_api")
    importlib.reload(api)

    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/crm")
    return TestClient(app), tmp_path, api


def _write_sessions(tmp_path, entries: dict):
    sdir = tmp_path / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "sessions.json").write_text(json.dumps(entries), encoding="utf-8")


def test_meta(client):
    c, *_ = client
    r = c.get("/api/plugins/crm/meta")
    assert r.status_code == 200
    assert r.json()["statuses"] == ["lead", "active", "customer", "archived"]


def test_crud_flow(client):
    c, *_ = client
    # create
    r = c.post("/api/plugins/crm/contacts", json={
        "display_name": "Ada", "status": "lead", "tags": ["vip"],
        "handles": [{"platform": "telegram", "user_id": "42"}],
    })
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    # list + search
    assert c.get("/api/plugins/crm/contacts").json()["total"] == 1
    assert c.get("/api/plugins/crm/contacts?q=ad").json()["total"] == 1
    assert c.get("/api/plugins/crm/contacts?status=customer").json()["total"] == 0
    # patch (promote)
    r = c.patch(f"/api/plugins/crm/contacts/{cid}", json={"status": "customer"})
    assert r.json()["status"] == "customer"
    # get
    assert c.get(f"/api/plugins/crm/contacts/{cid}").json()["display_name"] == "Ada"
    # delete
    assert c.delete(f"/api/plugins/crm/contacts/{cid}").json()["deleted"] is True
    assert c.get(f"/api/plugins/crm/contacts/{cid}").status_code == 404


def test_create_requires_name(client):
    c, *_ = client
    r = c.post("/api/plugins/crm/contacts", json={"display_name": "   "})
    assert r.status_code == 400


def test_conversations_resolution(client):
    c, tmp_path, _ = client
    # contact linked to telegram:42
    cid = c.post("/api/plugins/crm/contacts", json={
        "display_name": "Ada",
        "handles": [{"platform": "telegram", "user_id": "42"}],
    }).json()["id"]
    # gateway sessions: one matching DM, one other person
    _write_sessions(tmp_path, {
        "k1": {
            "session_id": "sid_ada", "updated_at": "2026-06-01T10:00:00",
            "chat_type": "dm", "display_name": "Ada T",
            "origin": {"platform": "telegram", "user_id": "42", "user_name": "Ada T"},
        },
        "k2": {
            "session_id": "sid_bob", "updated_at": "2026-06-02T10:00:00",
            "chat_type": "dm",
            "origin": {"platform": "telegram", "user_id": "99", "user_name": "Bob"},
        },
    })
    convos = c.get(f"/api/plugins/crm/contacts/{cid}/conversations").json()["conversations"]
    assert [s["session_id"] for s in convos] == ["sid_ada"]
    assert convos[0]["user_name"] == "Ada T"


def test_unlinked_excludes_linked_and_groups(client):
    c, tmp_path, _ = client
    # Ada is linked; Bob is not; a group chat must be excluded.
    c.post("/api/plugins/crm/contacts", json={
        "display_name": "Ada",
        "handles": [{"platform": "telegram", "user_id": "42"}],
    })
    _write_sessions(tmp_path, {
        "k1": {"session_id": "s1", "updated_at": "2026-06-01", "chat_type": "dm",
               "origin": {"platform": "telegram", "user_id": "42", "user_name": "Ada"}},
        "k2": {"session_id": "s2", "updated_at": "2026-06-02", "chat_type": "dm",
               "origin": {"platform": "telegram", "user_id": "99", "user_name": "Bob"}},
        "k3": {"session_id": "s3", "updated_at": "2026-06-03", "chat_type": "group",
               "origin": {"platform": "telegram", "user_id": "1000", "user_name": "Grp"}},
    })
    unlinked = c.get("/api/plugins/crm/unlinked").json()["unlinked"]
    keys = {f"{u['platform']}:{u['user_id']}" for u in unlinked}
    assert keys == {"telegram:99"}  # Ada linked, group excluded


def test_conversations_missing_contact(client):
    c, *_ = client
    assert c.get("/api/plugins/crm/contacts/nope/conversations").status_code == 404
