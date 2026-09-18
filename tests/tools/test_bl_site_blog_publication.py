"""What the blog tool does to a client's *live* site, pinned.

This is the one rental write path with no human in the loop.
``bl_site_product`` saves drafts, ``bl_site_redirect`` saves pending rows —
both wait for someone. ``create_blog_post`` hardcodes ``"status":
"published"`` and the article is on the client's public site the moment the
call returns. That is deliberate (it is why ``gap-hunter``'s prompt says its
rules are not optional, since nothing is checked afterwards), but it means a
bad post is a client-visible incident rather than a draft someone catches.

CLAUDE.md asserted the opposite until 2026-09-17 — that rented agents' posts
saved as drafts — and nothing contradicted it, because this action had no
tests at all while the two *safer* tools had fifty between them. Coverage was
inversely proportional to blast radius. These lock the real behaviour so the
docs can be checked against something.

The point is not that the tool can POST. It is *which* publication decisions
it makes on its own.
"""

import json

import pytest

from tools import bl_site_publish_tool as mod

SITE = "https://cliente.example"


@pytest.fixture(autouse=True)
def _credentials(monkeypatch):
    monkeypatch.setattr(mod, "_get_site_credentials", lambda: (SITE, "panel-pw"))
    monkeypatch.setattr(mod, "_get_jwt", lambda _u, _p: "jwt-token")


def wire(monkeypatch, response=None):
    """Capture what the tool sends, and control what comes back."""
    sent = {}

    def _http_json(method, url, body=None, token=None):
        sent["method"], sent["url"], sent["body"] = method, url, body
        return response if response is not None else {}

    monkeypatch.setattr(mod, "_http_json", _http_json)
    return sent


# ── create_blog_post: live immediately ───────────────────────────────────────

def test_a_new_post_goes_live_immediately(monkeypatch):
    """The deliberate exception. If this ever becomes a draft, say so in the
    docs and in gap-hunter's prompt — its rules are written around nothing
    checking the post afterwards."""
    sent = wire(monkeypatch, {"id": 7, "slug": "una-entrada"})

    mod.bl_site_publish(
        action="create_blog_post", title="Una entrada", content="Cuerpo."
    )

    assert sent["method"] == "POST"
    assert sent["url"] == f"{SITE}/api/blog/posts"
    assert sent["body"]["status"] == "published", (
        "create_blog_post writes to the client's public site unattended"
    )


def test_the_agent_is_told_the_post_is_already_live(monkeypatch):
    """An agent that believes it created a draft will not treat the call with
    the care a live publication needs."""
    wire(monkeypatch, {"id": 7, "slug": "una-entrada"})

    result = json.loads(
        mod.bl_site_publish(
            action="create_blog_post", title="Una entrada", content="Cuerpo."
        )
    )

    assert result["status"] == "published"
    assert "live" in result["note"].lower()


def test_publication_status_is_not_the_callers_to_choose() -> None:
    """Stronger than a hardcoded value: there is no argument to pass.

    Neither the model nor a caller can request a draft, so the behaviour above
    cannot be varied per call — which is exactly why it has to be documented
    accurately rather than treated as a default someone can override.
    """
    import inspect

    params = inspect.signature(mod.bl_site_publish).parameters
    assert "status" not in params, (
        "a status argument would make publication caller-controlled; today it "
        "is unconditional and the docs say so"
    )
    assert "status" not in mod.BL_SITE_PUBLISH_SCHEMA["parameters"]["properties"]


def test_a_post_needs_a_title_and_a_body_before_anything_is_sent(monkeypatch):
    """The only pre-publication check that exists on this path."""
    sent = wire(monkeypatch)

    assert "title" in mod.bl_site_publish(action="create_blog_post", content="Cuerpo.")
    assert "content" in mod.bl_site_publish(action="create_blog_post", title="Una entrada")
    assert sent == {}, "nothing may reach the site before the check passes"


# ── update_blog_post: never moves a post across the line ──────────────────────

def test_editing_a_post_never_changes_its_publication_status(monkeypatch):
    """CEO decision: an infographic must never pull a live article down for
    re-review. The API COALESCEs omitted fields, so *omitting* status is what
    keeps a published post published and a draft a draft — sending one would
    silently do the opposite of what this action is for."""
    sent = wire(monkeypatch, {"success": True, "id": 7, "status": "published"})

    mod.bl_site_publish(
        action="update_blog_post", post_id=7, content="Cuerpo con infografía."
    )

    assert sent["method"] == "PUT"
    assert "status" not in sent["body"], (
        "update_blog_post must never send status — see the COALESCE note"
    )
    assert sent["body"] == {"content": "Cuerpo con infografía."}


def test_editing_sends_only_the_fields_the_caller_named(monkeypatch):
    sent = wire(monkeypatch, {"success": True, "id": 7})

    mod.bl_site_publish(
        action="update_blog_post", post_id=7, title="Nuevo título", badges=["a", "b"]
    )

    assert sent["body"] == {"title": "Nuevo título", "badges": ["a", "b"]}


def test_an_edit_with_nothing_to_change_never_reaches_the_site(monkeypatch):
    sent = wire(monkeypatch)

    assert "at least one field" in mod.bl_site_publish(
        action="update_blog_post", post_id=7
    )
    assert sent == {}


# ── the schema is the only guard the model ever sees ─────────────────────────

def test_the_schema_warns_that_a_new_post_goes_live(monkeypatch):
    """Everything downstream of the POST is prose in a tool description. If a
    description is trimmed, the only in-repo warning disappears silently and
    the tool behaves exactly the same."""
    description = mod.BL_SITE_PUBLISH_SCHEMA["description"].lower()

    assert "create_blog_post" in description
    assert "immediately" in description or "inmediatamente" in description, (
        "the schema must tell the model the post is live on return"
    )
