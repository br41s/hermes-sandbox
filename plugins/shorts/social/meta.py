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
                            instagram_basic, instagram_content_publish
    META_IG_USER_ID         the Instagram professional account id
    META_GRAPH_VERSION      default v23.0

Instagram processes a video asynchronously. Publishing waits for it, bounded:
if processing outlasts the budget, the container id is returned as
``pending`` and the next call resumes it instead of uploading again.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

TIMEOUT = 300.0
PROCESS_BUDGET = 240.0
POLL_EVERY = 10.0

REQUIRED_ENV = ("META_PAGE_ID", "META_PAGE_ACCESS_TOKEN")


class MetaError(RuntimeError):
    pass


def _v() -> str:
    return (os.environ.get("META_GRAPH_VERSION") or "v23.0").strip()


def graph() -> str:
    return f"https://graph.facebook.com/{_v()}"


def configured(instagram: bool = False) -> bool:
    keys = REQUIRED_ENV + (("META_IG_USER_ID",) if instagram else ())
    return all((os.environ.get(k) or "").strip() for k in keys)


def _token() -> str:
    return os.environ["META_PAGE_ACCESS_TOKEN"].strip()


def _client():
    import httpx

    return httpx.Client(timeout=TIMEOUT, follow_redirects=True)


def _json(resp, what: str) -> Dict[str, Any]:
    try:
        body = resp.json()
    except Exception:
        body = {}
    if resp.status_code >= 400 or (isinstance(body, dict) and body.get("error")):
        err = body.get("error", {}) if isinstance(body, dict) else {}
        raise MetaError(f"{what}: HTTP {resp.status_code} {err.get('message') or resp.text[:300]}")
    return body


def _rupload(client, url: str, video: Path) -> Dict[str, Any]:
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

def facebook_reel(video: Path, description: str) -> Dict[str, Any]:
    if not configured():
        raise MetaError("Meta is not configured (META_PAGE_ID / META_PAGE_ACCESS_TOKEN)")
    page = os.environ["META_PAGE_ID"].strip()
    with _client() as client:
        start = _json(client.post(f"{graph()}/{page}/video_reels",
                                  data={"upload_phase": "start", "access_token": _token()}),
                      "start Facebook reel")
        video_id = start["video_id"]
        upload_url = start.get("upload_url") or f"https://rupload.facebook.com/video-upload/{_v()}/{video_id}"
        _rupload(client, upload_url, video)
        _json(client.post(f"{graph()}/{page}/video_reels", data={
            "upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED",
            "description": description[:2200], "access_token": _token(),
        }), "publish Facebook reel")
    return {"id": video_id, "url": f"https://www.facebook.com/reel/{video_id}"}


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------

def _cover_url(client, cover: Path) -> Optional[str]:
    """Host the titled cover as an unpublished Page photo; return its CDN URL.

    Instagram's ``cover_url`` must be public. An unpublished Page photo gives
    one without any hosting of our own and never appears on the Page.
    """
    page = os.environ["META_PAGE_ID"].strip()
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


def _publish_container(client, container_id: str) -> Dict[str, Any]:
    ig = os.environ["META_IG_USER_ID"].strip()
    media = _json(client.post(f"{graph()}/{ig}/media_publish",
                              data={"creation_id": container_id, "access_token": _token()}),
                  "publish to Instagram")
    info = _json(client.get(f"{graph()}/{media['id']}",
                            params={"fields": "permalink", "access_token": _token()}), "permalink")
    return {"id": media["id"], "url": info.get("permalink")}


def instagram(video: Path, *, kind: str, caption: str = "", cover: Optional[Path] = None,
              pending_container: Optional[str] = None) -> Dict[str, Any]:
    """Publish a Reel (``kind='reel'``) or Story (``kind='story'``).

    Returns ``{"id", "url"}`` when live, or ``{"pending": container_id}`` when
    Instagram is still processing — call again with ``pending_container``.
    """
    if not configured(instagram=True):
        raise MetaError("Instagram is not configured (META_PAGE_ACCESS_TOKEN / META_IG_USER_ID)")
    ig = os.environ["META_IG_USER_ID"].strip()
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
            container = _json(client.post(f"{graph()}/{ig}/media", data=data), f"create {kind} container")
            container_id = container["id"]
            _rupload(client, f"https://rupload.facebook.com/ig-api-upload/{_v()}/{container_id}", video)

        status = _wait_container(client, container_id, PROCESS_BUDGET)
        if status != "FINISHED":
            return {"pending": container_id, "status": status, "warnings": warnings}
        result = _publish_container(client, container_id)
    result["warnings"] = warnings
    return result
