"""How the published shorts did, read back from YouTube: the ``performance`` action.

Read-only. One token refresh, ``videos.list`` (Data API, 1 unit per 50 ids) to
keep only the shorts that are public, then one YouTube Analytics
``reports.query`` per 200 ids for views, average view duration and
percentage, likes and shares.

Each short's views become a multiple of the channel median: the median views of
the shorts measured here, so a 2.0 is twice a typical BigLobster short whatever
the channel's size. For each trait (hook_pattern, lang, palette) the report gives
the median multiple and n; under ``MIN_N`` it says "not enough signal" and gives
no number. Nothing is estimated: a short too young to judge, private or deleted
is left out and counted, never filled in.

Views are lifetime views, so a short is measured only from ``MIN_AGE_DAYS``
after publishing: Analytics lags two or three days and a short's views come
mostly in its first week. Only the last ``MAX_SHORTS`` measured shorts count,
so the baseline follows the channel as it is now.

The refresh token needs ``yt-analytics.readonly`` and ``youtube.readonly``
(``scripts/youtube_oauth.py``, shorts/STUDIO.md §3.2 step 8).
"""

from __future__ import annotations

import calendar
import statistics
import time
from typing import Any, Dict, List, Optional

from plugins.shorts.social import youtube

REPORTS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
METRICS = ("views", "averageViewDuration", "averageViewPercentage", "likes", "shares")
TRAITS = ("hook_pattern", "lang", "palette")
MIN_N = 5
MIN_AGE_DAYS = 7
MAX_SHORTS = 60
TIMEOUT = 30.0
NO_SIGNAL = "not enough signal"


def _client():
    import httpx

    return httpx.Client(timeout=TIMEOUT)


def _epoch(stamp: str) -> Optional[int]:
    try:
        return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def _chunks(items: List[str], size: int) -> List[List[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _public(client, auth: Dict[str, str], ids: List[str]) -> set:
    public = set()
    for chunk in _chunks(ids, 50):
        resp = client.get(youtube.VIDEOS_URL, params={"part": "status", "id": ",".join(chunk)}, headers=auth)
        if resp.status_code != 200:
            raise youtube._error(resp, "videos.list")
        public |= {v["id"] for v in resp.json().get("items", [])
                   if (v.get("status") or {}).get("privacyStatus") == "public"}
    return public


def _report(client, auth: Dict[str, str], ids: List[str], start: str, end: str) -> Dict[str, Dict[str, float]]:
    stats: Dict[str, Dict[str, float]] = {}
    for chunk in _chunks(ids, 200):
        resp = client.get(REPORTS_URL, headers=auth, params={
            "ids": "channel==MINE", "startDate": start, "endDate": end,
            "metrics": ",".join(METRICS), "dimensions": "video",
            "filters": "video==" + ",".join(chunk), "sort": "-views", "maxResults": 200,
        })
        if resp.status_code != 200:
            err = youtube._error(resp, "analytics reports.query")
            if resp.status_code == 403:
                raise youtube.YouTubeError(f"{err} — the refresh token needs yt-analytics.readonly and the "
                                           "YouTube Analytics API enabled (shorts/STUDIO.md §3.2 step 8)")
            raise err
        body = resp.json()
        names = [h["name"] for h in body.get("columnHeaders", [])]
        for row in body.get("rows") or []:
            rec = dict(zip(names, row))
            stats[rec["video"]] = {m: rec.get(m, 0) for m in METRICS}
    return stats


def _trait_table(shorts: List[Dict[str, Any]], trait: str, median: float) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[float]] = {}
    for s in shorts:
        if s.get(trait):  # unrounded, so a trait's median is not a median of roundings
            groups.setdefault(s[trait], []).append(s["views"] / median)
    table = {}
    for value, multiples in sorted(groups.items()):
        n = len(multiples)
        table[value] = ({"n": n, "median_multiple": round(statistics.median(multiples), 2)} if n >= MIN_N
                        else {"n": n, "median_multiple": None, "signal": NO_SIGNAL})
    return table


def performance(entries: List[Dict[str, Any]], *, now: Optional[float] = None) -> Dict[str, Any]:
    """The report for the ledger ``entries`` (oldest first). Raises ``YouTubeError``."""
    if not youtube.configured():
        raise youtube.YouTubeError("YouTube is not configured (YOUTUBE_CLIENT_ID/SECRET/REFRESH_TOKEN)")
    now = time.time() if now is None else now
    cutoff = now - MIN_AGE_DAYS * 86400
    on_youtube, too_young = [], 0
    for e in entries:
        yt = (e.get("published") or {}).get("youtube") or {}
        at = _epoch(yt.get("at", ""))
        if not yt.get("id") or at is None:
            continue
        if at > cutoff:
            too_young += 1
            continue
        on_youtube.append((e, yt["id"], at))
    on_youtube = on_youtube[-MAX_SHORTS:]
    result: Dict[str, Any] = {"min_age_days": MIN_AGE_DAYS, "min_n": MIN_N, "too_young": too_young,
                              "not_public": 0, "measured": 0, "baseline": None, "traits": {}, "shorts": []}
    if not on_youtube:
        return result

    ids = [vid for _, vid, _ in on_youtube]
    start = time.strftime("%Y-%m-%d", time.gmtime(min(at for _, _, at in on_youtube)))
    end = time.strftime("%Y-%m-%d", time.gmtime(now))
    with _client() as client:
        auth = {"Authorization": f"Bearer {youtube.access_token(client)}"}
        public = _public(client, auth, ids)
        stats = _report(client, auth, [i for i in ids if i in public], start, end) if public else {}

    shorts = []
    for e, vid, at in on_youtube:
        if vid not in public:
            continue
        # A public video with no row had no views in the window: that is a 0, not a gap.
        s = stats.get(vid) or {m: 0 for m in METRICS}
        shorts.append({
            "request_id": e.get("request_id"), "video_id": vid, "lang": e.get("lang"),
            "hook_pattern": e.get("hook_pattern"), "palette": e.get("palette"),
            "published": time.strftime("%Y-%m-%d", time.gmtime(at)),
            "views": int(s["views"]), "avg_view_duration_s": round(float(s["averageViewDuration"]), 1),
            "avg_view_pct": round(float(s["averageViewPercentage"]), 1),
            "likes": int(s["likes"]), "shares": int(s["shares"]),
        })
    result.update(not_public=len(on_youtube) - len(shorts), measured=len(shorts), window=[start, end])
    if not shorts:
        return result
    median = statistics.median(s["views"] for s in shorts)
    result["baseline"] = {"median_views": median, "n": len(shorts)}
    if median <= 0:
        # Half the shorts have no views: every multiple would divide by zero.
        result["note"] = "the median short has 0 views, so no multiple can be computed"
        result["shorts"] = shorts
        return result
    for s in shorts:
        s["multiple"] = round(s["views"] / median, 2)
    result["traits"] = {t: _trait_table(shorts, t, median) for t in TRAITS}
    result["shorts"] = shorts
    return result
