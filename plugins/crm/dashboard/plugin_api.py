"""CRM dashboard plugin — backend API routes.

Mounted at ``/api/plugins/crm/`` by the dashboard plugin system. Every
route here sits behind the dashboard's session-token auth middleware (the
``/api/plugins/...`` prefix is covered just like core API routes), so no
per-route auth is needed.

This layer is thin: contact CRUD wraps :mod:`plugins.crm.crm_db`, and the
conversation views resolve — read-only — against the existing stores:

* ``<HERMES_HOME>/sessions/sessions.json`` (gateway) maps a contact's
  ``(platform, user_id)`` handles to ``session_id``s, and
* :class:`hermes_state.SessionDB` supplies the messages for a session.

No message data is ever owned or duplicated here.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from plugins.crm import crm_db

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Session-store access (read-only)
# ---------------------------------------------------------------------------

def _sessions_json_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "sessions" / "sessions.json"


def _load_session_entries() -> list[dict[str, Any]]:
    """Return normalized session summaries from ``sessions.json``.

    Each item: ``{session_id, platform, user_id, user_name, chat_type,
    display_name, updated_at}``. The routing identity lives in the nested
    ``origin`` object; top-level ``platform``/``display_name`` are the
    display fallbacks. Missing/malformed file → empty list (never raises).
    """
    path = _sessions_json_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    out: list[dict[str, Any]] = []
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        origin = entry.get("origin") or {}
        platform = (origin.get("platform") or entry.get("platform") or "")
        user_id = origin.get("user_id")
        if not platform or not user_id:
            continue
        out.append(
            {
                "session_id": entry.get("session_id"),
                "platform": str(platform).lower(),
                "user_id": str(user_id),
                "user_name": origin.get("user_name") or entry.get("display_name"),
                "chat_type": entry.get("chat_type") or origin.get("chat_type") or "dm",
                "display_name": entry.get("display_name"),
                "updated_at": entry.get("updated_at"),
            }
        )
    return out


def _content_to_text(content: Any) -> str:
    """Flatten a message's content to plain text for the thread view.

    Provider messages may store content as a string or as a list of typed
    blocks (e.g. ``[{"type": "text", "text": "..."}, ...]``). The CRM thread
    only shows readable text, so non-text blocks are dropped.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                txt = block.get("text") or block.get("content")
                if isinstance(txt, str):
                    parts.append(txt)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)


_session_db = None


def _get_session_db():
    """Lazily construct a shared SessionDB (per-profile default path)."""
    global _session_db
    if _session_db is None:
        from hermes_state import SessionDB

        _session_db = SessionDB()
    return _session_db


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class HandleIn(BaseModel):
    platform: str
    user_id: str


class ContactIn(BaseModel):
    display_name: str
    status: Optional[str] = None
    source: Optional[str] = None
    notes: str = ""
    emails: Optional[list[str]] = None
    tags: Optional[list[str]] = None
    handles: Optional[list[HandleIn]] = None


class ContactPatch(BaseModel):
    display_name: Optional[str] = None
    status: Optional[str] = None
    source: Optional[str] = None
    notes: Optional[str] = None
    emails: Optional[list[str]] = None
    tags: Optional[list[str]] = None
    handles: Optional[list[HandleIn]] = None


def _handles_to_dicts(handles: Optional[list[HandleIn]]):
    if handles is None:
        return None
    return [{"platform": h.platform, "user_id": h.user_id} for h in handles]


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------

@router.get("/meta")
async def get_meta():
    return {"statuses": list(crm_db.STATUSES)}


# ---------------------------------------------------------------------------
# Contacts CRUD
# ---------------------------------------------------------------------------

@router.get("/contacts")
async def list_contacts(
    q: Optional[str] = None,
    status: Optional[str] = None,
    tag: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    return crm_db.list_contacts(q=q, status=status, tag=tag, limit=limit, offset=offset)


@router.post("/contacts")
async def create_contact(body: ContactIn):
    try:
        return crm_db.create_contact(
            body.display_name,
            status=body.status,
            source=body.source,
            notes=body.notes,
            emails=body.emails,
            tags=body.tags,
            handles=_handles_to_dicts(body.handles),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/contacts/{contact_id}")
async def get_contact(contact_id: str):
    c = crm_db.get_contact(contact_id)
    if not c:
        raise HTTPException(status_code=404, detail="contact not found")
    return c


@router.patch("/contacts/{contact_id}")
async def update_contact(contact_id: str, body: ContactPatch):
    try:
        c = crm_db.update_contact(
            contact_id,
            display_name=body.display_name,
            status=body.status,
            source=body.source,
            notes=body.notes,
            emails=body.emails,
            tags=body.tags,
            handles=_handles_to_dicts(body.handles),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not c:
        raise HTTPException(status_code=404, detail="contact not found")
    return c


@router.delete("/contacts/{contact_id}")
async def delete_contact(contact_id: str):
    if not crm_db.delete_contact(contact_id):
        raise HTTPException(status_code=404, detail="contact not found")
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Conversations (resolved read-only from the session stores)
# ---------------------------------------------------------------------------

@router.get("/contacts/{contact_id}/conversations")
async def contact_conversations(contact_id: str):
    """List the gateway sessions that belong to a contact's handles."""
    c = crm_db.get_contact(contact_id)
    if not c:
        raise HTTPException(status_code=404, detail="contact not found")
    wanted = {(h["platform"], h["user_id"]) for h in c["handles"]}
    if not wanted:
        return {"conversations": []}
    convos = [
        s for s in _load_session_entries() if (s["platform"], s["user_id"]) in wanted
    ]
    convos.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
    return {"conversations": convos}


@router.get("/conversations/{session_id}/messages")
async def conversation_messages(session_id: str, limit: int = Query(500, ge=1, le=2000)):
    """Return a session's messages (role/content/timestamp) for the thread view."""
    try:
        rows = _get_session_db().get_messages(session_id)
    except Exception as exc:
        log.warning("CRM: failed to load messages for %s: %s", session_id, exc)
        raise HTTPException(status_code=502, detail="could not load messages")
    messages = [
        {
            "role": r.get("role"),
            "content": _content_to_text(r.get("content")),
            "timestamp": r.get("timestamp"),
        }
        for r in rows[-limit:]
        if r.get("role") in ("user", "assistant")
    ]
    return {"messages": messages}


@router.get("/unlinked")
async def unlinked_conversations():
    """DM sessions whose ``(platform, user_id)`` is not attached to any contact.

    This is the inflow inbox: one click in the UI turns one of these into a
    new contact (or attaches it to an existing one). Group/channel sessions
    are excluded — they aren't a single person.
    """
    linked = set(crm_db.all_handles().keys())  # {"platform:user_id"}
    seen: dict[str, dict[str, Any]] = {}
    for s in _load_session_entries():
        if s["chat_type"] not in ("dm", "private", "direct"):
            continue
        key = f"{s['platform']}:{s['user_id']}"
        if key in linked:
            continue
        # Keep the most recently active session per handle.
        prev = seen.get(key)
        if prev is None or (s.get("updated_at") or "") > (prev.get("updated_at") or ""):
            seen[key] = s
    items = sorted(seen.values(), key=lambda s: s.get("updated_at") or "", reverse=True)
    return {"unlinked": items}
