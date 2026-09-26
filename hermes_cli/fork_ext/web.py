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
import hmac
import importlib
import logging
import os
import urllib.parse
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
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


def _cron_schedule_expr(job: Dict[str, Any]) -> str:
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


def _verify_delegate_secret(request: Request) -> Optional[Tuple[int, str]]:
    """Return ``(status_code, detail)`` when the request should be rejected, else ``None``."""
    secret = os.environ.get(_DELEGATE_SECRET_ENV_VAR, "").strip()
    if not secret:
        _log.error(
            "%s is not set — refusing /api/delegate requests", _DELEGATE_SECRET_ENV_VAR
        )
        return 503, "Delegate endpoint is not configured"
    provided = request.headers.get(_DELEGATE_SECRET_HEADER, "")
    if not provided or not hmac.compare_digest(provided.encode(), secret.encode()):
        return 401, "Unauthorized"
    return None


def _validate_delegate_webhook_url(url: str) -> Optional[str]:
    """Return a rejection reason when ``url`` is unsafe to POST results to, else ``None``.

    The secret check above proves the *caller* is trusted; it says nothing about
    where the caller wants the agent's output (and the callback secret itself)
    delivered. A full domain allowlist isn't practical — different customer
    profiles legitimately callback to different domains — so this only blocks
    the shapes that would point the callback at the engine's own network:
    non-https schemes and loopback/private/link-local/internal hosts.
    """
    import ipaddress

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        return "webhook_url must use https"
    host = parsed.hostname
    if not host:
        return "webhook_url is missing a host"
    if host == "localhost" or host.endswith(".internal") or host.endswith(".local"):
        return "webhook_url may not target an internal host"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (
        ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast
    ):
        return "webhook_url may not target a loopback/private/link-local address"
    return None


@router.post("/api/delegate", status_code=202)
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
    rejection = _verify_delegate_secret(request)
    if rejection is not None:
        status_code, detail = rejection
        _log.warning(
            "Rejected /api/delegate request for task_id=%s: %s", body.task_id, detail
        )
        raise HTTPException(status_code=status_code, detail=detail)

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
        callback_headers: dict = {}
        callback_secret = os.environ.get("HERMES_CALLBACK_SECRET", "")
        if callback_secret:
            callback_headers["x-hermes-secret"] = callback_secret
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(webhook_url, json=payload, headers=callback_headers)
    except Exception:
        _log.exception(
            "Delegate webhook delivery failed for task_id=%s to %s", task_id, webhook_url
        )


# Delegate task execution (system prompt + agent runner, in-process and
# profile-scoped subprocess paths) lives in hermes_cli/delegate_core.py.
# See _delegate_background above.
