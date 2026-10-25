"""YouTube uploads through the YouTube Data API v3 (free, quota-based): the blog Shorts and,
with ``short=False``, the long episodes of the "Agentes BigLobster" series (plugins/shorts/series.py).
``update`` rewrites an uploaded video's title, description and tags and sets its thumbnail
(about 100 units): SEODesk's optimisation of the series episodes. It can also carry the
video's ``localizations`` (translated titles and descriptions), and ``localize`` adds those
alone. ``api()`` is the authorised client for the other post-upload writes (captions, playlists,
comments) that ``plugins/shorts/post_upload.py`` drives.

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

import contextlib
import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from plugins.shorts._env import env

API_URL = "https://www.googleapis.com/youtube/v3"
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
    return all(env(k) for k in REQUIRED_ENV)


def _client():
    import httpx

    return httpx.Client(timeout=TIMEOUT, follow_redirects=True)


def access_token(client) -> str:
    resp = client.post(TOKEN_URL, data={
        "client_id": env("YOUTUBE_CLIENT_ID"),
        "client_secret": env("YOUTUBE_CLIENT_SECRET"),
        "refresh_token": env("YOUTUBE_REFRESH_TOKEN"),
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


def article_link(article: Optional[dict[str, Any]]) -> str:
    """The article URL tagged for Umami, so visits from YouTube stop counting as "direct".

    Campaign ``short-<slug>`` tells which Short sent the visit (retro 2026-10-12 experiment).
    """
    url = str((article or {}).get("url") or "").strip()
    slug = str((article or {}).get("slug") or "").strip()
    if not url.startswith("https://"):
        return ""
    sep = "&" if "?" in url else "?"
    campaign = f"short-{slug}" if slug else "short"
    return f"{url}{sep}utm_source=youtube&utm_medium=short&utm_campaign={campaign}"


def build_metadata(social: dict[str, Any], lang: str, *, synthetic: bool,
                   article: Optional[dict[str, Any]] = None, short: bool = True,
                   privacy: Optional[str] = None) -> dict[str, Any]:
    """``short=False``: a regular video (no #Shorts in the title). ``privacy`` overrides SHORTS_YOUTUBE_PRIVACY."""
    yt = social.get("youtube") or {}
    title = (yt.get("title") or "").strip()
    if short and "#shorts" not in title.lower() and len(title) <= 92:
        title = f"{title} #Shorts"
    description = (yt.get("description") or "").strip()
    link = article_link(article)
    if link and "utm_source=" not in description:
        description = f"{description}\n\n{link}".strip()
    return {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": [t for t in (yt.get("tags") or [])][:15],
            "categoryId": env("SHORTS_YOUTUBE_CATEGORY", "27") or "27",
            "defaultLanguage": lang,
            "defaultAudioLanguage": lang,
        },
        "status": {
            "privacyStatus": privacy or env("SHORTS_YOUTUBE_PRIVACY", "public") or "public",
            "selfDeclaredMadeForKids": False,
            # YouTube's disclosure for realistic synthetic people — true when
            # the short carries an AI avatar.
            "containsSyntheticMedia": bool(synthetic),
        },
    }


def _set_thumbnail(client, auth: dict[str, str], video_id: str, thumb: Optional[Path], warnings: list[str]) -> None:
    if thumb and thumb.exists():
        t = client.post(THUMB_URL, params={"videoId": video_id, "uploadType": "media"},
                        headers={**auth, "Content-Type": "image/jpeg"}, content=thumb.read_bytes())
        if t.status_code != 200:
            warnings.append(f"thumbnail not set ({_error(t, 'thumbnail')}); "
                            "custom thumbnails need a phone-verified channel")


_SNIPPET_FIELDS = ("title", "description", "tags", "categoryId", "defaultLanguage", "defaultAudioLanguage")


def _insert_caption(client, auth: dict[str, str], video_id: str, lang: str, srt: bytes):
    """``captions.insert`` (400 units): one caption track, returns the raw response."""
    boundary = "hermesshortsboundary"
    parts = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        + json.dumps({"snippet": {"videoId": video_id, "language": lang, "name": ""}})
        + f"\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8") + srt + f"\r\n--{boundary}--\r\n".encode()
    return client.post(CAPTIONS_URL, params={"part": "snippet", "uploadType": "multipart"},
                       headers={**auth, "Content-Type": f"multipart/related; boundary={boundary}"},
                       content=parts)


def _split_default(localizations: dict[str, Any], default: str, warnings: list[str]) -> dict[str, Any]:
    """The localizations for languages other than the video's own (that text is the snippet)."""
    kept = {code: loc for code, loc in localizations.items() if code.lower() != (default or "").lower()}
    if len(kept) != len(localizations):
        dropped = ", ".join(sorted(set(localizations) - set(kept)))
        warnings.append(f"localization skipped for the video's own language ({dropped})")
    return kept


def _put_video(client, auth: dict[str, str], part: str, body: dict[str, Any], what: str) -> None:
    resp = client.put(VIDEOS_URL, params={"part": part},
                      headers={**auth, "Content-Type": "application/json; charset=UTF-8"},
                      content=json.dumps(body).encode("utf-8"))
    if resp.status_code != 200:
        raise _error(resp, what)


def _current(client, auth: dict[str, str], video_id: str, part: str) -> dict[str, Any]:
    got = client.get(VIDEOS_URL, params={"part": part, "id": video_id}, headers=auth)
    if got.status_code != 200:
        raise _error(got, "read video")
    items = got.json().get("items") or []
    if not items:
        raise YouTubeError(f"read video: {video_id} not found")
    return items[0]


def update(video_id: str, social: dict[str, Any], lang: str, thumb: Optional[Path] = None,
           short: bool = False, localizations: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """New title, description and tags for an uploaded video, and its thumbnail if given.
    ``short=True`` keeps the #Shorts tag in the title, as ``upload`` puts it.

    ``videos.update`` replaces the whole snippet, so it is rebuilt complete (category, languages).
    ``localizations`` (``{"id": {"title", "description"}}``) go in the same call, added to the ones
    the video already has. Privacy is not touched.
    """
    if not configured():
        raise YouTubeError("YouTube is not configured (YOUTUBE_CLIENT_ID/SECRET/REFRESH_TOKEN)")
    snippet = build_metadata(social, lang, synthetic=False, short=short)["snippet"]
    warnings: list[str] = []
    body: dict[str, Any] = {"id": video_id, "snippet": snippet}
    part = "snippet"
    with _client() as client:
        auth = {"Authorization": f"Bearer {access_token(client)}"}
        kept = _split_default(localizations or {}, lang, warnings)
        if kept:
            body["localizations"] = {**(_current(client, auth, video_id, "localizations").get("localizations") or {}), **kept}
            part = "snippet,localizations"
        _put_video(client, auth, part, body, "update video")
        _set_thumbnail(client, auth, video_id, thumb, warnings)
    result = {"id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}",
              "thumbnail": bool(thumb) and not any(w.startswith("thumbnail") for w in warnings), "warnings": warnings}
    if kept:
        result["localized"] = sorted(kept)
    return result


def localize(video_id: str, lang: str, localizations: dict[str, Any]) -> dict[str, Any]:
    """Add translated titles and descriptions to a video, leaving its own snippet as it is.

    The snippet is read and sent back complete, because ``videos.update`` replaces it.
    """
    if not configured():
        raise YouTubeError("YouTube is not configured (YOUTUBE_CLIENT_ID/SECRET/REFRESH_TOKEN)")
    warnings: list[str] = []
    with _client() as client:
        auth = {"Authorization": f"Bearer {access_token(client)}"}
        video = _current(client, auth, video_id, "snippet,localizations")
        snippet = {k: v for k, v in (video.get("snippet") or {}).items() if k in _SNIPPET_FIELDS}
        snippet.setdefault("defaultLanguage", lang)
        kept = _split_default(localizations, snippet["defaultLanguage"], warnings)
        if kept:
            _put_video(client, auth, "snippet,localizations",
                       {"id": video_id, "snippet": snippet, "localizations": {**(video.get("localizations") or {}), **kept}},
                       "update localizations")
    return {"id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}",
            "localized": sorted(kept), "warnings": warnings}


class Api:
    """One authorised session for the post-upload writes; each method is one YouTube call."""

    def __init__(self, client, auth: dict[str, str]):
        self._client, self._auth = client, auth

    def call(self, method: str, resource: str, params: Optional[dict[str, Any]] = None,
             body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        headers, extra = dict(self._auth), {}
        if body is not None:
            headers["Content-Type"] = "application/json; charset=UTF-8"
            extra["content"] = json.dumps(body).encode("utf-8")
        resp = self._client.request(method, f"{API_URL}/{resource}", params=params or {}, headers=headers, **extra)
        if resp.status_code != 200:
            raise _error(resp, f"{method} {resource}")
        return resp.json()

    def insert_caption(self, video_id: str, lang: str, srt: bytes) -> dict[str, Any]:
        resp = _insert_caption(self._client, self._auth, video_id, lang, srt)
        if resp.status_code != 200:
            raise _error(resp, "captions")
        return resp.json()


@contextlib.contextmanager
def api() -> Iterator[Api]:
    if not configured():
        raise YouTubeError("YouTube is not configured (YOUTUBE_CLIENT_ID/SECRET/REFRESH_TOKEN)")
    with _client() as client:
        yield Api(client, {"Authorization": f"Bearer {access_token(client)}"})


def upload(video: Path, thumb: Optional[Path], srt: Optional[Path], social: dict[str, Any],
           lang: str, *, synthetic: bool = False,
           article: Optional[dict[str, Any]] = None, short: bool = True,
           privacy: Optional[str] = None) -> dict[str, Any]:
    if not configured():
        raise YouTubeError("YouTube is not configured (YOUTUBE_CLIENT_ID/SECRET/REFRESH_TOKEN)")
    meta = build_metadata(social, lang, synthetic=synthetic, article=article, short=short, privacy=privacy)
    size = video.stat().st_size
    warnings: list[str] = []
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

        _set_thumbnail(client, auth, video_id, thumb, warnings)
        if srt and srt.exists():
            c = _insert_caption(client, auth, video_id, lang, srt.read_bytes())
            if c.status_code != 200:
                warnings.append(f"captions not uploaded ({_error(c, 'captions')})")

    url = f"https://youtube.com/shorts/{video_id}" if short else f"https://www.youtube.com/watch?v={video_id}"
    result = {"id": video_id, "url": url,
              "privacy": applied_privacy, "warnings": warnings}
    requested = meta["status"]["privacyStatus"]
    if applied_privacy and applied_privacy != requested:
        result["warnings"].append(
            f"YouTube set the video to {applied_privacy!r} instead of {requested!r} — until the "
            "API project passes Google's audit, API uploads are forced private"
        )
    return result
