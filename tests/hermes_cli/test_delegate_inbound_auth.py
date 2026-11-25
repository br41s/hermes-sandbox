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
from hermes_cli.fork_ext import web as fork_web
from tools import url_safety

SECRET = "test-delegate-secret"
PUBLIC_IP = "93.184.216.34"
_REAL_GETADDRINFO = url_safety._getaddrinfo


def _answer(ip, port=None):
    return [(0, 0, 0, "", (ip, port or 0))]


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Every hostname resolves to a public address; literals keep their own value."""
    def resolve(host, port=None):
        if url_safety._parse_ip(host) is not None:
            return _REAL_GETADDRINFO(host, port)
        return _answer(PUBLIC_IP, port)

    monkeypatch.setattr(url_safety, "_getaddrinfo", resolve)


def _client():
    fork_web._reset_delegate_rate_limit()
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
    monkeypatch.setattr(fork_web, "_delegate_background",
                        lambda *a, **kw: scheduled.append(a))
    client, pa, ph = _client()
    try:
        # A caller presenting a secret learns the engine is unconfigured (retryable)...
        resp = client.post("/api/delegate", json=_body(), headers={"x-hermes-secret": "anything"})
        assert resp.status_code == 503
        # ...an anonymous one only that it is unauthorized, before the body is even validated.
        assert client.post("/api/delegate", json=_body()).status_code == 401
        assert client.post("/api/delegate", json={}).status_code == 401
        assert scheduled == []
    finally:
        _restore(pa, ph)
        client.close()


def test_missing_header_401(monkeypatch):
    monkeypatch.setenv("HERMES_CALLBACK_SECRET", SECRET)
    scheduled = []
    monkeypatch.setattr(fork_web, "_delegate_background",
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
    monkeypatch.setattr(fork_web, "_delegate_background",
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

    monkeypatch.setattr(fork_web, "_delegate_background", _fake_background)
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
        "https://0.0.0.0/steal",  # unspecified IP
        "https://[::ffff:10.0.0.5]/steal",  # IPv4-mapped private IPv6
        "https://[::ffff:169.254.169.254]/latest/meta-data",  # IPv4-mapped cloud metadata
        "https://[::ffff:0:10.0.0.5]/steal",  # IPv4-translated private IPv6
        "https://100.64.0.1/steal",  # CGNAT (Tailscale/internal)
        "https://metadata.google.internal/computeMetadata/v1/",  # cloud metadata hostname
        "https://127.0.0.1.nip.io/steal",  # wildcard DNS rebinding
        "https://10.0.0.1.sslip.io/steal",  # wildcard DNS rebinding
        "https://user:pass@biglobster.top/api/hermes-callback",  # embedded credentials
        "https://127.0.0.1\\@biglobster.top/steal",  # two parsers, two hosts
        "https://[not-an-ip/steal",  # unparseable: a 400, never a 500
    ],
)
def test_unsafe_webhook_url_rejected(monkeypatch, webhook_url):
    monkeypatch.setenv("HERMES_CALLBACK_SECRET", SECRET)
    scheduled = []
    monkeypatch.setattr(fork_web, "_delegate_background",
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


@pytest.mark.parametrize(
    "resolved_ip", ["172.18.0.5", "::ffff:10.0.0.5", "::ffff:0:10.0.0.5", "100.64.0.1", "169.254.169.254"])
@pytest.mark.parametrize("allow_private_urls", [False, True])
def test_webhook_url_resolving_to_private_ip_rejected(monkeypatch, resolved_ip, allow_private_urls):
    """The agent's ``allow_private_urls`` opt-out never widens where an API caller may aim results."""
    monkeypatch.setenv("HERMES_CALLBACK_SECRET", SECRET)
    monkeypatch.setattr(url_safety, "_global_allow_private_urls", lambda: allow_private_urls)
    monkeypatch.setattr(url_safety, "_getaddrinfo",
                        lambda _host, port=None: _answer(PUBLIC_IP) + _answer(resolved_ip))
    scheduled = []
    monkeypatch.setattr(fork_web, "_delegate_background",
                        lambda *a, **kw: scheduled.append(a))
    client, pa, ph = _client()
    try:
        resp = client.post(
            "/api/delegate",
            json=_body(webhook_url="https://rebind.attacker.example/hook"),
            headers={"x-hermes-secret": SECRET},
        )
        assert resp.status_code == 400
        assert "loopback/private/link-local" in resp.json()["detail"]
        assert scheduled == []
    finally:
        _restore(pa, ph)
        client.close()


@pytest.mark.parametrize("error", [OSError("no such host"), UnicodeError("label too long")])
def test_webhook_url_that_does_not_resolve_is_rejected(monkeypatch, error):
    """A host nobody could vet is refused, not accepted."""
    import asyncio

    def fail(_host, port=None):
        raise error

    monkeypatch.setattr(url_safety, "_getaddrinfo", fail)
    reason = asyncio.run(fork_web._validate_delegate_webhook_url("https://gone.example/hook"))
    assert reason == "webhook_url host does not resolve"


def test_webhook_url_dns_resolution_runs_off_event_loop(monkeypatch):
    import asyncio
    import threading

    resolver_threads = []

    def resolve(_host, port=None):
        resolver_threads.append(threading.get_ident())
        return _answer(PUBLIC_IP)

    monkeypatch.setattr(url_safety, "_getaddrinfo", resolve)

    async def probe():
        reason = await fork_web._validate_delegate_webhook_url("https://biglobster.top/api/hermes-callback")
        return reason, threading.get_ident()

    reason, loop_thread = asyncio.run(probe())
    assert reason is None
    assert resolver_threads and loop_thread not in resolver_threads


def test_callback_host_rebound_to_private_address_is_never_dialed(monkeypatch):
    """The host is vetted when the task is accepted; the DNS answer may change before delivery."""
    import asyncio
    import socket
    from hermes_cli import delegate_core

    monkeypatch.setenv("HERMES_CALLBACK_SECRET", SECRET)
    monkeypatch.setattr(url_safety, "_global_allow_private_urls", lambda: False)
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(delegate_core, "run_delegate_agent",
                        lambda task_id, prompt: {"final_response": "ok"})
    resolved = []

    def rebound(host, port=None):
        resolved.append(host)
        return _answer("127.0.0.1", port)

    monkeypatch.setattr(url_safety, "_getaddrinfo", rebound)
    with socket.create_server(("127.0.0.1", 0)) as internal:
        internal.settimeout(0.2)
        url = f"https://rebind.attacker.example:{internal.getsockname()[1]}/hook"
        asyncio.run(fork_web._delegate_background("t-rebind", "hi", url, None))
        assert resolved == ["rebind.attacker.example"]
        with pytest.raises(TimeoutError):
            internal.accept()


def test_delegate_failed_auth_rate_limited(monkeypatch):
    monkeypatch.setenv("HERMES_CALLBACK_SECRET", SECRET)
    client, pa, ph = _client()
    try:
        for _ in range(fork_web._DELEGATE_AUTH_MAX_FAILURES):
            resp = client.post(
                "/api/delegate",
                json=_body(),
                headers={"x-hermes-secret": "wrong"},
            )
            assert resp.status_code == 401
        throttled = client.post(
            "/api/delegate",
            json=_body(),
            headers={"x-hermes-secret": SECRET},
        )
        assert throttled.status_code == 429
    finally:
        fork_web._reset_delegate_rate_limit()
        _restore(pa, ph)
        client.close()


def test_delegate_callback_withholds_raw_secret_from_untrusted_origin(monkeypatch):
    """BigLobster's own callback receives x-hermes-secret + HMAC; a third-party/tenant
    webhook_url receives only the HMAC signature, never the master HERMES_CALLBACK_SECRET."""
    import asyncio
    from hermes_cli import delegate_core

    monkeypatch.setenv("HERMES_CALLBACK_SECRET", SECRET)
    monkeypatch.setenv("HERMES_CALLBACK_URL", "https://biglobster.top/api/hermes-callback")
    monkeypatch.setattr(
        delegate_core, "run_delegate_agent",
        lambda task_id, prompt: {"final_response": "ok"},
    )
    deliveries = []

    class _FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, *, content=None, headers=None, **kw):
            deliveries.append({"url": url, "headers": dict(headers or {}), "kwargs": self.kwargs})

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    asyncio.run(fork_web._delegate_background(
        "t-own", "hi", "https://biglobster.top/api/hermes-callback", None
    ))
    asyncio.run(fork_web._delegate_background(
        "t-ext", "hi", "https://customer.example.com/webhook", None
    ))

    own, ext = deliveries
    assert own["kwargs"].get("follow_redirects") is False
    assert own["headers"].get("x-hermes-secret") == SECRET
    assert own["headers"].get("x-hermes-signature", "").startswith("sha256=")
    assert "x-hermes-secret" not in ext["headers"]
    assert ext["headers"].get("x-hermes-signature", "").startswith("sha256=")
    assert ext["headers"].get("x-hermes-timestamp")
