"""Facebook Page Reels and Instagram Reels/Stories through the Graph API (free).

Replaces Zernio (free for two accounts only) with direct calls. Every upload
is a *resumable* upload from the local file, so no public video URL is
needed, unlike Instagram's ``video_url`` flow.

Credentials — one long-lived **Page** access token covers both networks when
the Instagram professional account is linked to the Facebook Page:

    META_PAGE_ID            numeric Facebook Page id
    META_PAGE_ACCESS_TOKEN  long-lived Page token (never expires when minted
                            from a long-lived user token); needs
                            pages_manage_posts, pages_read_engagement,
                            instagram_basic, instagram_content_publish, and
                            pages_manage_engagement, pages_read_user_content,
                            instagram_manage_comments,
                            instagram_manage_insights, read_insights for the
                            comments and metrics jobs that follow
    META_IG_USER_ID         the Instagram professional account id
    META_GRAPH_VERSION      default v23.0
    META_PUBLISH_MODE       ``live`` posts to Meta; anything else is shadow
                            mode (decide and report, post nothing)

A regular Page video (the series episodes, not a Reel) goes through
``facebook_video``: one multipart POST to ``graph-video.facebook.com``.

Instagram processes a video asynchronously. Publishing waits for it, bounded:
if processing outlasts the budget, the container id is returned as
``pending`` and the next call resumes it instead of uploading again.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from plugins.shorts._env import env

logger = logging.getLogger(__name__)

TIMEOUT = 300.0
PROCESS_BUDGET = 240.0
POLL_EVERY = 10.0
IG_UPLOAD_ATTEMPTS = 3    # Meta answers ProcessingFailedError to ~half of identical uploads (2026-11-13)
IG_RETRY_WAIT = 20.0

REQUIRED_ENV = ("META_PAGE_ID", "META_PAGE_ACCESS_TOKEN")

# A Graph API id: digits, optionally one underscore between two (``123_456``).
# Anything else could walk into another endpoint through the URL path.
COMMENT_ID = re.compile(r"^\d+(_\d+)?$")


class MetaError(RuntimeError):
    pass


def _v() -> str:
    return env("META_GRAPH_VERSION", "v23.0")


def graph() -> str:
    return f"https://graph.facebook.com/{_v()}"


def configured(instagram: bool = False) -> bool:
    keys = REQUIRED_ENV + (("META_IG_USER_ID",) if instagram else ())
    return all(env(k) for k in keys)


def publish_mode() -> str:
    """``live`` posts to Meta; anything else is shadow mode (post nothing)."""
    return "live" if env("META_PUBLISH_MODE").strip().lower() == "live" else "shadow"


def _token() -> str:
    return env("META_PAGE_ACCESS_TOKEN")


def _client():
    import httpx

    return httpx.Client(timeout=TIMEOUT, follow_redirects=True)


def _json(resp, what: str) -> dict[str, Any]:
    try:
        body = resp.json()
    except Exception:
        body = {}
    if resp.status_code >= 400 or (isinstance(body, dict) and body.get("error")):
        err = body.get("error", {}) if isinstance(body, dict) else {}
        raise MetaError(f"{what}: HTTP {resp.status_code} {err.get('message') or resp.text[:300]}")
    return body


def _rupload(client, url: str, video: Path) -> dict[str, Any]:
    data = video.read_bytes()
    resp = client.post(url, content=data, headers={
        "Authorization": f"OAuth {_token()}",
        "offset": "0",
        "file_size": str(len(data)),
        "Content-Type": "application/octet-stream",
    })
    return _json(resp, "upload video bytes")


# ---------------------------------------------------------------------------
# Facebook Page Reel
# ---------------------------------------------------------------------------

def facebook_reel(video: Path, description: str, ai_generated: bool = False) -> dict[str, Any]:
    if not configured():
        raise MetaError("Meta is not configured (META_PAGE_ID / META_PAGE_ACCESS_TOKEN)")
    page = env("META_PAGE_ID")
    with _client() as client:
        start = _json(client.post(f"{graph()}/{page}/video_reels",
                                  data={"upload_phase": "start", "access_token": _token()}),
                      "start Facebook reel")
        video_id = start["video_id"]
        upload_url = start.get("upload_url") or f"https://rupload.facebook.com/video-upload/{_v()}/{video_id}"
        _rupload(client, upload_url, video)
        finish = {"upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED",
                  "description": description[:2200], "access_token": _token()}
        if ai_generated:
            finish["is_ai_generated"] = "true"
        _json(client.post(f"{graph()}/{page}/video_reels", data=finish), "publish Facebook reel")
    return {"id": video_id, "url": f"https://www.facebook.com/reel/{video_id}"}


def facebook_video(video: Path, title: str, description: str) -> dict[str, Any]:
    """A regular (non-Reel) Page video, one multipart upload from the local file."""
    if not configured():
        raise MetaError("Meta is not configured (META_PAGE_ID / META_PAGE_ACCESS_TOKEN)")
    page = env("META_PAGE_ID")
    with _client() as client, open(video, "rb") as handle:
        resp = client.post(f"https://graph-video.facebook.com/{_v()}/{page}/videos",
                           data={"title": title[:255], "description": description[:5000],
                                 "access_token": _token()},
                           files={"source": (video.name, handle, "video/mp4")})
        body = _json(resp, "upload Facebook video")
    return {"id": body["id"], "url": f"https://www.facebook.com/{page}/videos/{body['id']}"}


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------

def _cover_url(client, cover: Path) -> Optional[str]:
    """Host the titled cover as an unpublished Page photo; return its CDN URL.

    Instagram's ``cover_url`` must be public. An unpublished Page photo gives
    one without any hosting of our own and never appears on the Page.
    """
    page = env("META_PAGE_ID")
    with open(cover, "rb") as handle:
        resp = client.post(f"{graph()}/{page}/photos",
                           data={"published": "false", "access_token": _token()},
                           files={"source": (cover.name, handle, "image/jpeg")})
    photo = _json(resp, "stage cover")
    info = _json(client.get(f"{graph()}/{photo['id']}",
                            params={"fields": "images", "access_token": _token()}), "read cover")
    images = sorted(info.get("images") or [], key=lambda i: i.get("width", 0), reverse=True)
    return images[0]["source"] if images else None


def _wait_container(client, container_id: str, budget: float) -> str:
    deadline = time.time() + budget
    status = ""
    while time.time() < deadline:
        info = _json(client.get(f"{graph()}/{container_id}",
                                params={"fields": "status_code,status", "access_token": _token()}),
                     "container status")
        status = info.get("status_code", "")
        if status == "FINISHED":
            return status
        if status in ("ERROR", "EXPIRED"):
            raise MetaError(f"Instagram rejected the video: {info.get('status')}")
        time.sleep(POLL_EVERY)
    return status or "IN_PROGRESS"


def _publish_container(client, container_id: str) -> dict[str, Any]:
    ig = env("META_IG_USER_ID")
    media = _json(client.post(f"{graph()}/{ig}/media_publish",
                              data={"creation_id": container_id, "access_token": _token()}),
                  "publish to Instagram")
    info = _json(client.get(f"{graph()}/{media['id']}",
                            params={"fields": "permalink", "access_token": _token()}), "permalink")
    return {"id": media["id"], "url": info.get("permalink")}


def instagram(video: Path, *, kind: str, caption: str = "", cover: Optional[Path] = None,
              pending_container: Optional[str] = None, ai_generated: bool = False) -> dict[str, Any]:
    """Publish a Reel (``kind='reel'``) or Story (``kind='story'``).

    Returns ``{"id", "url"}`` when live, or ``{"pending": container_id}`` when
    Instagram is still processing — call again with ``pending_container``.
    """
    if not configured(instagram=True):
        raise MetaError("Instagram is not configured (META_PAGE_ACCESS_TOKEN / META_IG_USER_ID)")
    ig = env("META_IG_USER_ID")
    warnings = []
    with _client() as client:
        container_id = pending_container
        if not container_id:
            data = {"upload_type": "resumable", "access_token": _token()}
            if kind == "reel":
                data.update({"media_type": "REELS", "caption": caption[:2200], "share_to_feed": "true"})
                if cover is not None and cover.exists():
                    try:
                        url = _cover_url(client, cover)
                        if url:
                            data["cover_url"] = url
                    except MetaError as exc:
                        warnings.append(f"titled cover not attached ({exc}); using a frame from the hook")
                if "cover_url" not in data:
                    data["thumb_offset"] = "1500"  # the hook title is on screen at 1.5s
            elif kind == "story":
                data["media_type"] = "STORIES"
            else:
                raise MetaError(f"unknown Instagram kind {kind!r}")
            if ai_generated:
                data["is_ai_generated"] = "true"
            last: Optional[MetaError] = None
            for attempt in range(1, IG_UPLOAD_ATTEMPTS + 1):
                container = _json(client.post(f"{graph()}/{ig}/media", data=data), f"create {kind} container")
                container_id = container["id"]
                try:
                    _rupload(client, f"https://rupload.facebook.com/ig-api-upload/{_v()}/{container_id}", video)
                    last = None
                    break
                except MetaError as exc:
                    if not ("ProcessingFailedError" in str(exc) or "HTTP 5" in str(exc)):
                        raise
                    last = exc
                    if attempt < IG_UPLOAD_ATTEMPTS:
                        time.sleep(IG_RETRY_WAIT)
            if last is not None:
                raise MetaError(f"Instagram upload failed after {IG_UPLOAD_ATTEMPTS} attempts: {last}")

        status = _wait_container(client, container_id, PROCESS_BUDGET)
        if status != "FINISHED":
            return {"pending": container_id, "status": status, "warnings": warnings}
        result = _publish_container(client, container_id)
    result["warnings"] = warnings
    return result


# ---------------------------------------------------------------------------
# Facebook Page posts and links
# ---------------------------------------------------------------------------

def facebook_link(link: str, message: str) -> dict[str, Any]:
    """Post a link to the Page feed; Facebook builds the preview card itself."""
    if not configured():
        raise MetaError("Meta is not configured (META_PAGE_ID / META_PAGE_ACCESS_TOKEN)")
    page = env("META_PAGE_ID")
    with _client() as client:
        post = _json(client.post(f"{graph()}/{page}/feed",
                                 data={"link": link, "message": message[:5000], "access_token": _token()}),
                     "post link to Facebook")
    return {"id": post["id"], "url": f"https://www.facebook.com/{post['id']}"}


def page_posts(limit: int = 25) -> list[dict[str, Any]]:
    """The Page's published posts and Reels, newest first."""
    if not configured():
        raise MetaError("Meta is not configured (META_PAGE_ID / META_PAGE_ACCESS_TOKEN)")
    page = env("META_PAGE_ID")
    with _client() as client:
        posts = _json(client.get(f"{graph()}/{page}/published_posts",
                                 params={"fields": "id,permalink_url,created_time,message",
                                         "access_token": _token()}), "read Facebook posts")
        reels = _json(client.get(f"{graph()}/{page}/video_reels",
                                 params={"fields": "id,permalink_url,created_time,description",
                                         "access_token": _token()}), "read Facebook reels")
    seen: set = set()
    items = []
    for raw in (posts.get("data") or []) + (reels.get("data") or []):
        if raw.get("id") in seen:
            continue
        seen.add(raw.get("id"))
        items.append({"id": raw.get("id"), "url": raw.get("permalink_url"),
                      "created": raw.get("created_time"),
                      "text": raw.get("message") or raw.get("description") or ""})
    items.sort(key=lambda item: item.get("created") or "", reverse=True)
    return items[:limit]


def page_comments(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Top-level comments on ``posts``; ``answered`` once the Page itself replied."""
    if not configured():
        raise MetaError("Meta is not configured (META_PAGE_ID / META_PAGE_ACCESS_TOKEN)")
    page = str(env("META_PAGE_ID"))
    out: list[dict[str, Any]] = []
    with _client() as client:
        for post in posts:
            try:
                body = _json(client.get(f"{graph()}/{post['id']}/comments", params={
                    "filter": "toplevel", "order": "reverse_chronological",
                    "fields": "id,from{id,name},message,created_time,comments.limit(25){from{id}}",
                    "limit": 50, "access_token": _token()}), "read Facebook comments")
            except MetaError as exc:
                logger.warning("page_comments: skipping post %s: %s", post.get("id"), exc)
                continue
            for comment in body.get("data") or []:
                author = comment.get("from") or {}
                if str(author.get("id")) == page:
                    continue
                replies = (comment.get("comments") or {}).get("data") or []
                answered = any(str((reply.get("from") or {}).get("id")) == page for reply in replies)
                out.append({"network": "facebook", "comment_id": comment.get("id"),
                            "author": author.get("name") or "", "text": comment.get("message") or "",
                            "created": comment.get("created_time"), "post_url": post.get("url"),
                            "answered": answered})
    return out


# ---------------------------------------------------------------------------
# Instagram media and comments
# ---------------------------------------------------------------------------

def instagram_image(image_url: str, caption: str) -> dict[str, Any]:
    """Publish a single image post; the container is processed and published in one call."""
    if not configured(instagram=True):
        raise MetaError("Instagram is not configured (META_PAGE_ACCESS_TOKEN / META_IG_USER_ID)")
    ig = env("META_IG_USER_ID")
    with _client() as client:
        container = _json(client.post(f"{graph()}/{ig}/media",
                                      data={"image_url": image_url, "caption": caption[:2200],
                                            "access_token": _token()}),
                          "create Instagram image container")
        container_id = container["id"]
        status = _wait_container(client, container_id, PROCESS_BUDGET)
        if status != "FINISHED":
            return {"pending": container_id}
        return _publish_container(client, container_id)


def ig_media(limit: int = 25) -> list[dict[str, Any]]:
    """The Instagram account's recent media, newest first."""
    if not configured(instagram=True):
        raise MetaError("Instagram is not configured (META_PAGE_ACCESS_TOKEN / META_IG_USER_ID)")
    ig = env("META_IG_USER_ID")
    with _client() as client:
        body = _json(client.get(f"{graph()}/{ig}/media", params={
            "fields": "id,permalink,timestamp,caption,media_type,like_count,comments_count",
            "limit": limit, "access_token": _token()}), "read Instagram media")
    return [{"id": media.get("id"), "url": media.get("permalink"), "created": media.get("timestamp"),
             "text": media.get("caption") or "", "type": media.get("media_type"),
             "likes": media.get("like_count"), "comments": media.get("comments_count")}
            for media in body.get("data") or []]


def ig_comments(media: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Top-level comments on ``media``; ``answered`` once our own account replied."""
    if not configured(instagram=True):
        raise MetaError("Instagram is not configured (META_PAGE_ACCESS_TOKEN / META_IG_USER_ID)")
    ig = env("META_IG_USER_ID")
    out: list[dict[str, Any]] = []
    with _client() as client:
        me = _json(client.get(f"{graph()}/{ig}", params={"fields": "username", "access_token": _token()}),
                   "read Instagram username").get("username") or ""
        for item in media:
            try:
                body = _json(client.get(f"{graph()}/{item['id']}/comments", params={
                    "fields": "id,username,text,timestamp,replies{username}",
                    "limit": 50, "access_token": _token()}), "read Instagram comments")
            except MetaError as exc:
                logger.warning("ig_comments: skipping media %s: %s", item.get("id"), exc)
                continue
            for comment in body.get("data") or []:
                if comment.get("username") == me:
                    continue
                replies = (comment.get("replies") or {}).get("data") or []
                answered = any(reply.get("username") == me for reply in replies)
                out.append({"network": "instagram", "comment_id": comment.get("id"),
                            "author": comment.get("username") or "", "text": comment.get("text") or "",
                            "created": comment.get("timestamp"), "post_url": item.get("url"),
                            "answered": answered})
    return out


def reply_comment(network: str, comment_id: str, text: str) -> dict[str, Any]:
    """Reply to one comment as the Page (Facebook) or as the professional account (Instagram)."""
    if not COMMENT_ID.fullmatch(comment_id):
        raise MetaError("invalid comment id")
    if network == "facebook":
        if not configured():
            raise MetaError("Meta is not configured (META_PAGE_ID / META_PAGE_ACCESS_TOKEN)")
        path = "comments"
    elif network == "instagram":
        if not configured(instagram=True):
            raise MetaError("Instagram is not configured (META_PAGE_ACCESS_TOKEN / META_IG_USER_ID)")
        path = "replies"
    else:
        raise MetaError(f"unknown network {network!r}")
    with _client() as client:
        body = _json(client.post(f"{graph()}/{comment_id}/{path}",
                                 data={"message": text, "access_token": _token()}),
                     f"reply to {network} comment")
    return {"id": body.get("id")}


# ---------------------------------------------------------------------------
# Daily metrics
# ---------------------------------------------------------------------------

def _when(raw: Any) -> Optional[datetime]:
    """Meta's ISO timestamps, as an aware UTC datetime (``None`` when unparseable)."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _metric_value(body: dict[str, Any], name: str) -> Optional[int]:
    for item in body.get("data") or []:
        if item.get("name") != name:
            continue
        values = item.get("values") or []
        if values:
            return values[0].get("value")
        return (item.get("total_value") or {}).get("value")
    return None


def _best_metric(client, path: str, metric: str) -> Optional[int]:
    """One insights metric; a missing or renamed one is ``None``, never an exception."""
    try:
        body = _json(client.get(f"{graph()}/{path}",
                                params={"metric": metric, "access_token": _token()}),
                     f"read {metric}")
    except MetaError as exc:
        logger.warning("post_metrics: %s unavailable for %s: %s", metric, path, exc)
        return None
    return _metric_value(body, metric)


def _facebook_row(client, post: dict[str, Any], created: datetime) -> dict[str, Any]:
    url = post.get("url") or ""
    kind = "reel" if "/reel/" in url else "post"
    likes = comments = shares = None
    try:
        body = _json(client.get(f"{graph()}/{post['id']}", params={
            "fields": "reactions.summary(total_count).limit(0),comments.summary(total_count).limit(0),shares",
            "access_token": _token()}), "read Facebook post metrics")
        likes = ((body.get("reactions") or {}).get("summary") or {}).get("total_count")
        comments = ((body.get("comments") or {}).get("summary") or {}).get("total_count")
        shares = (body.get("shares") or {}).get("count")
    except MetaError as exc:
        logger.warning("post_metrics: no Facebook totals for %s: %s", post.get("id"), exc)
    reach = _best_metric(client, f"{post['id']}/insights", "post_impressions_unique")
    views = _best_metric(client, f"{post['id']}/video_insights", "total_video_views_unique") if kind == "reel" else None
    return {"fecha": created.date().isoformat(), "red": "facebook", "enlace": url, "tipo": kind,
            "alcance": reach, "vistas": views, "likes": likes, "comentarios": comments,
            "compartidos": shares, "guardados": None}


def _instagram_row(client, media: dict[str, Any], created: datetime) -> dict[str, Any]:
    insights: dict[str, Optional[int]] = {}
    try:
        body = _json(client.get(f"{graph()}/{media['id']}/insights",
                                params={"metric": "reach,views,shares,saved", "access_token": _token()}),
                     "read Instagram insights")
        insights = {name: _metric_value(body, name) for name in ("reach", "views", "shares", "saved")}
    except MetaError as exc:
        logger.warning("post_metrics: combined insights failed for %s (%s); asking one by one", media.get("id"), exc)
        for name in ("reach", "views", "shares", "saved"):
            value = _best_metric(client, f"{media['id']}/insights", name)
            if value is not None:
                insights[name] = value
    return {"fecha": created.date().isoformat(), "red": "instagram", "enlace": media.get("url") or "",
            "tipo": media.get("type") or "", "alcance": insights.get("reach"), "vistas": insights.get("views"),
            "likes": media.get("likes"), "comentarios": media.get("comments"),
            "compartidos": insights.get("shares"), "guardados": insights.get("saved")}


def post_metrics(days: int = 14) -> list[dict[str, Any]]:
    """One row per Facebook post and Instagram media created in the last ``days`` days, newest first.

    Insights metric names change often, so every metric is best effort: a metric
    that Meta refuses is ``None`` and never fails the table.
    """
    if not configured(instagram=True):
        raise MetaError("Instagram is not configured (META_PAGE_ACCESS_TOKEN / META_IG_USER_ID)")
    cutoff = datetime.now(UTC) - timedelta(days=days)
    rows: list[tuple[datetime, dict[str, Any]]] = []
    with _client() as client:
        for post in page_posts():
            created = _when(post.get("created"))
            if created is not None and created >= cutoff:
                rows.append((created, _facebook_row(client, post, created)))
        for media in ig_media():
            created = _when(media.get("created"))
            if created is not None and created >= cutoff:
                rows.append((created, _instagram_row(client, media, created)))
    rows.sort(key=lambda row: row[0], reverse=True)
    return [row for _created, row in rows]
