"""Pexels Videos API client — free vertical stock B-roll.

Free tier: 200 requests/hour, 20,000/month. The Pexels licence allows
commercial use with no attribution required, and explicitly permits
modification (which is what we do — crop, add voiceover and captions).
It does *not* permit redistributing clips unaltered, so this module is only
ever used to feed :mod:`plugins.shorts.render`, never to hand a raw clip to
a customer.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.pexels.com/videos/search"
REQUEST_TIMEOUT = 30.0

# We render at 1080x1920. A source narrower than this gets upscaled, which
# looks soft, so prefer files at or above the target width but skip 4K
# monsters that cost more to download than they add.
TARGET_WIDTH = 1080
MAX_SOURCE_WIDTH = 2160


class StockError(RuntimeError):
    """Raised when stock search cannot be performed or returns nothing usable."""


def api_key() -> Optional[str]:
    """Return the configured Pexels key, or None when stock search is off."""
    value = (os.environ.get("PEXELS_API_KEY") or "").strip()
    return value or None


def _pick_video_file(video: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Choose the best portrait MP4 rendition of one Pexels video.

    Pexels returns several renditions per video at different resolutions.
    We want the smallest one that still covers 1080 wide, so the render
    neither upscales nor wastes bandwidth.
    """
    candidates = []
    for f in video.get("video_files") or []:
        if f.get("file_type") != "video/mp4" or not f.get("link"):
            continue
        width, height = f.get("width") or 0, f.get("height") or 0
        if not width or not height or height <= width:
            continue  # landscape or square rendition — unusable at 9:16
        if width > MAX_SOURCE_WIDTH:
            continue
        candidates.append(f)

    if not candidates:
        return None

    # Renditions at or above target width, smallest first; otherwise the
    # widest of the undersized ones.
    at_or_above = [f for f in candidates if f["width"] >= TARGET_WIDTH]
    if at_or_above:
        return min(at_or_above, key=lambda f: f["width"])
    return max(candidates, key=lambda f: f["width"])


def search_clips(
    query: str,
    *,
    per_page: int = 5,
    min_duration: int = 3,
) -> List[Dict[str, Any]]:
    """Search Pexels for portrait video clips matching ``query``.

    Returns a list of ``{id, url, duration, width, height, preview_image}``
    dicts, best rendition already selected. Raises :class:`StockError` when
    the key is missing or the API call fails.
    """
    key = api_key()
    if not key:
        raise StockError(
            "PEXELS_API_KEY is not set, so stock footage search is unavailable. "
            "Supply clip_url on each scene instead, or set the key."
        )
    if not query or not query.strip():
        raise StockError("query is required for stock_search")

    import httpx

    params = {
        "query": query.strip(),
        "orientation": "portrait",
        "size": "medium",
        "per_page": max(1, min(int(per_page), 15)),
    }
    try:
        response = httpx.get(
            SEARCH_URL,
            params=params,
            headers={"Authorization": key},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        # 429 is the common one — the free tier is 200/hour.
        raise StockError(
            f"Pexels returned HTTP {exc.response.status_code} for query "
            f"{query!r}. Retry later or narrow the query."
        ) from exc
    except Exception as exc:  # network, JSON, DNS
        raise StockError(f"Pexels search failed for {query!r}: {exc}") from exc

    clips: List[Dict[str, Any]] = []
    for video in payload.get("videos") or []:
        duration = video.get("duration") or 0
        if duration < min_duration:
            continue
        chosen = _pick_video_file(video)
        if not chosen:
            continue
        clips.append(
            {
                "id": video.get("id"),
                "url": chosen["link"],
                "duration": duration,
                "width": chosen.get("width"),
                "height": chosen.get("height"),
                "preview_image": video.get("image"),
            }
        )

    if not clips:
        raise StockError(
            f"No portrait clips found for {query!r}. Try a simpler, more "
            "concrete visual noun (e.g. 'watering plants' rather than "
            "'irrigation best practices')."
        )
    return clips
