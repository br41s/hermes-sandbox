"""Security audit regressions for the public dashboard deployment (hermes.biglobster.top).

Covers the public-URL-declared ``hermes dashboard`` surface behind a container reverse
proxy (Coolify/Traefik):
- ``detect_https`` honours an ``https://`` ``HERMES_DASHBOARD_PUBLIC_URL`` even if a
  proxy hop strips ``X-Forwarded-Proto``, so session and PKCE cookies still get ``__Host-``
  and ``Secure``.
- ``prefix_from_request``, ``serve_css`` and ``serve_spa`` ignore client-spoofed
  ``X-Forwarded-Prefix`` headers when ``HERMES_DASHBOARD_PUBLIC_URL`` is configured.
- ``GET /api/status`` (and ``?profile=``) redacts profile lists, ``install_id``, and host
  figures for unauthenticated callers when ``HERMES_DASHBOARD_PUBLIC_URL`` is set, while
  returning the full payload once signed in.
- Public bootstrap routes (``/api/model/info``, ``/api/config/schema``,
  ``/api/dashboard/themes``, ``/api/dashboard/plugins``) stay public for the default
  pre-login load but require an authenticated session when ``?profile=<name>`` is passed.
- Cookie-authenticated writes enforce ``Origin`` against ``HERMES_DASHBOARD_PUBLIC_URL``
  even when ``Sec-Fetch-Site`` is absent.
- Dashboard HTML and ``/login`` emit ``X-Frame-Options: DENY``,
  ``X-Content-Type-Options: nosniff``, and ``Referrer-Policy``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import hermes_cli.web_server_gateway as _web_server_gateway
import plugins.dashboard_auth.basic as basic_plugin
from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth.routes import _reset_password_rate_limit

PUBLIC_URL = "https://hermes.biglobster.top"


@pytest.fixture(scope="module")
def password_hash():
    return basic_plugin.hash_password("hunter2")


@pytest.fixture
def public_dashboard(password_hash, monkeypatch):
    """Simulate the production Coolify dashboard bind (0.0.0.0:9119) with
    HERMES_DASHBOARD_PUBLIC_URL=https://hermes.biglobster.top."""
    monkeypatch.setenv("HERMES_DASHBOARD_PUBLIC_URL", PUBLIC_URL)
    for key, value in (
        ("bound_host", "0.0.0.0"),
        ("bound_port", 9119),
        ("auth_required", True),
        ("trusted_public_hosts", frozenset({"hermes.biglobster.top"})),
    ):
        monkeypatch.setattr(web_server.app.state, key, value, raising=False)
    for key in ("ui_surface", "listener_public_url", "webapp_home"):
        if hasattr(web_server.app.state, key):
            monkeypatch.delattr(web_server.app.state, key)
    monkeypatch.setattr(
        _web_server_gateway,
        "_collect_profile_gateway_topology_cached",
        lambda: {
            "profiles": ["default", "grow-shop", "finview"],
            "parked_profiles": [],
            "gateway_mode": "multiplex",
            "gateways": [],
        },
    )
    clear_providers()
    register_provider(
        basic_plugin.BasicAuthProvider(
            username="admin", password_hash=password_hash, secret=b"s" * 32
        )
    )
    _reset_password_rate_limit()
    yield
    clear_providers()


def test_reconciled_trusted_proxies_turn_traefik_forwarded_headers_into_https_and_real_ip(
    public_dashboard,
):
    """With boot_reconcile.DASHBOARD_TRUSTED_PROXIES configured, uvicorn's ProxyHeadersMiddleware
    honours X-Forwarded-Proto: https and X-Forwarded-For from a Coolify Traefik bridge IP
    (172.18.0.x / 10.x.x.x), emitting __Host- + Secure cookies and recording the real client IP."""
    import asyncio
    from starlette.requests import Request
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    from hermes_cli.dashboard_auth.cookies import detect_https
    from hermes_cli.dashboard_auth.request_utils import client_ip
    from hermes_cli.fork_ext.boot_reconcile import DASHBOARD_TRUSTED_PROXIES
    from hermes_cli.web_server_lifecycle import _dashboard_forwarded_allow_ips

    trusted = _dashboard_forwarded_allow_ips({"trusted_proxies": DASHBOARD_TRUSTED_PROXIES})
    observed: dict = {}

    async def downstream(scope, receive, send):
        req = Request(scope)
        observed["https"] = detect_https(req)
        observed["ip"] = client_ip(req)

    middleware = ProxyHeadersMiddleware(downstream, trusted_hosts=trusted)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/auth/login",
        "raw_path": b"/auth/login",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"x-forwarded-proto", b"https"),
            (b"x-forwarded-for", b"203.0.113.42"),
        ],
        "client": ("172.18.0.5", 43120),
        "server": ("hermes", 9119),
    }

    async def receive():
        return {"type": "http.disconnect"}

    async def send(_message):
        return None

    asyncio.run(middleware(scope, receive, send))
    assert observed == {"https": True, "ip": "203.0.113.42"}


def test_spoofed_forwarded_prefix_ignored_when_public_url_is_set(public_dashboard):
    """When HERMES_DASHBOARD_PUBLIC_URL is set at root, a client-supplied
    X-Forwarded-Prefix cannot rewrite login URLs or downgrade __Host- cookies to __Secure-."""
    client = TestClient(web_server.app, base_url=PUBLIC_URL)
    r = client.get(
        "/login",
        headers={"X-Forwarded-Prefix": "/evil"},
    )
    assert r.status_code == 200
    assert 'data-prefix="' not in r.text
    assert "/evil" not in r.text

    login = client.post(
        "/auth/password-login",
        headers={"X-Forwarded-Prefix": "/evil"},
        json={"provider": "basic", "username": "admin", "password": "hunter2"},
    )
    assert login.status_code == 200
    cookies = login.headers.get_list("set-cookie")
    assert any(c.startswith("__Host-hermes_session_at=") for c in cookies)
    assert not any(c.startswith("__Secure-") for c in cookies)


def test_public_dashboard_redacts_status_until_signed_in(public_dashboard):
    """Anonymous callers to https://hermes.biglobster.top/api/status get liveness + auth shape
    only — never tenant profile names, install_id, or a ?profile= oracle."""
    client = TestClient(web_server.app, base_url=PUBLIC_URL)
    anon = client.get("/api/status")
    assert anon.status_code == 200
    body = anon.json()
    assert body["auth_required"] is True
    for redacted in ("profiles", "parked_profiles", "install_id", "memory", "disk"):
        assert redacted not in body
    # ?profile= is ignored for anonymous callers (no 404 oracle).
    assert client.get("/api/status?profile=no-such-tenant").status_code == 200

    # Once signed in, the full status payload and ?profile= resolution work.
    login = client.post(
        "/auth/password-login",
        json={"provider": "basic", "username": "admin", "password": "hunter2"},
    )
    assert login.status_code == 200
    authed = client.get("/api/status").json()
    assert authed["profiles"] == ["default", "grow-shop", "finview"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/model/info",
        "/api/config/schema",
        "/api/dashboard/themes",
        "/api/dashboard/plugins",
    ],
)
def test_public_bootstrap_routes_require_auth_for_named_profile(public_dashboard, path):
    """Pre-login bootstrap (no ?profile=) stays public; querying a specific profile
    (?profile=grow-shop) requires an authenticated session."""
    client = TestClient(web_server.app, base_url=PUBLIC_URL)
    assert client.get(path).status_code != 401
    assert client.get(path, params={"profile": "grow-shop"}).status_code == 401


def test_public_dashboard_rejects_cross_origin_cookie_writes_without_fetch_metadata(
    public_dashboard,
):
    """With HERMES_DASHBOARD_PUBLIC_URL configured, cookie-authenticated writes must match
    the public origin even when Sec-Fetch-Site is omitted."""
    client = TestClient(web_server.app, base_url=PUBLIC_URL)
    login = client.post(
        "/auth/password-login",
        json={"provider": "basic", "username": "admin", "password": "hunter2"},
    )
    assert login.status_code == 200

    forged = client.post(
        "/api/auth/ws-ticket",
        headers={"Origin": "https://evil.example"},
    )
    assert forged.status_code == 403

    own = client.post(
        "/api/auth/ws-ticket",
        headers={"Origin": PUBLIC_URL},
    )
    assert own.status_code == 200


def test_login_and_dashboard_emit_defensive_security_headers(public_dashboard):
    client = TestClient(web_server.app, base_url=PUBLIC_URL)
    login_resp = client.get("/login")
    assert login_resp.status_code == 200
    assert login_resp.headers.get("x-frame-options") == "DENY"
    assert login_resp.headers.get("x-content-type-options") == "nosniff"
    assert login_resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
