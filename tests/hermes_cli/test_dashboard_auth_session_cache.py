"""Regression tests for the verified-session cache in ``gated_auth_middleware``.

The gate verifies the OAuth session on every gated request. ``verify_session``
does a synchronous JWKS fetch + RS256 verify; on a single uvicorn worker that
serialised every ``/api/*`` call the SPA makes and, on the 5-min JWKS refresh,
froze the whole event loop. The middleware now caches a verified session by
access token until the token's own ``exp`` and offloads the cold verify to a
threadpool.

These tests pin the cache behaviour so a future refactor can't silently
reintroduce per-request re-verification (the perf regression) or — worse —
start serving expired tokens from the cache (a security regression).
"""
from __future__ import annotations

import asyncio
import time
import types

import pytest

from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth.base import (
    DashboardAuthProvider,
    LoginStart,
    ProviderError,
    Session,
)
from hermes_cli.dashboard_auth.cookies import SESSION_AT_COOKIE
from hermes_cli.dashboard_auth.middleware import (
    _VERIFIED_CACHE,
    _VERIFIED_CACHE_MAX,
    gated_auth_middleware,
)


def _session(exp: int) -> Session:
    return Session(
        user_id="u1", email="", display_name="", org_id="",
        provider="counting", expires_at=exp,
        access_token="tok", refresh_token="",
    )


class _CountingProvider(DashboardAuthProvider):
    """Records how many times ``verify_session`` actually runs."""

    name = "counting"
    display_name = "Counting (test)"

    def __init__(self, session: Session | None, *, raises: bool = False) -> None:
        self._session = session
        self._raises = raises
        self.calls = 0

    def start_login(self, *, redirect_uri: str) -> LoginStart:  # pragma: no cover
        raise NotImplementedError

    def complete_login(self, *, code, state, code_verifier, redirect_uri):  # pragma: no cover
        raise NotImplementedError

    def refresh_session(self, *, refresh_token):  # pragma: no cover
        raise NotImplementedError

    def verify_session(self, *, access_token: str):
        self.calls += 1
        if self._raises:
            raise ProviderError("jwks unreachable")
        return self._session

    def revoke_session(self, *, refresh_token) -> None:  # pragma: no cover
        return None


def _request(token: str | None, path: str = "/api/sessions"):
    headers = []
    if token is not None:
        headers.append((b"cookie", f"{SESSION_AT_COOKIE}={token}".encode()))
    app = types.SimpleNamespace(state=types.SimpleNamespace(auth_required=True))
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"",
        "headers": headers,
        "client": ("1.2.3.4", 1234),
        "app": app,
    }
    from starlette.requests import Request
    return Request(scope)


class _SentinelResponse(str):
    """String sentinel that also tolerates ``response.set_cookie(...)``.

    ``gated_auth_middleware`` tags the ``call_next`` response with a
    provider-hint cookie when the session came from a fresh verify rather
    than a request cookie; a bare string return breaks that with
    ``AttributeError``. Subclassing ``str`` keeps every existing
    ``out == "PASS:..."`` assertion working unchanged.
    """

    def set_cookie(self, *_a, **_kw):
        pass


async def _call_next(request):
    # Sentinel that proves the request was let through and which session
    # got attached.
    return _SentinelResponse(f"PASS:{getattr(request.state, 'session').user_id}")


@pytest.fixture(autouse=True)
def _clean():
    clear_providers()
    _VERIFIED_CACHE.clear()
    yield
    clear_providers()
    _VERIFIED_CACHE.clear()


def test_repeat_token_is_served_from_cache_without_reverifying():
    prov = _CountingProvider(_session(int(time.time()) + 300))
    register_provider(prov)

    out1 = asyncio.run(gated_auth_middleware(_request("tokA"), _call_next))
    assert out1 == "PASS:u1"
    assert prov.calls == 1  # cold verify

    # Second request, same token: must hit the cache, not re-verify.
    out2 = asyncio.run(gated_auth_middleware(_request("tokA"), _call_next))
    assert out2 == "PASS:u1"
    assert prov.calls == 1, "repeat token should be served from cache"


def test_distinct_tokens_each_verify_once():
    prov = _CountingProvider(_session(int(time.time()) + 300))
    register_provider(prov)

    asyncio.run(gated_auth_middleware(_request("tokA"), _call_next))
    asyncio.run(gated_auth_middleware(_request("tokB"), _call_next))
    assert prov.calls == 2


def test_expired_cached_session_is_not_served():
    # Provider hands back an already-expired session; the cache stores it but
    # must never serve it on the next read — re-verification happens instead.
    prov = _CountingProvider(_session(int(time.time()) - 5))
    register_provider(prov)

    asyncio.run(gated_auth_middleware(_request("tokC"), _call_next))
    assert prov.calls == 1
    asyncio.run(gated_auth_middleware(_request("tokC"), _call_next))
    assert prov.calls == 2, "expired entry must not be served from cache"


def test_unreachable_provider_returns_503_and_does_not_cache():
    prov = _CountingProvider(None, raises=True)
    register_provider(prov)

    resp = asyncio.run(gated_auth_middleware(_request("tokD"), _call_next))
    assert getattr(resp, "status_code", None) == 503
    assert "tokD" not in _VERIFIED_CACHE


def test_cache_size_is_bounded():
    prov = _CountingProvider(_session(int(time.time()) + 300))
    register_provider(prov)

    for i in range(_VERIFIED_CACHE_MAX + 5):
        asyncio.run(gated_auth_middleware(_request(f"t{i}"), _call_next))
    assert len(_VERIFIED_CACHE) <= _VERIFIED_CACHE_MAX
