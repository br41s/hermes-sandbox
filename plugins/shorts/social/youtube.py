"""YouTube Shorts upload through the YouTube Data API v3 (free, quota-based).

Replaces the browser upload in YouTube Studio, which stopped on "Verify it's
you" prompts. A short is any vertical video of 3 minutes or less; there is no
separate Shorts endpoint.

Credentials (an OAuth *desktop* client and a refresh token minted once with
``scripts/youtube_oauth.py``):

    YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN
    SHORTS_YOUTUBE_PRIVACY    public | unlisted | private   (default public)
    SHORTS_YOUTUBE_CATEGORY   default 27 (Education)

Quota: an upload is about 1,600 of the default 10,000 daily units; the thumbnail
costs 50 and the captions 400, so two shorts a day use about 4,100.

Until Google's (free) API audit approves the project, the API forces every
upload to private whatever ``privacyStatus`` says. The tool reports the
privacy YouTube actually applied, so that state is never silent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMB_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
CAPTIONS_URL = "https://www.googleapis.com/upload/youtube/v3/captions"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
TIMEOUT = 600.0

REQUIRED_ENV = ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")


class YouTubeError(RuntimeError):
    pass


def configured() -> bool:
    return all((os.environ.get(k) or "").strip() for k in REQUIRED_ENV)


def _client():
    import httpx

    return httpx.Client(timeout=TIMEOUT, follow_redirects=True)


def access_token(client) -> str:
    resp = client.post(TOKEN_URL, data={
        "client_id": os.environ["YOUTUBE_CLIENT_ID"].strip(),
        "client_secret": os.environ["YOUTUBE_CLIENT_SECRET"].strip(),
        "refresh_token": os.environ["YOUTUBE_REFRESH_TOKEN"].strip(),
        "grant_type": "refresh_token",
    })
    if resp.status_code != 200:
        raise YouTubeError(f"token refresh failed: HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json()["access_token"]


def _error(resp, what: str) -> YouTubeError:
    try:
        reason = resp.json().get("error", {}).get("errors", [{}])[0].get("reason", "")
    except Exception:
        reason = ""
    return YouTubeError(f"{what}: HTTP {resp.status_code} {reason} {resp.text[:300]}".strip())


def build_metadata(social: Dict[str, Any], lang: str, *, synthetic: bool) -> Dict[str, Any]:
    yt = social.get("youtube") or {}
    title = (yt.get("title") or "").strip()
    if "#shorts" not in title.lower() and len(title) <= 92:
        title = f"{title} #Shorts"
    return {
        "snippet": {
            "title": title[:100],
            "description": (yt.get("description") or "")[:4900],
            "tags": [t for t in (yt.get("tags") or [])][:15],
            "categoryId": os.environ.get("SHORTS_YOUTUBE_CATEGORY", "27").strip() or "27",
            "defaultLanguage": lang,
            "defaultAudioLanguage": lang,
        },
        "status": {
            "privacyStatus": os.environ.get("SHORTS_YOUTUBE_PRIVACY", "public").strip() or "public",
            "selfDeclaredMadeForKids": False,
            # YouTube's disclosure for realistic synthetic people — true when
            # the short carries an AI avatar.
            "containsSyntheticMedia": bool(synthetic),
        },
    }


def upload(video: Path, thumb: Optional[Path], srt: Optional[Path], social: Dict[str, Any],
           lang: str, *, synthetic: bool = False) -> Dict[str, Any]:
    if not configured():
        raise YouTubeError("YouTube is not configured (YOUTUBE_CLIENT_ID/SECRET/REFRESH_TOKEN)")
    meta = build_metadata(social, lang, synthetic=synthetic)
    size = video.stat().st_size
    warnings: List[str] = []
    with _client() as client:
        auth = {"Authorization": f"Bearer {access_token(client)}"}
        init = client.post(
            UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={**auth, "Content-Type": "application/json; charset=UTF-8",
                     "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)},
            content=json.dumps(meta).encode("utf-8"),
        )
        if init.status_code != 200 or "location" not in init.headers:
            raise _error(init, "start upload")
        put = client.put(init.headers["location"], content=video.read_bytes(),
                         headers={**auth, "Content-Type": "video/mp4"})
        if put.status_code not in (200, 201):
            raise _error(put, "upload video")
        body = put.json()
        video_id = body["id"]
        applied_privacy = body.get("status", {}).get("privacyStatus")

        if thumb and thumb.exists():
            t = client.post(THUMB_URL, params={"videoId": video_id, "uploadType": "media"},
                            headers={**auth, "Content-Type": "image/jpeg"}, content=thumb.read_bytes())
            if t.status_code != 200:
                warnings.append(f"thumbnail not set ({_error(t, 'thumbnail')}); "
                                "custom thumbnails need a phone-verified channel")
        if srt and srt.exists():
            boundary = "hermesshortsboundary"
            parts = (
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                + json.dumps({"snippet": {"videoId": video_id, "language": lang, "name": ""}})
                + f"\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8") + srt.read_bytes() + f"\r\n--{boundary}--\r\n".encode("utf-8")
            c = client.post(CAPTIONS_URL, params={"part": "snippet", "uploadType": "multipart"},
                            headers={**auth, "Content-Type": f"multipart/related; boundary={boundary}"},
                            content=parts)
            if c.status_code != 200:
                warnings.append(f"captions not uploaded ({_error(c, 'captions')})")

    result = {"id": video_id, "url": f"https://youtube.com/shorts/{video_id}",
              "privacy": applied_privacy, "warnings": warnings}
    requested = meta["status"]["privacyStatus"]
    if applied_privacy and applied_privacy != requested:
        result["warnings"].append(
            f"YouTube set the video to {applied_privacy!r} instead of {requested!r} — until the "
            "API project passes Google's audit, API uploads are forced private"
        )
    return result
