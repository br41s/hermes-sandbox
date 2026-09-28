"""Fork: a bl_site JWT the site stops accepting is replaced, not cached until restart.

Before tools/bl_site_client.py the login cache had no eviction, so an expired token
(or one invalidated by a panel-password rotation) failed every later call to that
site from the long-lived gateway with a 401.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

import tools.bl_site_client as client
import tools.bl_site_product_tool as product
import tools.bl_site_publish_tool as publish

SITE = "https://client.example"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _http_error(url, code, body):
    return urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(json.dumps(body).encode()))


@pytest.fixture
def site(monkeypatch):
    """A fake site: login hands out tok1, tok2, ...; ``accepted`` is the set of live tokens."""
    client._jwt_cache.clear()
    client._relogin.clear()
    monkeypatch.setattr(publish, "_get_automation_key", lambda: None)
    state = {"logins": 0, "accepted": set(), "calls": [], "login_status": 200, "password": "pw"}

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        if url.endswith("/api/auth/login"):
            sent = json.loads(req.data)["password"]
            if state["login_status"] != 200 or sent != state["password"]:
                raise _http_error(url, 401, {"error": "bad password"})
            state["logins"] += 1
            token = f"tok{state['logins']}"
            return _Resp(json.dumps({"token": token}).encode())
        auth = req.get_header("Authorization") or ""
        state["calls"].append((req.get_method(), url, auth))
        if auth.removeprefix("Bearer ") not in state["accepted"]:
            raise _http_error(url, 401, {"error": "invalid token"})
        return _Resp(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    yield state
    client._jwt_cache.clear()
    client._relogin.clear()


def test_stale_token_is_replaced_and_the_request_retried_once(site):
    site["accepted"] = {"tok1"}
    token = publish._get_jwt(SITE, "pw")
    assert product._request("GET", f"{SITE}/api/products/queue", token) == {"ok": True}

    site["accepted"] = {"tok2"}  # the site rotated: tok1 is dead
    result = product._request("PUT", f"{SITE}/api/products/A1", token, {"x": 1})

    assert result == {"ok": True}
    assert site["logins"] == 2
    assert site["calls"][-2:] == [
        ("PUT", f"{SITE}/api/products/A1", "Bearer tok1"),
        ("PUT", f"{SITE}/api/products/A1", "Bearer tok2"),
    ]
    # Later callers get the fresh token straight from the cache, without another login.
    assert publish._get_jwt(SITE, "pw") == "tok2" and site["logins"] == 2


def test_a_401_after_relogin_raises_instead_of_looping(site):
    site["accepted"] = set()  # nothing is ever accepted
    token = publish._get_jwt(SITE, "pw")

    with pytest.raises(RuntimeError, match="HTTP 401"):
        product._request("GET", f"{SITE}/api/products/queue", token)

    assert site["logins"] == 2
    assert len(site["calls"]) == 2


def test_a_second_caller_reuses_a_relogin_that_already_happened(site):
    site["accepted"] = {"tok1"}
    stale = publish._get_jwt(SITE, "pw")
    site["accepted"] = {"tok2"}
    product._request("GET", f"{SITE}/api/products/queue", stale)
    assert site["logins"] == 2

    # A caller still holding tok1 retries with the cached tok2 instead of logging in again.
    assert product._request("GET", f"{SITE}/api/products/queue", stale) == {"ok": True}
    assert site["logins"] == 2


def test_a_rotated_panel_password_is_used_by_the_relogin(site):
    """The motivating case: the client changes their panel password, which kills the
    token AND the password the first login used. The profile's .env now carries the
    new one, and the re-login must send that, not the one captured at first login."""
    site["accepted"] = {"tok1"}
    token = publish._get_jwt(SITE, "pw")

    site["password"], site["accepted"] = "pw2", {"tok2"}
    token_seen_by_a_later_call = publish._get_jwt(SITE, "pw2")  # cache hit: still tok1
    assert token_seen_by_a_later_call == token == "tok1"

    assert product._request("GET", f"{SITE}/api/products/queue", token) == {"ok": True}
    assert site["logins"] == 2


def test_a_post_is_reauthenticated_but_never_replayed(site):
    """A 401 on a create or publish must not send it twice: it raises, and only the
    NEXT call carries the fresh token."""
    site["accepted"] = {"tok1"}
    token = publish._get_jwt(SITE, "pw")
    site["accepted"] = {"tok2"}

    with pytest.raises(RuntimeError, match="HTTP 401"):
        product._request("POST", f"{SITE}/api/redirects", token, {"x": 1})
    assert [c[0] for c in site["calls"]] == ["POST"]

    fresh = publish._get_jwt(SITE, "pw")
    assert fresh == "tok2" and site["logins"] == 2
    assert product._request("POST", f"{SITE}/api/redirects", fresh, {"x": 1}) == {"ok": True}


def test_a_failed_login_is_not_retried(site):
    site["login_status"] = 401
    with pytest.raises(RuntimeError, match="HTTP 401 from .*/api/auth/login"):
        publish._get_jwt(SITE, "wrong")
    assert client._jwt_cache == {}


def test_a_401_from_an_unknown_site_is_not_retried(site):
    site["accepted"] = {"tok1"}
    publish._get_jwt(SITE, "pw")

    with pytest.raises(RuntimeError, match="HTTP 401"):
        product._request("GET", "https://client.example.evil/api/x", "revoked")
    assert site["logins"] == 1


def test_each_tool_keeps_its_own_error_wording(monkeypatch):
    """The shared client must not change what the agent reads when a call fails."""
    import tools.bl_site_redirect_tool as redirect

    def unreachable(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(client.urllib.request, "urlopen", unreachable)
    with pytest.raises(RuntimeError, match=r"^No se pudo contactar https://x/api: connection refused$"):
        redirect._request("GET", "https://x/api", "t")
    with pytest.raises(urllib.error.URLError):
        product._request("GET", "https://x/api", "t")
    assert publish._jwt_cache is client._jwt_cache
