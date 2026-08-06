"""Regression guard: /api/delegate must not run open on the public dashboard app.

``/api/delegate`` sits on ``PUBLIC_API_PATHS`` so the dashboard cookie/OAuth
gate never touches it (BigLobster's server-to-server call carries no
session). That means the handler itself is the only thing standing between
the public internet and "run an arbitrary prompt on this agent, and POST the
result wherever I say" — there used to be nothing there at all. This file
locks the fix: a missing/unset secret must fail closed (503), a wrong or
absent ``x-hermes-secret`` header must 401, and even an authenticated
request must not be allowed to point ``webhook_url`` at a loopback/private/
internal address.
"""

import pytest
from starlette.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS

SECRET = "test-delegate-secret"


def _client():
    prev_auth = getattr(web_server.app.state, "auth_required", None)
    prev_host = getattr(web_server.app.state, "bound_host", None)
    web_server.app.state.auth_required = True
    web_server.app.state.bound_host = None
    client = TestClient(web_server.app)
    return client, prev_auth, prev_host


def _restore(prev_auth, prev_host):
    if prev_auth is None:
        if hasattr(web_server.app.state, "auth_required"):
            delattr(web_server.app.state, "auth_required")
    else:
        web_server.app.state.auth_required = prev_auth
    if prev_host is None:
        if hasattr(web_server.app.state, "bound_host"):
            delattr(web_server.app.state, "bound_host")
    else:
        web_server.app.state.bound_host = prev_host


def _body(**overrides):
    body = {
        "task_id": "t1",
        "prompt": "do something",
        "webhook_url": "https://biglobster.top/api/hermes-callback",
    }
    body.update(overrides)
    return body


def test_delegate_path_is_public():
    """The route legitimately bypasses the cookie gate — that's why the
    handler's own auth check is load-bearing."""
    assert "/api/delegate" in PUBLIC_API_PATHS


def test_no_secret_configured_fails_closed(monkeypatch):
    monkeypatch.delenv("HERMES_CALLBACK_SECRET", raising=False)
    scheduled = []
    monkeypatch.setattr(web_server, "_delegate_background",
                        lambda *a, **kw: scheduled.append(a))
    client, pa, ph = _client()
    try:
        resp = client.post("/api/delegate", json=_body())
        assert resp.status_code == 503
        assert scheduled == []
    finally:
        _restore(pa, ph)
        client.close()


def test_missing_header_401(monkeypatch):
    monkeypatch.setenv("HERMES_CALLBACK_SECRET", SECRET)
    scheduled = []
    monkeypatch.setattr(web_server, "_delegate_background",
                        lambda *a, **kw: scheduled.append(a))
    client, pa, ph = _client()
    try:
        resp = client.post("/api/delegate", json=_body())
        assert resp.status_code == 401
        assert scheduled == []
    finally:
        _restore(pa, ph)
        client.close()


def test_wrong_secret_401(monkeypatch):
    monkeypatch.setenv("HERMES_CALLBACK_SECRET", SECRET)
    scheduled = []
    monkeypatch.setattr(web_server, "_delegate_background",
                        lambda *a, **kw: scheduled.append(a))
    client, pa, ph = _client()
    try:
        resp = client.post(
            "/api/delegate",
            json=_body(),
            headers={"x-hermes-secret": "not-the-secret"},
        )
        assert resp.status_code == 401
        assert scheduled == []
    finally:
        _restore(pa, ph)
        client.close()


def test_valid_secret_accepted(monkeypatch):
    monkeypatch.setenv("HERMES_CALLBACK_SECRET", SECRET)
    scheduled = []

    async def _fake_background(*a, **kw):
        scheduled.append(a)

    monkeypatch.setattr(web_server, "_delegate_background", _fake_background)
    client, pa, ph = _client()
    try:
        resp = client.post(
            "/api/delegate",
            json=_body(),
            headers={"x-hermes-secret": SECRET},
        )
        assert resp.status_code == 202
        assert resp.json() == {"task_id": "t1", "status": "accepted"}
        assert len(scheduled) == 1
    finally:
        _restore(pa, ph)
        client.close()


@pytest.mark.parametrize(
    "webhook_url",
    [
        "http://biglobster.top/api/hermes-callback",  # not https
        "https://127.0.0.1/steal",  # loopback
        "https://localhost/steal",
        "https://169.254.169.254/latest/meta-data",  # link-local / cloud metadata
        "https://hermes-sandbox.zeabur.internal:9119/steal",  # internal host
        "https://10.0.0.5/steal",  # private range
    ],
)
def test_unsafe_webhook_url_rejected(monkeypatch, webhook_url):
    monkeypatch.setenv("HERMES_CALLBACK_SECRET", SECRET)
    scheduled = []
    monkeypatch.setattr(web_server, "_delegate_background",
                        lambda *a, **kw: scheduled.append(a))
    client, pa, ph = _client()
    try:
        resp = client.post(
            "/api/delegate",
            json=_body(webhook_url=webhook_url),
            headers={"x-hermes-secret": SECRET},
        )
        assert resp.status_code == 400
        assert scheduled == []
    finally:
        _restore(pa, ph)
        client.close()
