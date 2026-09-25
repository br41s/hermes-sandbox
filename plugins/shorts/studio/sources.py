"""Free footage and music for the render side.

B-roll: Pexels (portrait, needs a free ``PEXELS_API_KEY``) first, Mixkit's
public pages as the keyless fallback. Music: Mixkit's free stock music, picked
by mood tag and never repeating a recently used track.

Both licences allow commercial use inside an edited video, which is the only
way anything here is used — clips are always cropped, cut and composited
under graphics and voice, never handed on as-is.

Selection is deterministic for a given ``seed`` (the request id), so a re-run
of the same package picks the same clips.
"""

from __future__ import annotations

import hashlib
import os
import re
import urllib.parse
from pathlib import Path
from typing import Dict, Iterable, List, Optional

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) BigLobsterShortsStudio/1.0"
TIMEOUT = 45.0
MAX_DOWNLOAD_BYTES = 120 * 1024 * 1024

MIXKIT_VIDEO_PAGE = "https://mixkit.co/free-stock-video/{slug}/"
MIXKIT_MUSIC_PAGE = "https://mixkit.co/free-stock-music/tag/{slug}/"
_MIXKIT_VIDEO = re.compile(r"https://assets\.mixkit\.co/videos/[^\"'\s<>]+?\.mp4")
_MIXKIT_MUSIC = re.compile(r"https://assets\.mixkit\.co/music/[^\"'\s<>]+?\.mp3")
_MIXKIT_ID = re.compile(r"/(?:videos|music)/(?:preview/[a-z0-9-]*?-)?(\d+)")

# Known-good beds, used when the tag page cannot be read. 516 is the bed the
# manual workflow shipped with. Extend as tracks prove themselves.
FALLBACK_MUSIC = ["516"]


class SourceError(RuntimeError):
    pass


def _client():
    import httpx

    return httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT})


def _pick(candidates: List[Dict], avoid: Iterable[str], seed: str) -> Optional[Dict]:
    avoid = {str(a) for a in avoid}
    fresh = [c for c in candidates if str(c["id"]) not in avoid]
    pool = fresh or candidates
    if not pool:
        return None
    # Stable choice among the top few: varied between shorts, identical on re-run.
    top = pool[: min(4, len(pool))]
    idx = int(hashlib.sha256(seed.encode()).hexdigest(), 16) % len(top)
    return top[idx]


def _slug(query: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")


def pexels_candidates(query: str) -> List[Dict]:
    key = (os.environ.get("PEXELS_API_KEY") or "").strip()
    if not key:
        return []
    from plugins.shorts.stock import StockError, search_clips

    try:
        clips = search_clips(query, per_page=10, min_duration=4)
    except StockError:
        return []
    return [{"id": f"pexels-{c['id']}", "url": c["url"], "source": "pexels",
             "duration": c.get("duration")} for c in clips]


def mixkit_video_candidates(query: str) -> List[Dict]:
    try:
        with _client() as client:
            resp = client.get(MIXKIT_VIDEO_PAGE.format(slug=_slug(query)))
        if resp.status_code != 200:
            return []
        html = resp.text
    except Exception:
        return []
    seen, out = set(), []
    for url in _MIXKIT_VIDEO.findall(html):
        m = _MIXKIT_ID.search(url)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        vid = m.group(1)
        # Prefer the 1080 rendition when the page only links the 720/360 preview.
        out.append({"id": f"mixkit-{vid}", "source": "mixkit",
                    "url": f"https://assets.mixkit.co/videos/{vid}/{vid}-1080.mp4",
                    "fallback_url": url})
    return out


def find_broll(query: str, avoid: Iterable[str], seed: str) -> Optional[Dict]:
    """Best clip for ``query``: Pexels portrait if a key is set, else Mixkit."""
    for finder in (pexels_candidates, mixkit_video_candidates):
        choice = _pick(finder(query), avoid, seed)
        if choice:
            return choice
    return None


def find_music(mood: str, avoid: Iterable[str], seed: str) -> Optional[Dict]:
    candidates: List[Dict] = []
    try:
        with _client() as client:
            resp = client.get(MIXKIT_MUSIC_PAGE.format(slug=_slug(mood)))
        if resp.status_code == 200:
            seen = set()
            for url in _MIXKIT_MUSIC.findall(resp.text):
                m = _MIXKIT_ID.search(url)
                if m and m.group(1) not in seen:
                    seen.add(m.group(1))
                    candidates.append({"id": m.group(1), "source": "mixkit",
                                       "url": f"https://assets.mixkit.co/music/{m.group(1)}/{m.group(1)}.mp3",
                                       "fallback_url": url})
    except Exception:
        pass
    if not candidates:
        candidates = [{"id": i, "source": "mixkit",
                       "url": f"https://assets.mixkit.co/music/{i}/{i}.mp3"} for i in FALLBACK_MUSIC]
    return _pick(candidates, avoid, seed)


def download(url: str, dest: Path, *, fallback_url: Optional[str] = None) -> Path:
    """Stream ``url`` to ``dest`` (bounded). Tries ``fallback_url`` on failure."""
    errors = []
    for candidate in [u for u in (url, fallback_url) if u]:
        parsed = urllib.parse.urlparse(candidate)
        if parsed.scheme != "https":
            errors.append(f"{candidate}: https only")
            continue
        written = 0
        try:
            with _client() as client, client.stream("GET", candidate) as resp:
                if resp.status_code != 200:
                    errors.append(f"{candidate}: HTTP {resp.status_code}")
                    continue
                with open(dest, "wb") as handle:
                    for chunk in resp.iter_bytes(1 << 16):
                        written += len(chunk)
                        if written > MAX_DOWNLOAD_BYTES:
                            raise SourceError(f"{candidate}: larger than {MAX_DOWNLOAD_BYTES >> 20} MB")
                        handle.write(chunk)
            if written:
                return dest
            errors.append(f"{candidate}: empty body")
        except SourceError as exc:
            errors.append(str(exc))
        except Exception as exc:
            errors.append(f"{candidate}: {exc}")
    raise SourceError("; ".join(errors) or f"could not download {url}")
