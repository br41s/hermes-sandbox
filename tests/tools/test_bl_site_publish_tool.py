"""Tests for the shared login helper every bl_site_* tool imports.

The Turnstile-bypass header is the point: a client site with Turnstile
enabled on /login (see bl-site-package src/api/auth.js) rejects a bare
password login from rental automation. BL_SITE_AUTOMATION_KEY, when a
profile has one, must ride along as a header so that login keeps working
without weakening the panel password check on the server side.
"""

import json

import pytest

from tools import bl_site_publish_tool as publish


@pytest.fixture(autouse=True)
def clear_jwt_cache():
    publish._jwt_cache.clear()
    yield
    publish._jwt_cache.clear()


class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture_request(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        captured["url"] = req.full_url
        return _FakeResponse({"token": "jwt-token"})

    monkeypatch.setattr(publish.urllib.request, "urlopen", fake_urlopen)
    return captured


def test_login_sends_automation_key_header_when_configured(monkeypatch):
    monkeypatch.setattr(publish, "_get_automation_key", lambda: "rental-shared-secret")
    captured = _capture_request(monkeypatch)

    token = publish._get_jwt("https://cliente.example", "panel-password")

    assert token == "jwt-token"
    assert captured["headers"].get("X-automation-key") == "rental-shared-secret"


def test_login_omits_automation_key_header_when_not_configured(monkeypatch):
    monkeypatch.setattr(publish, "_get_automation_key", lambda: None)
    captured = _capture_request(monkeypatch)

    publish._get_jwt("https://cliente.example", "panel-password")

    assert "X-automation-key" not in captured["headers"]


def test_get_automation_key_reads_profile_env(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.get_env_value",
        lambda name: "the-secret" if name == "BL_SITE_AUTOMATION_KEY" else None,
    )
    assert publish._get_automation_key() == "the-secret"


def test_get_automation_key_is_none_when_unset(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.get_env_value", lambda name: None)
    assert publish._get_automation_key() is None
