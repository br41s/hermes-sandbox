"""Presenter beats: Lucía (es) or Martín (en) says the hook and the CTA to camera.

Runs in Actions, before the timeline: a presenter clip carries its own voice,
so its length sets the beat's length, exactly like an avatar clip.

Model: HeyGen Avatar IV through OpenRouter's video API (``heygen/avatar-iv``):
one reference photo and the script; HeyGen voices it and animates the face,
gestures and lips. The photos are fixed brand assets in
``shorts/studio/presenters/`` (vertical, front-facing, no text on clothes).

Budget: it shares the generated-video budget with ``genvideo`` (per short and
per day, see there) and plans first: the presenter is the part of the short
people stay for. Each clip is estimated from its word count before any
request. Nothing here can fail a render: a refused, failed or late clip
leaves the beat to the normal voice-over and template, and the reason goes
into the manifest.

Config (Actions env), on top of genvideo's key and caps:

    SHORTS_PRESENTER_MODEL              default heygen/avatar-iv
    SHORTS_PRESENTER_PRICE_PER_SECOND   default 0.05
    SHORTS_PRESENTER_VOICE_ES / _EN     a HeyGen voice id; unset = HeyGen's choice
    SHORTS_PRESENTER_TIMEOUT_S          default 600, for all clips of one short
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from plugins.shorts.studio import genvideo

PHOTO_DIR = Path(__file__).resolve().parents[3] / "shorts" / "studio" / "presenters"
PRESENTERS = {"es": ("lucia", "Lucía"), "en": ("martin", "Martín")}
WORDS_PER_SECOND = 2.4       # HeyGen voices, measured loosely; only for the estimate
MOTION = ("Speaks warmly and calmly straight to the camera with a genuine smile, small natural "
          "hand gestures, steady posture, no fast or abrupt movements.")


def config() -> Dict[str, Any]:
    return {
        "model": (os.environ.get("SHORTS_PRESENTER_MODEL") or "heygen/avatar-iv").strip(),
        "price_per_s": genvideo._env_float("SHORTS_PRESENTER_PRICE_PER_SECOND", 0.05),
        "timeout_s": genvideo._env_float("SHORTS_PRESENTER_TIMEOUT_S", 600.0),
        "voice": {lang: (os.environ.get(f"SHORTS_PRESENTER_VOICE_{lang.upper()}") or "").strip()
                  for lang in PRESENTERS},
    }


def who(lang: str) -> Optional[tuple]:
    return PRESENTERS.get(lang)


def estimate_seconds(script: str) -> float:
    return round(len(script.split()) / WORDS_PER_SECOND + 1.0, 1)


def photo_data_url(slug: str) -> Optional[str]:
    path = PHOTO_DIR / f"{slug}.jpg"
    if not path.exists():
        return None
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def request_body(cfg: Dict[str, Any], script: str, photo: str, lang: str) -> Dict[str, Any]:
    params: Dict[str, Any] = {"motion_prompt": MOTION, "expressiveness": "medium"}
    if cfg["voice"].get(lang):
        params["voice_id"] = cfg["voice"][lang]
    return {
        "model": cfg["model"],
        "prompt": script,
        "aspect_ratio": "9:16",
        "input_references": [{"type": "image_url", "image_url": {"url": photo}}],
        "provider": {"options": {"heygen": {"parameters": params}}},
    }


def render(pkg: Dict[str, Any], work: Path, budget: Dict[str, Any], *, offline: bool) -> Dict[str, Any]:
    """Generate the presenter clips. Returns {"clips": {beat: Path}, "report": {...}}.

    ``budget`` is genvideo.open_budget()'s dict; what this spends is added to
    ``budget["committed"]`` so the scenes that follow plan with the rest.
    """
    cfg = config()
    wanted = [i for i, b in enumerate(pkg["beats"]) if b.get("presenter")]
    person = who(pkg["lang"])
    report: Dict[str, Any] = {"model": cfg["model"], "presenter": person[1] if person else None,
                              "requested": len(wanted), "generated": 0, "spent_usd": 0.0, "beats": []}
    clips: Dict[int, Path] = {}
    if not wanted:
        return {"clips": clips, "report": report}
    photo = photo_data_url(person[0]) if person else None
    reason = ("offline render" if offline else
              "SHORTS_OPENROUTER_API_KEY is not set" if not budget["cfg"]["key"] else
              f"no presenter for language {pkg['lang']!r}" if not person else
              f"photo {person[0]}.jpg missing from shorts/studio/presenters" if not photo else None)
    if reason:
        report["beats"] = [{"beat": i, "status": "skipped", "reason": reason} for i in wanted]
        genvideo._log(f"presenter: {reason}; {len(wanted)} beat(s) use the voice-over")
        return {"clips": clips, "report": report}

    import httpx

    gcfg = budget["cfg"]
    jobs: List[Dict[str, Any]] = []
    for i in wanted:
        script = pkg["beats"][i]["vo"]
        estimate = round(estimate_seconds(script) * cfg["price_per_s"], 4)
        why = genvideo.over_budget(budget, estimate)
        if why:
            report["beats"].append({"beat": i, "status": "skipped", "reason": why})
            continue
        budget["committed"] += estimate
        jobs.append({"beat": i, "estimate": estimate, "seconds": estimate_seconds(script),
                     "body": request_body(cfg, script, photo, pkg["lang"])})

    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        for job in jobs:
            try:
                genvideo.submit_body(client, gcfg["key"], job, job.pop("body"))
                job["state"] = "pending"
            except Exception as exc:
                job.update(state="failed", reason=str(exc))
                genvideo._log(f"presenter beat {job['beat']}: {exc}")
        deadline = time.time() + cfg["timeout_s"]
        genvideo.wait_for(client, gcfg["key"], jobs, deadline, work, prefix="presenter_src")

    for job in jobs:
        spent = genvideo.settle(job, budget)
        report["spent_usd"] += spent
        entry = {"beat": job["beat"], "status": job["state"], "id": job.get("id"),
                 "cost_usd": round(spent, 4)}
        if job.get("reason"):
            entry["reason"] = job["reason"]
        if job["state"] == "generated":
            clips[job["beat"]] = job["src"]
        report["beats"].append(entry)
    report["beats"].sort(key=lambda b: b["beat"])
    report["generated"] = len(clips)
    report["spent_usd"] = round(report["spent_usd"], 4)
    genvideo._log(f"presenter: {report['generated']}/{len(wanted)} clips by {person[1]}, "
                  f"${report['spent_usd']:.2f} spent")
    for b in report["beats"]:
        if b["status"] != "generated":
            genvideo._log(f"presenter beat {b['beat']} on the voice-over: {b.get('reason')}")
    return {"clips": clips, "report": report}
