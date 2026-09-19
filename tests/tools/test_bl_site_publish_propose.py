"""The write paths the Content Updater uses, and the guard they carry.

Everything else `bl_site_publish` does is additive — a post that did not
exist, an image that was not there. These two actions change prose that is
already live on a client's site, so what is tested here is mostly what the
tool REFUSES to do: publish a proposal, drop a fingerprint, or turn "someone
else edited this" into a silent overwrite.
"""

import json

import pytest

from tools import bl_site_publish_tool as publish


@pytest.fixture(autouse=True)
def site_credentials(monkeypatch):
    monkeypatch.setattr(
        publish, "_get_site_credentials", lambda: ("https://cliente.example", "panel-pw")
    )
    monkeypatch.setattr(publish, "_get_jwt", lambda url, pw: "jwt-token")
    publish._jwt_cache.clear()
    yield
    publish._jwt_cache.clear()


def _fake_http(monkeypatch, response=None, error=None):
    """Capture the call bl_site_publish makes instead of performing it."""
    calls = []

    def fake(method, url, body=None, token=None, headers=None):
        calls.append({"method": method, "url": url, "body": body})
        if error:
            raise RuntimeError(error)
        return response or {}

    monkeypatch.setattr(publish, "_http_json", fake)
    return calls


# ── get_post ────────────────────────────────────────────────────────────────


def test_get_post_returns_the_fingerprint(monkeypatch):
    # Without this the agent has nothing to send back as base_hash, and every
    # guard downstream is unreachable.
    _fake_http(
        monkeypatch,
        {"id": 7, "title": "T", "slug": "t", "status": "published",
         "content": "<p>x</p>", "content_hash": "abc123"},
    )
    result = json.loads(publish.bl_site_publish(action="get_post", post_id="7"))
    assert result["content_hash"] == "abc123"


# ── update_blog_post ────────────────────────────────────────────────────────


def test_update_sends_base_hash_when_given(monkeypatch):
    calls = _fake_http(monkeypatch, {"success": True, "id": 7})
    publish.bl_site_publish(
        action="update_blog_post", post_id="7", content="<p>nuevo</p>", base_hash="abc123"
    )
    assert calls[0]["body"]["base_hash"] == "abc123"


def test_update_without_base_hash_still_works(monkeypatch):
    # The panel, the infographic engineer and the maintenance agent all write
    # this way. Requiring the hash here would have broken them on upgrade day.
    calls = _fake_http(monkeypatch, {"success": True, "id": 7})
    publish.bl_site_publish(action="update_blog_post", post_id="7", content="<p>x</p>")
    assert "base_hash" not in calls[0]["body"]


def test_update_explains_a_conflict_instead_of_leaking_the_http_error(monkeypatch):
    _fake_http(monkeypatch, error="HTTP 409 from https://cliente.example/...: changed")
    result = publish.bl_site_publish(
        action="update_blog_post", post_id="7", content="<p>x</p>", base_hash="stale"
    )
    # The agent has to understand that re-sending is wrong, not just that
    # something failed — an agent that reads "error" retries.
    assert "NOT applied" in result
    assert "get_post again" in result
    assert "Never re-send the same edit" in result


def test_update_still_raises_on_an_unrelated_http_error(monkeypatch):
    # Only 409 has a recovery; a 500 must not be dressed up as one.
    _fake_http(monkeypatch, error="HTTP 500 from https://cliente.example/...: boom")
    result = publish.bl_site_publish(
        action="update_blog_post", post_id="7", content="<p>x</p>", base_hash="abc"
    )
    assert "500" in result
    assert "get_post again" not in result


# ── propose_edit ────────────────────────────────────────────────────────────


def test_propose_posts_to_the_proposal_endpoint(monkeypatch):
    calls = _fake_http(monkeypatch, {"id": 3, "status": "pending"})
    publish.bl_site_publish(
        action="propose_edit",
        post_id="7",
        content="<p>corregido</p>",
        base_hash="abc123",
        reason="El plazo ya pasó",
        evidence='[{"claim": "plazo", "source_url": "https://boe.es/x"}]',
    )
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/api/blog/posts/7/propose")
    assert calls[0]["body"]["reason"] == "El plazo ya pasó"
    assert calls[0]["body"]["base_hash"] == "abc123"


def test_propose_says_plainly_that_nothing_was_published(monkeypatch):
    # The agent writes its client-facing report from this string. If it reads
    # like a publish, the client is told their site changed when it did not.
    _fake_http(monkeypatch, {"id": 3})
    result = json.loads(
        publish.bl_site_publish(
            action="propose_edit", post_id="7", content="<p>x</p>", base_hash="abc"
        )
    )
    assert result["status"] == "pending"
    assert "NOT on the live site" in result["note"]


def test_propose_requires_base_hash(monkeypatch):
    _fake_http(monkeypatch, {"id": 3})
    result = publish.bl_site_publish(action="propose_edit", post_id="7", content="<p>x</p>")
    assert "base_hash" in result
    assert "error" in result.lower()


def test_propose_requires_something_to_change(monkeypatch):
    _fake_http(monkeypatch, {"id": 3})
    result = publish.bl_site_publish(action="propose_edit", post_id="7", base_hash="abc")
    assert "at least one" in result


def test_propose_on_an_outdated_site_forbids_falling_back_to_publishing(monkeypatch):
    # The dangerous failure: a site too old to hold proposals, and an agent
    # that "helpfully" publishes the change instead.
    _fake_http(monkeypatch, error="HTTP 404 from https://cliente.example/...: Not Found")
    result = publish.bl_site_publish(
        action="propose_edit", post_id="7", content="<p>x</p>", base_hash="abc"
    )
    assert "1.8.0" in result
    assert "Do NOT fall back" in result


def test_propose_reports_a_conflict_rather_than_saving(monkeypatch):
    _fake_http(monkeypatch, error="HTTP 409 from https://cliente.example/...: changed")
    result = publish.bl_site_publish(
        action="propose_edit", post_id="7", content="<p>x</p>", base_hash="stale"
    )
    assert "not saved" in result
    assert "get_post again" in result


# ── author attribution ──────────────────────────────────────────────────────


def test_update_sends_author(monkeypatch):
    # Without this the client's Blog -> Historial attributes the agent's edit
    # to the client themselves, which is worse than no attribution at all.
    calls = _fake_http(monkeypatch, {"success": True, "id": 7})
    publish.bl_site_publish(
        action="update_blog_post", post_id="7", content="<p>x</p>",
        base_hash="abc", author="content-updater",
    )
    assert calls[0]["body"]["author"] == "content-updater"


def test_update_omits_author_when_not_given(monkeypatch):
    # Existing callers (the infographic engineer, maintenance) send none, and
    # the site records the row unattributed rather than guessing.
    calls = _fake_http(monkeypatch, {"success": True, "id": 7})
    publish.bl_site_publish(action="update_blog_post", post_id="7", content="<p>x</p>")
    assert "author" not in calls[0]["body"]


# ── schema ──────────────────────────────────────────────────────────────────


def test_propose_edit_is_an_offered_action():
    assert "propose_edit" in publish.BL_SITE_PUBLISH_SCHEMA["parameters"]["properties"]["action"]["enum"]


def test_the_new_parameters_are_declared():
    props = publish.BL_SITE_PUBLISH_SCHEMA["parameters"]["properties"]
    for name in ("base_hash", "reason", "evidence", "author"):
        assert name in props, f"{name} is passed by the handler but not declared"


def test_the_registered_handler_forwards_the_new_parameters():
    # A parameter declared in the schema but dropped by the lambda is invisible
    # until an agent uses it in production and the guard silently does nothing.
    from tools.registry import registry

    handler = registry.get_entry("bl_site_publish").handler

    seen = {}
    original = publish.bl_site_publish

    def spy(**kwargs):
        seen.update(kwargs)
        return "{}"

    publish.bl_site_publish = spy
    try:
        handler({"action": "propose_edit", "base_hash": "h", "reason": "r",
                 "evidence": "[]", "author": "content-updater"})
    finally:
        publish.bl_site_publish = original

    assert seen.get("base_hash") == "h"
    assert seen.get("reason") == "r"
    assert seen.get("evidence") == "[]"
    assert seen.get("author") == "content-updater"
