"""Fork-owned dashboard routes, mounted on the web_server app by one include.

These routes used to be defined inline in ``hermes_cli/web_server.py``. That
file is upstream's, and upstream split it into ``hermes_cli/web_routers/``, so
every fork route there was a merge conflict waiting to happen. Keeping them
on this router leaves web_server.py with a two-line include instead.

``web_server`` includes this router right after the memory-OAuth router, i.e.
before every upstream route, plugin router and the SPA catch-all, so nothing
can shadow these paths.

This module must not import ``hermes_cli.web_server`` at module level:
web_server imports it while it is still loading. Helpers that live there are
resolved at call time instead.
"""

import asyncio
from collections import OrderedDict, deque
import hashlib
import hmac
import importlib
import json
import logging
import os
import threading
import time
import urllib.parse
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from hermes_cli.bl_rental_webhook import router as _bl_rental_router

# Same logger the routes used while they lived in web_server.py, so log
# filtering and routing are unchanged (upstream's web_routers do the same).
_log = logging.getLogger("hermes_cli.web_server")

router = APIRouter()

# Payment-confirmed rental provisioning (BigLobster Stripe side → Hermes).
# Carries its own HMAC auth; see hermes_cli/bl_rental_webhook.py.
router.include_router(_bl_rental_router)


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------

@router.get("/health")
async def health_check():
    """Simple health probe for container orchestrators (Zeabur, Fly.io, Railway, etc.).

    Always returns 200 as long as the process is alive.  The richer liveness
    data (gateway state, active sessions, etc.) lives on /api/status.
    """
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Cron job copy
# ---------------------------------------------------------------------------


def _cron_helper(name: str):
    """Return web_server's cron helper ``name``, resolved at call time.

    Looked up on each call (not imported) so there is no import cycle and
    ``monkeypatch.setattr(web_server, name, ...)`` still takes effect. Upstream
    v2026.9.x moves these helpers to ``hermes_cli.web_server_cron``; fall back
    there so this route keeps working across that merge.
    """
    from hermes_cli import web_server

    fn = getattr(web_server, name, None)
    if fn is None:
        fn = getattr(importlib.import_module("hermes_cli.web_server_cron"), name)
    return fn


def _find_cron_job_profile(job_id: str) -> Optional[str]:
    return _cron_helper("_find_cron_job_profile")(job_id)


def _call_cron_for_profile(target_profile: Optional[str], func_name: str, *args, **kwargs):
    return _cron_helper("_call_cron_for_profile")(target_profile, func_name, *args, **kwargs)


def _cron_schedule_expr(job: dict[str, Any]) -> str:
    """Return the raw schedule expression from a stored job dict.

    Reconstructs a string that parse_schedule() can re-ingest:
    - cron jobs   → the cron expression (e.g. "0 9 * * *")
    - interval    → "every Nm" (e.g. "every 30m")
    - once/other  → schedule_display as a best-effort fallback
    """
    sched = job.get("schedule") or {}
    kind = str(sched.get("kind") or "")
    if kind == "cron":
        return str(sched.get("expr") or "")
    if kind == "interval":
        mins = sched.get("minutes") or 0
        return f"every {int(mins)}m"
    return str(job.get("schedule_display") or sched.get("display") or "")


@router.post("/api/cron/jobs/{job_id}/copy")
async def copy_cron_job(
    job_id: str,
    to_profile: str = "default",
    profile: Optional[str] = None,
):
    """Copy a cron job to another (or the same) profile."""
    source_profile = profile or _find_cron_job_profile(job_id)
    if not source_profile:
        raise HTTPException(status_code=404, detail="Job not found")
    source_job = _call_cron_for_profile(source_profile, "get_job", job_id)
    if not source_job:
        raise HTTPException(status_code=404, detail="Job not found")
    schedule_expr = _cron_schedule_expr(source_job)
    if not schedule_expr:
        raise HTTPException(status_code=400, detail="Cannot determine schedule expression for copy")
    try:
        new_job = _call_cron_for_profile(
            to_profile,
            "create_job",
            prompt=source_job.get("prompt") or "",
            schedule=schedule_expr,
            name=source_job.get("name") or "",
            deliver=source_job.get("deliver") or "local",
            skills=source_job.get("skills") or None,
            model=source_job.get("model") or None,
            provider=source_job.get("provider") or None,
            base_url=source_job.get("base_url") or None,
            script=source_job.get("script") or None,
            no_agent=bool(source_job.get("no_agent")),
            enabled_toolsets=source_job.get("enabled_toolsets") or None,
            workdir=source_job.get("workdir") or None,
        )
    except Exception as exc:
        _log.exception("POST /api/cron/jobs/%s/copy failed", job_id)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return new_job


# ---------------------------------------------------------------------------
# Cron job trigger — the panel's "Run now"
# ---------------------------------------------------------------------------
#
# Upstream's handler (web_routers/cron.py ``_trigger_cron_job_sync``) runs the
# job synchronously inside the dashboard request via ``fire_due``. A Gap Hunter
# run takes 20+ minutes, so Zeabur's proxy drops the request long before it
# returns and the panel never shows its confirmation (2026-10-06) — and the run
# executes in the dashboard process with ``adapters=None`` instead of the
# gateway. Here the job is only marked due (``trigger_job``, what
# ``hermes cron run`` does); the gateway's ticker fires it within one 60s tick,
# on the profile's own pool, and the request answers at once. Same path wins
# over upstream's because this router is mounted first.


def _job_in_flight(home, job_id: str) -> bool:
    """True when the store's executions ledger holds a claimed/running attempt.

    Read from the store's executions.db directly: the scheduler's in-process
    registry lives in the gateway, not in the dashboard serving this request.
    """
    import sqlite3
    from pathlib import Path

    db = Path(home) / "cron" / "executions.db"
    if not db.exists():
        return False
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT 1 FROM executions WHERE job_id=? AND status IN ('claimed', 'running') LIMIT 1",
            (job_id,),
        ).fetchone()
    finally:
        con.close()
    return row is not None


def _trigger_cron_job_sync(job_id: str, profile: Optional[str]) -> dict[str, Any]:
    from cron.scheduler_provider import InProcessCronScheduler, resolve_cron_scheduler
    from hermes_cli.web_routers import cron as upstream_cron

    selected = upstream_cron._job_profile(job_id, profile)
    _name, home = _cron_helper("_cron_profile_home")(selected)
    with _cron_helper("_cron_store_scope")(home):
        provider = resolve_cron_scheduler()
    if not isinstance(provider, InProcessCronScheduler):
        # An external provider fires remotely; marking the local store due would do nothing.
        return upstream_cron._trigger_cron_job_sync(job_id, profile)

    job = _call_cron_for_profile(selected, "resolve_job_ref", job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if _job_in_flight(home, job["id"]):
        raise HTTPException(status_code=409, detail="Job is already running")
    try:
        queued = _call_cron_for_profile(selected, "trigger_job", job["id"])
    except ValueError as exc:  # terminal one-shot: trigger_job names the resume command
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not queued:
        raise HTTPException(status_code=404, detail="Job not found")
    return queued


@router.post("/api/cron/jobs/{job_id}/trigger")
async def trigger_cron_job(job_id: str, profile: Optional[str] = None):
    return await asyncio.to_thread(_trigger_cron_job_sync, job_id, profile)


# ---------------------------------------------------------------------------
# Delegate API — async task delegation for external orchestrators
# ---------------------------------------------------------------------------

class _DelegateRequest(BaseModel):
    task_id: str
    prompt: str
    webhook_url: str
    profile: Optional[str] = None  # target Hermes profile (customer); None = default profile


# Shared secret with the BigLobster COO. ``/api/delegate`` sits on the
# dashboard-auth public allowlist (``dashboard_auth/public_paths.py``) so
# BigLobster's server-to-server call is not bounced by the OAuth gate — this
# header check, not the allowlist, is the actual security boundary. It is
# mandatory: with no secret configured the route refuses every request
# rather than letting anyone on the public host run arbitrary prompts.
_DELEGATE_SECRET_ENV_VAR = "HERMES_CALLBACK_SECRET"
_DELEGATE_SECRET_HEADER = "x-hermes-secret"
_DEFAULT_CALLBACK_URL = "https://biglobster.top/api/hermes-callback"
_DNS_REBIND_SUFFIXES = (
    ".internal", ".local", ".localhost", ".nip.io", ".sslip.io", ".xip.io",
    ".localtest.me", ".lvh.me",
)

# Per-IP brute-force throttle on failed /api/delegate authentication attempts.
_DELEGATE_AUTH_MAX_FAILURES = 10
_DELEGATE_AUTH_WINDOW_SEC = 60.0
_DELEGATE_AUTH_MAX_BUCKETS = 4096
_delegate_auth_failures: OrderedDict[str, deque[float]] = OrderedDict()
_delegate_auth_lock = threading.Lock()


def _delegate_client_ip(request: Request) -> str:
    return request.client.host if request.client else "_unknown_"


def _is_delegate_rate_limited(ip: str) -> bool:
    now = time.monotonic()
    cutoff = now - _DELEGATE_AUTH_WINDOW_SEC
    with _delegate_auth_lock:
        bucket = _delegate_auth_failures.get(ip or "_unknown_")
        if not bucket:
            return False
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        return len(bucket) >= _DELEGATE_AUTH_MAX_FAILURES


def _record_delegate_auth_failure(ip: str) -> None:
    now = time.monotonic()
    cutoff = now - _DELEGATE_AUTH_WINDOW_SEC
    key = ip or "_unknown_"
    with _delegate_auth_lock:
        if key not in _delegate_auth_failures and len(_delegate_auth_failures) >= _DELEGATE_AUTH_MAX_BUCKETS:
            for k in list(_delegate_auth_failures.keys()):
                b = _delegate_auth_failures[k]
                while b and b[0] < cutoff:
                    b.popleft()
                if not b:
                    _delegate_auth_failures.pop(k, None)
            while key not in _delegate_auth_failures and len(_delegate_auth_failures) >= _DELEGATE_AUTH_MAX_BUCKETS:
                _delegate_auth_failures.popitem(last=False)
        bucket = _delegate_auth_failures.get(key)
        if bucket is None:
            bucket = deque()
            _delegate_auth_failures[key] = bucket
        else:
            _delegate_auth_failures.move_to_end(key)
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        bucket.append(now)


def _reset_delegate_rate_limit() -> None:
    with _delegate_auth_lock:
        _delegate_auth_failures.clear()


def _verify_delegate_secret(request: Request) -> Optional[tuple[int, str]]:
    """Return ``(status_code, detail)`` when the request should be rejected, else ``None``.

    A request with no secret header is a 401 before anything else, configured or not."""
    ip = _delegate_client_ip(request)
    if _is_delegate_rate_limited(ip):
        return 429, "Too many failed authentication attempts"
    provided = request.headers.get(_DELEGATE_SECRET_HEADER, "")
    if not provided:
        _record_delegate_auth_failure(ip)
        return 401, "Unauthorized"
    secret = os.environ.get(_DELEGATE_SECRET_ENV_VAR, "").strip()
    if not secret:
        _log.error(
            "%s is not set — refusing /api/delegate requests", _DELEGATE_SECRET_ENV_VAR
        )
        return 503, "Delegate endpoint is not configured"
    if not hmac.compare_digest(provided.encode(), secret.encode()):
        _record_delegate_auth_failure(ip)
        return 401, "Unauthorized"
    return None


def _is_unsafe_ip(ip) -> bool:
    return bool(
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve_webhook_host_ips(host: str) -> list[str]:
    import socket

    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return [info[4][0] for info in infos if info and len(info) >= 5 and info[4]]


def _validate_delegate_webhook_url(url: str) -> Optional[str]:
    """Return a rejection reason when ``url`` is unsafe to POST results to, else ``None``.

    The secret check above proves the *caller* is trusted; it says nothing about
    where the caller wants the agent's output delivered. A full domain allowlist
    isn't practical — different customer profiles legitimately callback to
    different domains — so this blocks non-https schemes, credentials in URLs,
    loopback/private/link-local/internal hosts, wildcard DNS-rebinding domains,
    and hostnames that resolve to non-public IP addresses.
    """
    import ipaddress

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        return "webhook_url must use https"
    if parsed.username is not None or parsed.password is not None:
        return "webhook_url must not contain userinfo"
    raw_host = parsed.hostname
    if not raw_host:
        return "webhook_url is missing a host"
    host = raw_host.strip().lower().rstrip(".")
    if host == "localhost" or host.endswith(_DNS_REBIND_SUFFIXES):
        return "webhook_url may not target an internal host"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if _is_unsafe_ip(ip):
            return "webhook_url may not target a loopback/private/link-local address"
        return None
    for resolved_raw in _resolve_webhook_host_ips(host):
        try:
            resolved_ip = ipaddress.ip_address(resolved_raw.split("%", 1)[0])
        except ValueError:
            continue
        if _is_unsafe_ip(resolved_ip):
            return "webhook_url may not target a loopback/private/link-local address"
    return None


def _is_trusted_callback_origin(webhook_url: str) -> bool:
    """True only when ``webhook_url`` shares the origin of ``HERMES_CALLBACK_URL``
    (or ``https://biglobster.top/api/hermes-callback`` when unset).

    The raw ``x-hermes-secret`` grants full ``/api/delegate`` execution, so it
    must never be sent to third-party or tenant webhook origins; external
    callbacks receive ``x-hermes-signature`` (HMAC-SHA256) instead.
    """
    from hermes_cli.urllib_security import url_origin

    configured = os.environ.get("HERMES_CALLBACK_URL", "").strip() or _DEFAULT_CALLBACK_URL
    try:
        target_origin = url_origin(webhook_url)
        trusted_origin = url_origin(configured)
    except ValueError:
        return False
    return bool(target_origin[1]) and target_origin == trusted_origin


def _require_delegate_secret(request: Request) -> None:
    """Route dependency: FastAPI runs it before validating the body, so an anonymous caller
    gets 401 and never the request schema in a 422."""
    rejection = _verify_delegate_secret(request)
    if rejection is not None:
        status_code, detail = rejection
        _log.warning("Rejected /api/delegate request: %s", detail)
        raise HTTPException(status_code=status_code, detail=detail)


@router.post("/api/delegate", status_code=202, dependencies=[Depends(_require_delegate_secret)])
async def post_delegate(request: Request, body: _DelegateRequest):
    """Accept a task from an external orchestrator and execute it asynchronously.

    Returns 202 immediately. When the agent finishes, POSTs the result to
    ``webhook_url`` with ``{"task_id", "status", "response", "error"}``.

    When ``profile`` is set, the task runs in that profile's ``HERMES_HOME``
    (own workspace, memory, sessions). When omitted, it runs in-process in the
    default profile (unchanged behavior).

    Requires the ``x-hermes-secret`` header to match ``HERMES_CALLBACK_SECRET`` —
    see ``_verify_delegate_secret`` for why that, not the dashboard-auth
    allowlist, is the real gate on this endpoint.
    """
    webhook_reason = _validate_delegate_webhook_url(body.webhook_url)
    if webhook_reason:
        raise HTTPException(status_code=400, detail=f"Invalid webhook_url: {webhook_reason}")

    asyncio.create_task(
        _delegate_background(body.task_id, body.prompt, body.webhook_url, body.profile)
    )
    return {"task_id": body.task_id, "status": "accepted"}


async def _delegate_background(
    task_id: str, prompt: str, webhook_url: str, profile: Optional[str] = None
) -> None:
    from hermes_cli.delegate_core import run_delegate_agent, run_delegate_in_profile

    loop = asyncio.get_event_loop()
    try:
        if profile:
            result = await loop.run_in_executor(
                None, run_delegate_in_profile, task_id, prompt, profile
            )
        else:
            result = await loop.run_in_executor(None, run_delegate_agent, task_id, prompt)
        payload: dict = {
            "task_id": task_id,
            "status": "error" if result.get("error") else "completed",
            "response": result.get("final_response", ""),
        }
        if result.get("error"):
            payload["error"] = result["error"]
    except Exception as exc:
        _log.exception("Delegate background task failed for task_id=%s", task_id)
        payload = {"task_id": task_id, "status": "error", "error": str(exc)}

    try:
        import httpx
        raw_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        callback_headers: dict = {"Content-Type": "application/json"}
        callback_secret = os.environ.get("HERMES_CALLBACK_SECRET", "").strip()
        if callback_secret:
            ts = str(int(time.time()))
            sig = hmac.new(
                callback_secret.encode("utf-8"),
                ts.encode("utf-8") + b"." + raw_body,
                hashlib.sha256,
            ).hexdigest()
            callback_headers["x-hermes-timestamp"] = ts
            callback_headers["x-hermes-signature"] = f"sha256={sig}"
            if _is_trusted_callback_origin(webhook_url):
                callback_headers["x-hermes-secret"] = callback_secret
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            await client.post(webhook_url, content=raw_body, headers=callback_headers)
    except Exception:
        _log.exception(
            "Delegate webhook delivery failed for task_id=%s to %s", task_id, webhook_url
        )


# Delegate task execution (system prompt + agent runner, in-process and
# profile-scoped subprocess paths) lives in hermes_cli/delegate_core.py.
# See _delegate_background above.
