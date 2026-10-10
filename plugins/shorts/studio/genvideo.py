"""Generated backgrounds for the beats a package marks with ``scene``. Runs in Actions.

A package may ask for up to ``package.MAX_SCENES`` generated backgrounds (the
hook and the most visual beats). Each becomes one clip from OpenRouter's video
API (default model ``heygen/heygen-video-1``: text to video, 5-15 s, 768p, no
avatars, no lip sync), cropped to 1080x1920 under the same Remotion graphics as
stock footage. Every other beat keeps its free ``broll``.

Spend is capped three ways, cheapest check first:

1. Per short: ``SHORTS_AI_SHORT_BUDGET_USD`` (default 0.50). Two shorts a day
   render in parallel and cannot see each other, so this is what keeps their
   sum inside the daily cap.
2. Per day: ``SHORTS_AI_DAILY_CAP_USD`` (default 1.00) against what the key
   has spent today (UTC), read from OpenRouter before planning. Catches
   retries and manual dispatches. If OpenRouter does not report it, only the
   per-short budget applies, and the log says so.
3. The key's own daily limit, set on openrouter.ai. That is the hard stop;
   1 and 2 keep us from ever reaching it mid-render.

Estimates use ``SHORTS_AI_PRICE_PER_SECOND`` (default 0.015, the 768p launch
price, which ends after October). The real cost OpenRouter reports is what the
manifest records, and the log says when it is above the configured price.

Nothing here can fail a render. No key, no budget, a refused request, a model
error or a timeout all leave the beat on its stock footage, and the reason
lands in the manifest.

Config (Actions env): ``SHORTS_OPENROUTER_API_KEY`` (a key used for nothing
else, so shorts can never drain the agents' shared key),
``SHORTS_AI_VIDEO_MODEL``, ``SHORTS_AI_RESOLUTION`` (default 768p),
``SHORTS_AI_TIMEOUT_S`` (default 600, for all clips of one short together).
"""

from __future__ import annotations

import hashlib
import math
import os
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

API = "https://openrouter.ai/api/v1/videos"
KEY_INFO = "https://openrouter.ai/api/v1/key"
_AUTH_HOSTS = {"openrouter.ai"}
MIN_CLIP_S = 5
MAX_CLIP_S = 15
POLL_EVERY_S = 10.0
MAX_DOWNLOAD_BYTES = 150 * 1024 * 1024

# The palette names the short's accent colours; the grade follows them so the
# generated clips and the Remotion graphics read as one piece.
STYLE = ("Vertical 9:16 cinematic b-roll, {colours} colour accents, soft natural light, "
         "shallow depth of field, slow steady camera movement, photographic and calm. "
         "No text, no letters, no numbers, no logos, no watermarks, no screens with readable "
         "content, no recognisable real people.")


def _log(msg: str) -> None:
    print(f"[studio] ai: {msg}", file=sys.stderr, flush=True)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def config() -> Dict[str, Any]:
    return {
        "key": (os.environ.get("SHORTS_OPENROUTER_API_KEY") or "").strip(),
        "model": (os.environ.get("SHORTS_AI_VIDEO_MODEL") or "heygen/heygen-video-1").strip(),
        "resolution": (os.environ.get("SHORTS_AI_RESOLUTION") or "768p").strip(),
        "price_per_s": _env_float("SHORTS_AI_PRICE_PER_SECOND", 0.015),
        "daily_cap": _env_float("SHORTS_AI_DAILY_CAP_USD", 1.0),
        "short_budget": _env_float("SHORTS_AI_SHORT_BUDGET_USD", 0.5),
        "timeout_s": _env_float("SHORTS_AI_TIMEOUT_S", 600.0),
    }


def clip_seconds(beat_seconds: float) -> int:
    """What to ask the model for: the scene's length, within the model's 5-15 s."""
    return max(MIN_CLIP_S, min(MAX_CLIP_S, math.ceil(beat_seconds - 1e-6)))


def prompt_for(scene: str, palette: str) -> str:
    colours = " and ".join(palette.split("-")) if palette else "neutral"
    return f"{scene.rstrip('. ')}. {STYLE.format(colours=colours)}"


def numeric_seed(seed: str) -> int:
    return int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16)


def plan(pkg: Dict[str, Any], timeline: List[Dict[str, Any]], cfg: Dict[str, Any],
         daily_used: Optional[float], fps: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Decide which scenes fit the budget, in the order they appear (the hook first)."""
    jobs: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    reserved = 0.0
    for i, (beat, slot) in enumerate(zip(pkg["beats"], timeline)):
        if not beat.get("scene"):
            continue
        seconds = clip_seconds(slot["duration"] / fps)
        estimate = round(seconds * cfg["price_per_s"], 4)
        if reserved + estimate > cfg["short_budget"] + 1e-9:
            skipped.append({"beat": i, "status": "skipped",
                            "reason": f"short budget ${cfg['short_budget']:.2f} reached"})
            continue
        if daily_used is not None and daily_used + reserved + estimate > cfg["daily_cap"] + 1e-9:
            skipped.append({"beat": i, "status": "skipped",
                            "reason": f"daily cap ${cfg['daily_cap']:.2f} reached "
                                      f"(${daily_used:.2f} spent today)"})
            continue
        reserved += estimate
        jobs.append({"beat": i, "seconds": seconds, "estimate": estimate,
                     "prompt": prompt_for(beat["scene"], pkg["style"]["palette"])})
    return jobs, skipped


def _headers(url: str, key: str) -> Dict[str, str]:
    """The key goes to OpenRouter only — never to the storage host a result URL points at."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" and (parts.hostname or "").lower() in _AUTH_HOSTS:
        return {"Authorization": f"Bearer {key}"}
    return {}


def daily_spend(client, key: str) -> Optional[float]:
    """USD this key has spent today (UTC), or None when OpenRouter does not say."""
    try:
        resp = client.get(KEY_INFO, headers=_headers(KEY_INFO, key))
        if resp.status_code != 200:
            _log(f"key info: HTTP {resp.status_code}")
            return None
        value = (resp.json().get("data") or {}).get("usage_daily")
        return float(value) if value is not None else None
    except Exception as exc:  # spend info is advisory; the key's own limit is the hard stop
        _log(f"key info: {exc}")
        return None


def _submit(client, cfg: Dict[str, Any], job: Dict[str, Any], seed: int) -> None:
    body = {"model": cfg["model"], "prompt": job["prompt"], "duration": job["seconds"],
            "aspect_ratio": "9:16", "resolution": cfg["resolution"], "seed": seed}
    resp = client.post(API, headers=_headers(API, cfg["key"]), json=body)
    if resp.status_code == 402:
        raise RuntimeError("OpenRouter refused: no credit or the key's limit is reached (402)")
    if resp.status_code >= 400:
        raise RuntimeError(f"submit: HTTP {resp.status_code} {resp.text[:200]}")
    data = resp.json()
    job["id"] = str(data.get("id") or data.get("job_id") or "")
    if not job["id"]:
        raise RuntimeError(f"submit: no job id in {str(data)[:200]}")
    job["poll"] = data.get("polling_url") or f"{API}/{job['id']}"


def _download(client, url: str, key: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with client.stream("GET", url, headers=_headers(url, key)) as resp:
        if resp.status_code >= 400:
            raise RuntimeError(f"download: HTTP {resp.status_code}")
        with open(dest, "wb") as handle:
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError("download: clip larger than expected")
                handle.write(chunk)
    return dest


def _cost(status: Dict[str, Any]) -> Optional[float]:
    value = (status.get("usage") or {}).get("cost")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def render_scenes(pkg: Dict[str, Any], timeline: List[Dict[str, Any]], work: Path, public: Path,
                  seed: str, *, fps: int, offline: bool) -> Dict[str, Any]:
    """Generate the planned scenes and point their timeline slots at the clips.

    Sets ``slot["broll"]`` for every beat that got a clip, so the stock pass
    leaves it alone. Returns the report that goes into the manifest.
    """
    from plugins.shorts.studio import media

    cfg = config()
    wanted = [i for i, b in enumerate(pkg["beats"]) if b.get("scene")]
    report: Dict[str, Any] = {"model": cfg["model"], "requested": len(wanted), "generated": 0,
                              "spent_usd": 0.0, "scenes": []}
    if not wanted:
        return report
    if offline or not cfg["key"]:
        reason = "offline render" if offline else "SHORTS_OPENROUTER_API_KEY is not set"
        report["scenes"] = [{"beat": i, "status": "skipped", "reason": reason} for i in wanted]
        _log(f"{reason}: {len(wanted)} scene(s) stay on stock footage")
        return report

    import httpx

    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        used = daily_spend(client, cfg["key"])
        report["spent_today_before_usd"] = used
        if used is None:
            _log("OpenRouter did not report today's spend; only the per-short budget applies")
        jobs, skipped = plan(pkg, timeline, cfg, used, fps)
        report["scenes"].extend(skipped)
        base_seed = numeric_seed(seed)

        for job in jobs:
            try:
                _submit(client, cfg, job, base_seed)
                job["state"] = "pending"
            except Exception as exc:
                job.update(state="failed", reason=str(exc))
                _log(f"beat {job['beat']}: {exc}")

        deadline = time.time() + cfg["timeout_s"]
        observed_price = 0.0
        while any(j.get("state") == "pending" for j in jobs) and time.time() < deadline:
            time.sleep(POLL_EVERY_S)
            for job in (j for j in jobs if j.get("state") == "pending"):
                try:
                    resp = client.get(job["poll"], headers=_headers(job["poll"], cfg["key"]))
                    status = resp.json() if resp.status_code == 200 else {}
                except Exception as exc:
                    _log(f"beat {job['beat']}: poll: {exc}")
                    continue
                state = status.get("status")
                if state in ("failed", "cancelled", "expired", "error"):
                    job.update(state="failed", cost=_cost(status) or 0.0,
                               reason=f"model said {state}: {str(status.get('error') or '')[:160]}")
                elif state == "completed":
                    job["cost"] = _cost(status)
                    urls = status.get("unsigned_urls") or [f"{API}/{job['id']}/content?index=0"]
                    try:
                        src = _download(client, urls[0], cfg["key"], work / f"ai_src_{job['beat']:02d}.mp4")
                        rel = f"ai/s{job['beat']:02d}.mp4"
                        (public / "ai").mkdir(parents=True, exist_ok=True)
                        slot = timeline[job["beat"]]
                        media.normalize_clip(src, public / rel, slot["duration"] / fps)
                        slot["broll"] = rel
                        job["state"] = "generated"
                    except Exception as exc:
                        job.update(state="failed", reason=f"after generation: {exc}")
                    if job.get("cost"):
                        observed_price = max(observed_price, job["cost"] / job["seconds"])

        for job in jobs:
            if job.get("state") == "pending":
                job.update(state="failed", reason=f"not finished within {cfg['timeout_s']:.0f}s")
            # A clip we paid for but could not use still counts; an unreported
            # cost is taken at the estimate, never at zero.
            spent = job.get("cost")
            if spent is None:
                spent = job["estimate"] if job.get("id") else 0.0
            report["spent_usd"] += spent
            entry = {"beat": job["beat"], "status": job["state"], "id": job.get("id"),
                     "seconds": job["seconds"], "cost_usd": round(spent, 4)}
            if job.get("reason"):
                entry["reason"] = job["reason"]
            report["scenes"].append(entry)
        if observed_price > cfg["price_per_s"]:
            _log(f"observed ${observed_price:.4f}/s, above the configured "
                 f"${cfg['price_per_s']:.4f}/s — raise SHORTS_AI_PRICE_PER_SECOND")
            report["observed_price_per_s"] = round(observed_price, 4)

    report["scenes"].sort(key=lambda s: s["beat"])
    report["generated"] = sum(1 for s in report["scenes"] if s["status"] == "generated")
    report["spent_usd"] = round(report["spent_usd"], 4)
    _log(f"{report['generated']}/{len(wanted)} scenes generated, ${report['spent_usd']:.2f} spent")
    for s in report["scenes"]:
        if s["status"] != "generated":
            _log(f"beat {s['beat']} on stock footage: {s.get('reason')}")
    return report
