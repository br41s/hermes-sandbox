"""Tool handlers — the code that runs when the LLM calls each CRM tool.

Thin wrappers over :mod:`plugins.crm.crm_db`. Every handler returns a JSON
string via ``tool_result`` / ``tool_error`` (the registry contract).
"""

from __future__ import annotations

from typing import Optional

from tools.registry import tool_error, tool_result

from plugins.crm import crm_db


def _as_str_list(value) -> Optional[list[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    try:
        return [str(v) for v in value]
    except TypeError:
        return [str(value)]


def _handle_from_args(args: dict) -> Optional[list[dict]]:
    """Build a one-element handle list from platform+user_id, if both given."""
    platform = str(args.get("platform") or "").strip()
    user_id = str(args.get("user_id") or "").strip()
    if platform and user_id:
        return [{"platform": platform, "user_id": user_id}]
    return None


def _summarize(contact: dict) -> dict:
    """Compact view for tool output — keep token cost low."""
    return {
        "id": contact["id"],
        "display_name": contact["display_name"],
        "status": contact["status"],
        "tags": contact["tags"],
        "emails": contact["emails"],
        "handles": contact["handles"],
    }


def handle_crm_find_contact(args: dict, **_kw) -> str:
    platform = str(args.get("platform") or "").strip()
    user_id = str(args.get("user_id") or "").strip()
    try:
        # Exact handle lookup takes precedence when both are supplied.
        if platform and user_id:
            c = crm_db.find_contact_by_handle(platform, user_id)
            return tool_result(
                {"contacts": [_summarize(c)] if c else [], "total": 1 if c else 0}
            )
        limit = int(args.get("limit") or 20)
        res = crm_db.list_contacts(
            q=args.get("query") or None,
            status=args.get("status") or None,
            tag=args.get("tag") or None,
            limit=limit,
        )
        return tool_result(
            {
                "contacts": [_summarize(c) for c in res["contacts"]],
                "total": res["total"],
            }
        )
    except Exception as exc:
        return tool_error(f"crm_find_contact failed: {type(exc).__name__}: {exc}")


def handle_crm_create_contact(args: dict, **_kw) -> str:
    name = str(args.get("display_name") or "").strip()
    if not name:
        return tool_error("display_name is required")
    try:
        c = crm_db.create_contact(
            name,
            status=args.get("status"),
            source=args.get("source"),
            notes=str(args.get("notes") or ""),
            emails=_as_str_list(args.get("emails")),
            tags=_as_str_list(args.get("tags")),
            handles=_handle_from_args(args),
        )
        return tool_result({"created": True, "contact": _summarize(c)})
    except ValueError as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"crm_create_contact failed: {type(exc).__name__}: {exc}")


def handle_crm_update_contact(args: dict, **_kw) -> str:
    cid = str(args.get("id") or "").strip()
    if not cid:
        return tool_error("id is required")
    try:
        existing = crm_db.get_contact(cid)
        if not existing:
            return tool_error(f"no contact with id {cid!r}")

        # add_tags appends to the current set; tags (if given) is authoritative.
        tags = _as_str_list(args.get("tags"))
        add_tags = _as_str_list(args.get("add_tags"))
        if tags is None and add_tags:
            tags = list(dict.fromkeys([*existing["tags"], *add_tags]))

        # A new handle links in addition to existing ones (not a replace).
        new_handle = _handle_from_args(args)
        handles = None
        if new_handle:
            handles = [*existing["handles"], *new_handle]

        c = crm_db.update_contact(
            cid,
            display_name=args.get("display_name"),
            status=args.get("status"),
            notes=args.get("notes"),
            emails=_as_str_list(args.get("emails")),
            tags=tags,
            handles=handles,
        )
        return tool_result({"updated": True, "contact": _summarize(c)})
    except ValueError as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"crm_update_contact failed: {type(exc).__name__}: {exc}")
