"""Payment-confirmed provisioning webhook for BigLobster rentals.

This is the trigger ``AGENT_RENTAL_SETUP.md`` deferred until a payment gate
existed. BigLobster owns the Stripe integration; when an order is confirmed on
its side it POSTs the order here, HMAC-signed with a shared secret, and this
module calls ``scripts/provision_bl_client.provision()`` — the exact same code
path the CEO runs by hand. No human is in the per-order loop.

Design notes worth keeping in mind before changing anything here:

* **Deterministic, not agentic.** The existing webhook *adapter*
  (``gateway/platforms/webhook.py``) turns a payload into a prompt and lets a
  model act on it. That is the wrong shape for provisioning a paid order — a
  model that skips a step spends the buyer's money and creates half a tenant.
  This route calls the provisioning function directly.
* **Its own auth, like /api/delegate.** The path is on the dashboard-auth
  allowlist (``dashboard_auth/public_paths.py``) so BigLobster's server-to-server
  call is not bounced by the OAuth gate. The HMAC check below — not the
  allowlist — is the security boundary, and it is mandatory: with no secret
  configured, the route refuses every request rather than running open.
* **Idempotent on ``order_id``.** Stripe webhooks retry. A replay of an order
  that already provisioned returns the stored result instead of creating a
  second profile and a second set of cron jobs billed to the buyer's key.

See AGENT_RENTAL_SETUP.md for the request/response contract, which is written
so the BigLobster side can be wired against it without reading this file.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from utils import atomic_replace

_log = logging.getLogger(__name__)

router = APIRouter()

# Shared secret with BigLobster's Stripe-side server. A credential, so it lives
# in .env (config.yaml is for behaviour, per AGENTS.md). Unset => route closed.
SECRET_ENV_VAR = "BL_RENTAL_WEBHOOK_SECRET"

SIGNATURE_HEADER = "x-bl-signature"
TIMESTAMP_HEADER = "x-bl-timestamp"

# Replay window for the signed timestamp. Wide enough for a slow Stripe->
# BigLobster->Hermes hop and modest clock skew, narrow enough that a captured
# request is not indefinitely replayable.
MAX_TIMESTAMP_SKEW_SECONDS = 300

# How long an order may sit "in_progress" before a retry is allowed to take it
# over. Provisioning takes seconds; anything past this is a crashed run, and a
# paid order must not be pinned by one.
STALE_IN_PROGRESS_SECONDS = 1800

# Durable state, both under HERMES_HOME and 0600 (they hold panel passwords and
# order details), written the same tempfile+chmod+atomic_replace way
# hermes_cli/webhook.py writes its subscription secrets.
ORDERS_FILENAME = "bl_rental_orders.json"
INSTANCES_FILENAME = "bl_site_instances.json"
_STATE_FILE_MODE = 0o600

# Guards the read-decide-write window on both files. FastAPI serialises these
# handlers on one event loop, so an asyncio lock is enough within a process;
# the files themselves are the cross-restart record.
_state_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# State files
# ---------------------------------------------------------------------------

def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _load_state(filename: str) -> dict:
    path = _hermes_home() / filename
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        _log.exception("Could not read %s; treating as empty", path)
        return {}


def _save_state(filename: str, data: dict) -> None:
    path = _hermes_home() / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, _STATE_FILE_MODE)
        atomic_replace(tmp_path, path)
        os.chmod(path, _STATE_FILE_MODE)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _verify_signature(raw_body: bytes, signature: str, timestamp: str, secret: str) -> Optional[str]:
    """Return None when the request is authentic, else a reason string.

    Signed content is ``<timestamp>.<raw body>``, so a captured body cannot be
    replayed outside the window with its original signature. Both the header
    comparison and the digest comparison are constant time.
    """
    if not signature or not timestamp:
        return "missing signature or timestamp header"
    try:
        sent_at = int(timestamp)
    except ValueError:
        return "timestamp header is not an integer unix time"
    if abs(time.time() - sent_at) > MAX_TIMESTAMP_SKEW_SECONDS:
        return "timestamp outside the replay window"

    expected = hmac.new(
        secret.encode(),
        timestamp.encode() + b"." + raw_body,
        hashlib.sha256,
    ).hexdigest()
    provided = signature[len("sha256=") :] if signature.startswith("sha256=") else signature
    if not hmac.compare_digest(provided.encode(), expected.encode()):
        return "signature mismatch"
    return None


# ---------------------------------------------------------------------------
# Instance pool
# ---------------------------------------------------------------------------
# A paid checkout cannot wait for someone to click through Zeabur, so blank
# bl-site-package instances are deployed AHEAD of demand and claimed here. That
# turns "deploy a site for this customer" (per-order human work, which is what
# makes something a custom project) into restocking inventory, which is not
# per-order and not per-customer.
#
# ~/.hermes/bl_site_instances.json:
#   {"instances": [{"site_url": "https://bl-blank-01.zeabur.app",
#                   "status": "free"}]}

def _claim_instance(order_id: str) -> Optional[str]:
    state = _load_state(INSTANCES_FILENAME)
    instances = state.get("instances")
    if not isinstance(instances, list):
        return None
    for entry in instances:
        if isinstance(entry, dict) and entry.get("status") == "free" and entry.get("site_url"):
            entry["status"] = "claimed"
            entry["order_id"] = order_id
            entry["claimed_at"] = int(time.time())
            _save_state(INSTANCES_FILENAME, state)
            return entry["site_url"]
    return None


def _release_instance(site_url: str) -> None:
    """Hand a claimed instance back after a failed provision.

    Without this a transient failure would burn a blank instance per retry and
    silently drain the pool.
    """
    state = _load_state(INSTANCES_FILENAME)
    for entry in state.get("instances", []) or []:
        if isinstance(entry, dict) and entry.get("site_url") == site_url:
            entry["status"] = "free"
            entry.pop("order_id", None)
            entry.pop("claimed_at", None)
    _save_state(INSTANCES_FILENAME, state)


def _free_instance_count() -> int:
    instances = _load_state(INSTANCES_FILENAME).get("instances") or []
    return sum(1 for e in instances if isinstance(e, dict) and e.get("status") == "free")


# ---------------------------------------------------------------------------
# CEO notification
# ---------------------------------------------------------------------------

def _notify_ceo(text: str) -> None:
    """Best-effort Telegram ping to the configured home channel.

    A paid order that fails must never fail *silently* — the buyer has been
    charged on the BigLobster side. Delivery problems here are logged and
    swallowed: the HTTP response is the authoritative signal to BigLobster.
    """
    try:
        from tools.send_message_tool import send_message_tool

        send_message_tool({"action": "send", "target": "telegram", "message": text})
    except Exception:
        _log.exception("Could not notify the CEO about a rental provisioning event")


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

class RentalOrder(BaseModel):
    order_id: str
    slug: str
    client_name: str
    openrouter_key: str
    agents: list[str]
    questionnaire: dict[str, Any] | None = None
    # Optional — omit to claim a blank instance from the pool.
    site_url: Optional[str] = None
    # Optional — omit and one is generated and returned for BigLobster to send
    # to the buyer. Never logged.
    panel_password: Optional[str] = None
    fal_key: Optional[str] = None
    old_site_url: Optional[str] = None
    image_model: Optional[str] = None


# Error code -> HTTP status. 4xx means "this order is wrong, a retry with the
# same body will fail the same way"; 5xx means "try again". Getting this split
# right is what keeps Stripe's retry schedule from hammering a doomed order.
_STATUS_BY_CODE = {
    "invalid_questionnaire": 400,
    "invalid_order": 400,
    "invalid_api_key": 400,
    "slug_collision": 409,
    "site_already_claimed": 409,
    "no_instance_available": 503,
    "site_unreachable": 502,
    "site_setup_failed": 502,
    "internal": 500,
}


def _error(code: str, detail: str, order_id: str, **extra: Any) -> JSONResponse:
    body = {"status": "failed", "code": code, "detail": detail, "order_id": order_id}
    body.update(extra)
    return JSONResponse(status_code=_STATUS_BY_CODE.get(code, 500), content=body)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@router.post("/api/bl/rental/provision")
async def post_bl_rental_provision(request: Request):
    secret = os.environ.get(SECRET_ENV_VAR, "").strip()
    if not secret:
        # Fail closed. An unauthenticated provisioning endpoint would let
        # anyone create profiles and cron jobs on this engine.
        _log.error("%s is not set — refusing rental provisioning requests", SECRET_ENV_VAR)
        return JSONResponse(
            status_code=503,
            content={"status": "failed", "code": "not_configured",
                     "detail": f"{SECRET_ENV_VAR} is not set on the Hermes engine."},
        )

    raw_body = await request.body()
    reason = _verify_signature(
        raw_body,
        request.headers.get(SIGNATURE_HEADER, ""),
        request.headers.get(TIMESTAMP_HEADER, ""),
        secret,
    )
    if reason:
        _log.warning("Rejected rental provisioning request: %s", reason)
        return JSONResponse(status_code=401, content={"status": "failed", "code": "unauthorized", "detail": reason})

    try:
        order = RentalOrder(**json.loads(raw_body.decode("utf-8")))
    except Exception as exc:
        return _error("invalid_order", f"Malformed order payload: {exc}", order_id="")

    async with _state_lock:
        orders = _load_state(ORDERS_FILENAME)
        prior = orders.get(order.order_id)
        if prior:
            # Idempotency: a retried webhook must not double-provision.
            if prior.get("status") == "in_progress":
                # A crash mid-provision would otherwise pin a paid order at
                # "in progress" forever and 409 every retry. Anything older
                # than the stale window is treated as retryable — provision()
                # re-checks the slug and the template application is
                # idempotent, so a genuine straggler still can't double up.
                started = prior.get("started_at", 0)
                if time.time() - started < STALE_IN_PROGRESS_SECONDS:
                    return JSONResponse(
                        status_code=409,
                        content={"status": "in_progress", "order_id": order.order_id,
                                 "detail": "This order is already being provisioned."},
                    )
                _log.warning("Order %s was stuck in_progress since %s; retrying", order.order_id, started)
            if prior.get("status") == "provisioned":
                result = dict(prior["result"])
                result["idempotent_replay"] = True
                return JSONResponse(status_code=200, content=result)
            # A previously failed order is allowed to retry from scratch.

        site_url = order.site_url or _claim_instance(order.order_id)
        claimed_from_pool = site_url is not None and order.site_url is None
        if not site_url:
            _notify_ceo(
                f"⚠️ BigLobster rental order {order.order_id} ({order.client_name}) could not be "
                "provisioned: the blank bl-site-package instance pool is empty. The buyer has "
                "paid. Deploy more blank instances and register them in "
                f"~/.hermes/{INSTANCES_FILENAME}, then BigLobster can replay the webhook."
            )
            return _error("no_instance_available", "No blank site instance is available to claim.", order.order_id)

        panel_password = order.panel_password or secrets.token_urlsafe(18)
        orders[order.order_id] = {"status": "in_progress", "started_at": int(time.time()),
                                  "slug": order.slug, "site_url": site_url}
        _save_state(ORDERS_FILENAME, orders)

    code, detail, result = await asyncio.get_event_loop().run_in_executor(
        None, _run_provision, order, site_url, panel_password
    )

    async with _state_lock:
        orders = _load_state(ORDERS_FILENAME)
        if code:
            orders[order.order_id] = {"status": "failed", "code": code, "detail": detail,
                                      "failed_at": int(time.time()), "slug": order.slug}
            _save_state(ORDERS_FILENAME, orders)
            if claimed_from_pool:
                _release_instance(site_url)
        else:
            payload = {
                "status": "provisioned",
                "order_id": order.order_id,
                "profile": result["profile"],
                "site_url": site_url,
                "panel_url": f"{site_url.rstrip('/')}/panel",
                "panel_password": panel_password,
                "jobs": result["jobs"],
                "site_setup": result.get("site_setup"),
            }
            orders[order.order_id] = {"status": "provisioned", "provisioned_at": int(time.time()),
                                      "result": payload}
            _save_state(ORDERS_FILENAME, orders)

    if code:
        _notify_ceo(
            f"⚠️ BigLobster rental order {order.order_id} ({order.client_name}) FAILED to provision.\n"
            f"Code: {code}\n{detail}\n"
            "The buyer has already paid — this needs a hand."
        )
        return _error(code, detail, order.order_id)

    remaining = _free_instance_count()
    _notify_ceo(
        f"✅ BigLobster rental order {order.order_id} provisioned for {order.client_name}.\n"
        f"Profile: {result['profile']} · Site: {site_url} · Agents: {', '.join(order.agents)}\n"
        f"Blank instances left in the pool: {remaining}"
    )
    if remaining <= 2:
        _notify_ceo(
            f"⚠️ Only {remaining} blank bl-site-package instance(s) left. Deploy more and add them "
            f"to ~/.hermes/{INSTANCES_FILENAME} before the pool runs dry — an empty pool bounces "
            "paid orders."
        )
    return JSONResponse(status_code=200, content=payload)


def _run_provision(order: RentalOrder, site_url: str, panel_password: str):
    """Blocking provisioning call. Returns (error_code, detail, result).

    Runs in a threadpool: provision() does several seconds of synchronous
    network I/O (OpenRouter key + model checks, FAL check, the site template
    application) and must not block the dashboard's event loop.
    """
    from scripts.bl_site_setup import SiteSetupError
    from scripts.provision_bl_client import KeyValidationError, provision

    try:
        result = provision(
            slug=order.slug,
            client_name=order.client_name,
            site_url=site_url,
            panel_password=panel_password,
            openrouter_key=order.openrouter_key,
            agents=order.agents,
            # "local", like the hand-run path. A rented profile's .env carries
            # only that client's site credentials — no Telegram bot token — so
            # a "telegram" delivery target would resolve to nothing and the
            # job report would vanish. CEO visibility comes from _notify_ceo
            # below, which runs in the default profile that does have one.
            deliver="local",
            old_site_url=order.old_site_url,
            fal_key=order.fal_key,
            image_model=order.image_model,
            questionnaire=order.questionnaire,
        )
    except FileExistsError as exc:
        return "slug_collision", str(exc), None
    except KeyValidationError as exc:
        return "invalid_api_key", str(exc), None
    except SiteSetupError as exc:
        return exc.code, str(exc), None
    except ValueError as exc:
        return "invalid_order", str(exc), None
    except Exception as exc:  # noqa: BLE001 — the buyer paid; never 500 silently
        _log.exception("Rental provisioning crashed for order %s", order.order_id)
        return "internal", f"{type(exc).__name__}: {exc}", None
    return None, "", result
