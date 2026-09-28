"""JSON-over-HTTP client shared by the bl_site_* tools (fork-owned).

Every rented agent reaches a client's bl-site-package site through one of these tools,
with that profile's own panel password. They used to carry three copies of the request
helper and one login cache with no eviction: a JWT that expired or was invalidated by a
password rotation stayed cached in the long-lived gateway process, so every later write
to that site failed with a 401 until the next restart.

``request_json`` is the one request path. On a 401 to an authenticated request it drops
the cached token, logs in again once through the same login function that produced it,
and retries that request once. A 401 means the site refused the request, so a write
retried this way cannot apply twice.

Each tool keeps its own error wording: the 422 ``blockers`` text is passed through as
instructions for the agent, with a tool-specific prefix, exactly as before.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable, Optional

# site_url -> JWT. Module-level so every tool shares one login per site and process.
_jwt_cache: dict[str, str] = {}
# site_url -> the login that produced the cached token, for a transparent re-login.
_relogin: dict[str, Callable[[], str]] = {}


def cached_token(site_url: str, login: Callable[[], str]) -> str:
    """The cached JWT for ``site_url``, logging in through ``login`` when there is none."""
    token = _jwt_cache.get(site_url)
    if token:
        return token
    token = login()
    _jwt_cache[site_url] = token
    _relogin[site_url] = login
    return token


def _site_of(url: str) -> Optional[str]:
    return next((site for site in _jwt_cache if url == site or url.startswith(site + "/")), None)


def _fresh_token(url: str, stale: str) -> Optional[str]:
    """A token to retry ``url`` with after ``stale`` got a 401, or None when there is none."""
    site = _site_of(url)
    if site is None:
        return None
    current = _jwt_cache.get(site)
    if current and current != stale:
        return current  # another call already re-logged in
    _jwt_cache.pop(site, None)
    login = _relogin.get(site)
    if login is None:
        return None
    token = login()
    _jwt_cache[site] = token
    return token


def request_json(
    method: str,
    url: str,
    *,
    token: Optional[str] = None,
    body: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: float = 15,
    refusal_prefix: Optional[str] = None,
    unreachable_prefix: Optional[str] = None,
    raw_errors: bool = False,
    _retried: bool = False,
) -> dict:
    """``method url`` with a JSON body, returning the parsed JSON response.

    Errors raise ``RuntimeError`` with ``HTTP <code> from <url>: <detail>``, where
    ``detail`` is the body's ``error`` field unless ``raw_errors`` (then the raw body).
    ``refusal_prefix``: a 422 listing ``blockers`` raises that prefix plus the blockers.
    ``unreachable_prefix``: a connection failure raises ``<prefix> <url>: <reason>``
    instead of propagating ``URLError``.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        if e.code == 401 and token and not _retried:
            fresh = _fresh_token(url, token)
            if fresh:
                return request_json(
                    method, url, token=fresh, body=body, headers=headers, timeout=timeout,
                    refusal_prefix=refusal_prefix, unreachable_prefix=unreachable_prefix,
                    raw_errors=raw_errors, _retried=True)
        if raw_errors:
            raise RuntimeError(f"HTTP {e.code} from {url}: {detail}") from e
        try:
            payload = json.loads(detail)
        except ValueError:
            raise RuntimeError(f"HTTP {e.code} from {url}: {detail}") from e
        if refusal_prefix and e.code == 422 and payload.get("blockers"):
            raise RuntimeError(refusal_prefix + "; ".join(payload["blockers"])) from e
        raise RuntimeError(f"HTTP {e.code} from {url}: {payload.get('error', detail)}") from e
    except urllib.error.URLError as e:
        if unreachable_prefix is None:
            raise
        raise RuntimeError(f"{unreachable_prefix} {url}: {e.reason}") from e
