"""The Facebook Page + Instagram community inbox: new comments, replies, daily metrics.

The cron agent asks for what changed (``new_comments``), answers one comment
(``reply``), discards one (``skip``) and once a day publishes the metrics
table to Buzz (``metrics_daily``). Every write goes through the same three
rails:

* **Shadow by default.** ``reply`` only calls Meta when
  ``SOCIAL_REPLY_MODE=live``; otherwise it records what it would have said.
* **Never twice.** The ledger (``workspace/social/ledger.json``) remembers
  every comment id it replied to, skipped or drafted, and ``new_comments`` stops
  offering it. A shadow draft is offered again once the mode is live, so the
  comments of the shadow days still get their real reply, once.
* **A daily cap.** ``SOCIAL_REPLIES_MAX_PER_DAY`` (default 15) bounds live
  replies per Bangkok day, so a loop cannot burn the Page's rate limit.

Reply text passes the same guardrail as the YouTube comment replies
(``plugins/shorts/post_upload._text``): up to 500 characters, no e-mail
address, no link unless it is an https ``biglobster.top`` one.

Config (service env of the profile that runs the job):

    META_PAGE_ID, META_PAGE_ACCESS_TOKEN   the Page and its long-lived token
    META_IG_USER_ID                        the linked professional account
    SOCIAL_REPLY_MODE                      ``live`` replies; anything else is shadow
    SOCIAL_REPLIES_MAX_PER_DAY             default 15
    SOCIAL_METRICS_BUZZ_CHANNEL            channel for the daily table
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from plugins.shorts import buzz_share
from plugins.shorts import post_upload
from plugins.shorts._env import env
from plugins.shorts.social import meta

logger = logging.getLogger(__name__)

BUZZ_TIMEOUT = 120
COMMENT_LIMIT = 500
NEW_COMMENTS_SHOWN = 30
METRICS_DAYS = 14
ICT = timezone(timedelta(hours=7))   # Bangkok, where the owner reads the table


def _done_states() -> tuple[str, ...]:
    """A shadow draft only closes a comment while still in shadow: once live, it gets its real reply."""
    return ("replied", "skipped", "shadow") if reply_mode() == "shadow" else ("replied", "skipped")


class SocialError(RuntimeError):
    pass


SOCIAL_INBOX_SCHEMA: dict[str, Any] = {
    "name": "social_inbox",
    "description": (
        "Facebook Page and Instagram comments. action='new_comments' lists "
        "unanswered comments that Hermes has not touched yet, newest first. "
        "action='reply' answers one comment (network and comment_id from "
        "new_comments); it stays in shadow mode unless SOCIAL_REPLY_MODE=live, "
        "refuses a comment already answered, and respects the daily cap. "
        "action='skip' records a comment as intentionally ignored, with a "
        "reason. action='metrics_daily' posts the 14-day FB/IG metrics table "
        "to the Buzz channel, once per day."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["new_comments", "reply", "skip", "metrics_daily"],
                "description": "Which operation to perform.",
            },
            "network": {
                "type": "string",
                "enum": ["facebook", "instagram"],
                "description": "reply: the network of the comment.",
            },
            "comment_id": {
                "type": "string",
                "description": "reply/skip: the comment id, exactly as new_comments returned it.",
            },
            "text": {
                "type": "string",
                "description": "reply: the reply text, up to 500 characters.",
            },
            "reason": {
                "type": "string",
                "description": "skip: one line on why this comment is not answered.",
            },
        },
        "required": ["action"],
    },
}


def _ok(**payload: Any) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


def _fail(error: str, **payload: Any) -> str:
    return json.dumps({"success": False, "error": error, **payload}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Ledger and settings
# ---------------------------------------------------------------------------

def ledger_path() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "workspace" / "social" / "ledger.json"


def _load() -> dict[str, Any]:
    path = ledger_path()
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    return {"comments": data.get("comments") or {},
            "replies_by_day": data.get("replies_by_day") or {},
            "metrics_posted": data.get("metrics_posted")}


def _save(data: dict[str, Any]) -> None:
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _today() -> str:
    return datetime.now(ICT).date().isoformat()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def reply_mode() -> str:
    """``live`` replies; anything else is shadow mode (record, reply to nobody)."""
    return "live" if env("SOCIAL_REPLY_MODE").strip().lower() == "live" else "shadow"


def _max_replies_per_day() -> int:
    raw = env("SOCIAL_REPLIES_MAX_PER_DAY")
    try:
        return int(raw) if raw else 15
    except ValueError:
        return 15


def _replies_left(data: dict[str, Any]) -> int:
    used = int(data["replies_by_day"].get(_today()) or 0)
    return max(0, _max_replies_per_day() - used)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _action_new_comments(args: dict[str, Any]) -> str:
    data = _load()
    try:
        comments = meta.page_comments(meta.page_posts()) + meta.ig_comments(meta.ig_media())
    except meta.MetaError as exc:
        return _fail(str(exc))
    fresh = [c for c in comments
             if not c.get("answered") and (data["comments"].get(c.get("comment_id")) or {}).get("state") not in _done_states()]
    fresh.sort(key=lambda c: c.get("created") or "", reverse=True)
    fresh = fresh[:NEW_COMMENTS_SHOWN]
    return _ok(comments=fresh, count=len(fresh), reply_mode=reply_mode(),
               replies_left_today=_replies_left(data))


def _action_reply(args: dict[str, Any]) -> str:
    network = (args.get("network") or "").strip()
    comment_id = (args.get("comment_id") or "").strip()
    if network not in ("facebook", "instagram"):
        return _fail("network must be 'facebook' or 'instagram'")
    if not comment_id:
        return _fail("comment_id is required")
    problems: list[str] = []
    text = post_upload._text(args.get("text"), "text", COMMENT_LIMIT, problems)
    if problems:
        return _fail("; ".join(problems))

    data = _load()
    state = (data["comments"].get(comment_id) or {}).get("state")
    if state == "replied" or (state == "shadow" and reply_mode() == "shadow"):
        return _fail(f"comment {comment_id} is already {state}; it is never answered twice")
    today = _today()
    cap = _max_replies_per_day()
    used = int(data["replies_by_day"].get(today) or 0)
    if used >= cap:
        return _fail(f"daily reply cap reached ({cap} per day, {today} ICT)")
    left = cap - used

    if reply_mode() == "shadow":
        data["comments"][comment_id] = {"state": "shadow", "network": network,
                                        "text": text, "at": _now()}
        _save(data)
        return _ok(shadow=True, would_reply=text, reply_mode="shadow", replies_left_today=left)

    try:
        result = meta.reply_comment(network, comment_id, text)
    except meta.MetaError as exc:
        return _fail(str(exc))
    data["comments"][comment_id] = {"state": "replied", "network": network, "text": text,
                                    "reply_id": result.get("id"), "at": _now()}
    data["replies_by_day"][today] = used + 1
    _save(data)
    return _ok(replied=True, comment_id=comment_id, reply_id=result.get("id"),
               reply_mode="live", replies_left_today=left - 1)


def _action_skip(args: dict[str, Any]) -> str:
    comment_id = (args.get("comment_id") or "").strip()
    reason = (args.get("reason") or "").strip()
    if not comment_id:
        return _fail("comment_id is required")
    if not reason:
        return _fail("reason is required")
    data = _load()
    data["comments"][comment_id] = {"state": "skipped", "reason": reason, "at": _now()}
    _save(data)
    return _ok(skipped=True, comment_id=comment_id)


def _cell(value: Any) -> str:
    return "—" if value is None else str(value)


def _totals_line(rows: list[dict[str, Any]], red: str) -> str:
    mine = [row for row in rows if row.get("red") == red]
    parts = []
    for key, label in (("alcance", "Alcance"), ("vistas", "Vistas"), ("likes", "Likes"),
                       ("comentarios", "Comentarios"), ("compartidos", "Shares"), ("guardados", "Saves")):
        values = [row[key] for row in mine if row.get(key) is not None]
        parts.append(f"{label} {sum(values) if values else '—'}")
    return f"Totales {red.capitalize()}: " + " · ".join(parts)


def _table(rows: list[dict[str, Any]], today: str) -> str:
    lines = [f"📊 Métricas FB/IG últimos 14 días (Hermes, {today} ICT)", "",
             _totals_line(rows, "facebook"), _totals_line(rows, "instagram"), "",
             "| Fecha | Red | Enlace | Tipo | Alcance | Vistas | Likes | Com | Shares | Saves |",
             "|" + "---|" * 10]
    for row in rows:
        url = row.get("enlace") or ""
        lines.append("| " + " | ".join([
            row.get("fecha") or "", row.get("red") or "", f"[enlace]({url})" if url else "—",
            row.get("tipo") or "—", _cell(row.get("alcance")), _cell(row.get("vistas")),
            _cell(row.get("likes")), _cell(row.get("comentarios")),
            _cell(row.get("compartidos")), _cell(row.get("guardados")),
        ]) + " |")
    return "\n".join(lines)


def _post_to_buzz(channel: str, content: str) -> None:
    binary = buzz_share.cli()
    if not binary:
        raise SocialError("the buzz CLI is not installed in this image")
    try:
        done = subprocess.run([binary, "messages", "send", "--channel", channel, "--content", "-"],
                              input=content, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=BUZZ_TIMEOUT, env=buzz_share._child_env(),
                              check=False)
    except subprocess.TimeoutExpired as exc:
        raise SocialError(f"buzz messages send timed out after {BUZZ_TIMEOUT}s") from exc
    if done.returncode != 0:
        tail = " / ".join((done.stderr or done.stdout or "").strip().splitlines()[-4:])
        raise SocialError(f"buzz messages send failed ({done.returncode}): {tail[:400]}")


def _action_metrics_daily(args: dict[str, Any]) -> str:
    today = _today()
    data = _load()
    if data.get("metrics_posted") == today:
        return _ok(already=True)
    channel = env("SOCIAL_METRICS_BUZZ_CHANNEL")
    if not channel:
        return _fail("SOCIAL_METRICS_BUZZ_CHANNEL is not set")
    try:
        rows = meta.post_metrics(METRICS_DAYS)
    except meta.MetaError as exc:
        return _fail(f"metrics: {exc}")
    try:
        _post_to_buzz(channel, _table(rows, today))
    except SocialError as exc:
        return _fail(str(exc))
    data["metrics_posted"] = today
    _save(data)
    return _ok(posted=True, rows=len(rows))


_ACTIONS: dict[str, Callable[[dict[str, Any]], str]] = {
    "new_comments": _action_new_comments,
    "reply": _action_reply,
    "skip": _action_skip,
    "metrics_daily": _action_metrics_daily,
}


def handle_social_inbox(args: dict[str, Any], **_kwargs: Any) -> str:
    action = (args.get("action") or "").strip()
    handler = _ACTIONS.get(action)
    if handler is None:
        return _fail(f"unknown action {action!r}; one of {sorted(_ACTIONS)}")
    try:
        return handler(args)
    except Exception as exc:  # pragma: no cover - defensive: tools return JSON, never raise
        logger.error("social_inbox %s failed: %s", action, exc, exc_info=True)
        return _fail(f"unexpected failure in {action}: {exc}")


def check_social_available() -> bool:
    """Visible only where Meta's Page and Instagram account are configured."""
    return meta.configured(instagram=True)
