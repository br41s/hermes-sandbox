"""Presenter: Lucía (es) or Martín (en), the face and voice of every short.

Tip format (``tip_*`` functions, owner's review 2026-10-11): the voice is a
fixed neural voice per presenter (``package.TIP_VOICES``, Edge TTS, Spain
Spanish for Lucía), so it is the same in every short and costs nothing. The
avatar is paid by the second, so it is generated only for the lines said on
camera (opener, hook, CTA), lip-synced to that voice, in a setting an image
model builds from the presenter's photo and the article's subject. While
the cards play, a loop recorded once per presenter fills the bubble.

Classic format (``render``, below):

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
    SHORTS_PRESENTER_VOICE_ES / _EN     a HeyGen voice id; default VOICES below
    SHORTS_PRESENTER_TIMEOUT_S          default 600, for all clips of one short
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from plugins.shorts.studio import genvideo

CHAT_API = "https://openrouter.ai/api/v1/chat/completions"

PHOTO_DIR = Path(__file__).resolve().parents[3] / "shorts" / "studio" / "presenters"
PRESENTERS = {"es": ("lucia", "Lucía"), "en": ("martin", "Martín")}
# Where each photo's face is, as a fraction of the frame height: Remotion
# zooms in on it and centres it in the bubble (Avatar IV keeps the framing).
FACE_Y = {"lucia": 0.33, "martin": 0.21}
# Avatar IV needs a voice when it is given a script (OpenRouter answers 400
# without one, and lists these). HeyGen stock voices: Amelia (Spanish, female)
# for Lucía, Andrew (English, male) for Martín.
VOICES = {"es": "246cdbf530954380a62109f3107fce0d", "en": "6be73833ef9a4eb0aeee399b8fe9d62b"}
# Charged on 2026-10-10 at $0.05/s: Andrew (en) said 29 words for $0.47, ~3.1
# words a second; Amelia (es) said 55 for $1.05, ~2.6. Only for the estimate;
# the real cost replaces it.
WORDS_PER_SECOND = {"en": 3.0, "es": 2.6}
MOTION = ("Speaks warmly and calmly straight to the camera with a genuine smile, small natural "
          "hand gestures, steady posture, no fast or abrupt movements.")


def config() -> Dict[str, Any]:
    return {
        "model": (os.environ.get("SHORTS_PRESENTER_MODEL") or "heygen/avatar-iv").strip(),
        "price_per_s": genvideo._env_float("SHORTS_PRESENTER_PRICE_PER_SECOND", 0.05),
        "timeout_s": genvideo._env_float("SHORTS_PRESENTER_TIMEOUT_S", 600.0),
        "voice": {lang: (os.environ.get(f"SHORTS_PRESENTER_VOICE_{lang.upper()}") or VOICES[lang]).strip()
                  for lang in PRESENTERS},
    }


def who(lang: str) -> Optional[tuple]:
    return PRESENTERS.get(lang)


def estimate_seconds(script: str, lang: str = "en") -> float:
    # +0.3 s for the breath in and out. A full second per clip over-billed the
    # first two real clips by 25% (11.7 s estimated, 9.4 s charged).
    return round(len(script.split()) / WORDS_PER_SECOND.get(lang, 2.6) + 0.3, 1)


def photo_data_url(slug: str) -> Optional[str]:
    path = PHOTO_DIR / f"{slug}.jpg"
    if not path.exists():
        return None
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def request_body(cfg: Dict[str, Any], script: str, photo: str, lang: str) -> Dict[str, Any]:
    # Options sit directly under provider.options.heygen: that is where
    # OpenRouter looks for voice_id (its 400 names the path).
    heygen = {"voice_id": cfg["voice"][lang], "motion_prompt": MOTION, "expressiveness": "medium"}
    return {
        "model": cfg["model"],
        "prompt": script,
        "aspect_ratio": "9:16",
        "input_references": [{"type": "image_url", "image_url": {"url": photo}}],
        "provider": {"options": {"heygen": heygen}},
    }


def render(pkg: Dict[str, Any], work: Path, budget: Dict[str, Any], *, offline: bool) -> Dict[str, Any]:
    """Generate the presenter clips. Returns {"clips": {beat: Path}, "report": {...}}.

    Classic format only: one clip per beat marked ``presenter``, voiced by
    HeyGen. ``budget`` is genvideo.open_budget()'s dict; what this spends is added to
    ``budget["committed"]`` so the scenes that follow plan with the rest.
    """
    cfg = config()
    scripts = {i: b["vo"] for i, b in enumerate(pkg["beats"]) if b.get("presenter")}
    wanted = list(scripts)
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
        script = scripts[i]
        seconds = estimate_seconds(script, pkg["lang"])
        estimate = round(seconds * cfg["price_per_s"], 4)
        why = genvideo.over_budget(budget, estimate)
        if why:
            report["beats"].append({"beat": i, "status": "skipped", "reason": why})
            continue
        budget["committed"] += estimate
        jobs.append({"beat": i, "estimate": estimate, "seconds": seconds,
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


# ---------------------------------------------------------------------------
# Tip format: a setting, a lip-synced clip of the on-camera lines, the loop

SETTING_MODEL = "google/gemini-3.1-flash-image-preview"
SETTING_PROMPT = (
    "Place the person in this photo in a new scene, keeping the same face, hair, build and age "
    "exactly. Scene: {setting}. Vertical 9:16 photograph, medium shot from the chest up with "
    "the face in the upper third, looking straight at the camera, natural light, realistic, "
    "sharp focus on the face. No text, no letters, no logos, no watermarks anywhere."
)
LIPSYNC_MOTION = ("Talks naturally to the camera, warm and relaxed, small hand gestures, "
                  "keeps the scene and clothing exactly as in the photo.")
LOOP_SECONDS = 5.0
LOOP_MOTION = ("Silent, mouth closed, smiling. Turns the head to look at the left side of the frame, "
               "points there with the index finger, then looks back at the camera and nods slightly.")
# Fraction of the frame height where the face sits in a generated setting
# (the prompt asks for the face in the upper third).
SETTING_FACE_Y = 0.3


def tip_config() -> Dict[str, Any]:
    return {
        "setting_model": (os.environ.get("SHORTS_SETTING_IMAGE_MODEL") or SETTING_MODEL).strip(),
        "setting_estimate": genvideo._env_float("SHORTS_SETTING_IMAGE_ESTIMATE_USD", 0.08),
        "asset_dir": Path(os.environ.get("SHORTS_ASSET_DIR")
                          or Path.home() / ".cache" / "shorts-assets"),
    }


def _file_data_url(path: Path, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def setting_image(client, budget: Dict[str, Any], slug: str, setting: str, dest: Path,
                  report: Dict[str, Any]) -> Optional[Path]:
    """The presenter's photo placed in the article's world, or None (then the plain photo).

    One image request through OpenRouter's chat API with the photo as the
    reference. Cheap (cents), but still counted against the short's budget.
    """
    tcfg = tip_config()
    photo = photo_data_url(slug)
    estimate = tcfg["setting_estimate"]
    why = genvideo.over_budget(budget, estimate)
    if not photo or why:
        report["setting"] = {"status": "skipped", "reason": why or "no photo"}
        return None
    budget["committed"] += estimate
    body = {
        "model": tcfg["setting_model"],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": SETTING_PROMPT.format(setting=setting.rstrip(". "))},
            {"type": "image_url", "image_url": {"url": photo}},
        ]}],
        "modalities": ["image", "text"],
        "image_config": {"aspect_ratio": "9:16"},
        "usage": {"include": True},
    }
    spent = 0.0
    try:
        resp = client.post(CHAT_API, headers=genvideo._headers(CHAT_API, budget["cfg"]["key"]), json=body)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {resp.text[:800]}")
        data = resp.json()
        spent = float((data.get("usage") or {}).get("cost") or estimate)
        images = ((data.get("choices") or [{}])[0].get("message") or {}).get("images") or []
        url = ((images[0] if images else {}).get("image_url") or {}).get("url") or ""
        if not url.startswith("data:image/"):
            raise RuntimeError("no image in the answer")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(base64.b64decode(url.split(",", 1)[1]))
        report["setting"] = {"status": "generated", "model": tcfg["setting_model"],
                             "cost_usd": round(spent, 4)}
        return dest
    except Exception as exc:
        report["setting"] = {"status": "failed", "reason": str(exc)[:800], "cost_usd": round(spent, 4)}
        genvideo._log(f"presenter setting: {exc}")
        return None
    finally:
        budget["committed"] += spent - estimate
        report["spent_usd"] = round(report.get("spent_usd", 0.0) + spent, 4)


# How Avatar IV is handed a voice track instead of a script. OpenRouter's 400
# for a script without a voice says "supply an audio input reference instead";
# the exact shape is not documented, so each is tried in turn — a refused
# request costs nothing.
AUDIO_SHAPES = (
    lambda url, b64: {"type": "audio_url", "audio_url": {"url": url}},
    lambda url, b64: {"type": "input_audio", "input_audio": {"data": b64, "format": "mp3"}},
)


def lipsync_body(cfg: Dict[str, Any], image_url: str, audio_ref: Dict[str, Any], motion: str) -> Dict[str, Any]:
    return {
        "model": cfg["model"],
        "prompt": motion,
        "aspect_ratio": "9:16",
        "input_references": [{"type": "image_url", "image_url": {"url": image_url}}, audio_ref],
        "provider": {"options": {"heygen": {"motion_prompt": motion, "expressiveness": "medium"}}},
    }


def lipsync(client, budget: Dict[str, Any], image: Path, audio_mp3: Path, seconds: float, work: Path,
            name: str, motion: str, report: Dict[str, Any]) -> Optional[Path]:
    """Avatar IV: the image says exactly ``audio_mp3``. Returns the clip or None."""
    cfg = config()
    estimate = round(seconds * cfg["price_per_s"], 4)
    why = genvideo.over_budget(budget, estimate)
    entry: Dict[str, Any] = {"part": name, "seconds": round(seconds, 2)}
    report.setdefault("clips", []).append(entry)
    if why:
        entry.update(status="skipped", reason=why)
        return None
    budget["committed"] += estimate
    image_url = _file_data_url(image, "image/png" if image.suffix == ".png" else "image/jpeg")
    b64 = base64.b64encode(audio_mp3.read_bytes()).decode("ascii")
    audio_url = "data:audio/mpeg;base64," + b64
    job: Dict[str, Any] = {"beat": 0, "estimate": estimate, "seconds": seconds}
    errors = []
    for shape in AUDIO_SHAPES:
        try:
            genvideo.submit_body(client, budget["cfg"]["key"], job,
                                 lipsync_body(cfg, image_url, shape(audio_url, b64), motion))
            job["state"] = "pending"
            break
        except Exception as exc:
            errors.append(str(exc))
            if "HTTP 400" not in str(exc):
                break
    if job.get("state") != "pending":
        job.update(state="failed", reason=" | ".join(errors)[:1600])
    else:
        genvideo.wait_for(client, budget["cfg"]["key"], [job], time.time() + cfg["timeout_s"], work,
                          prefix=f"lipsync_{name}")
    spent = genvideo.settle(job, budget)
    report["spent_usd"] = round(report.get("spent_usd", 0.0) + spent, 4)
    entry.update(status=job["state"], id=job.get("id"), cost_usd=round(spent, 4))
    if job.get("reason"):
        entry["reason"] = job["reason"]
        genvideo._log(f"presenter {name}: {job['reason']}")
    return job.get("src") if job["state"] == "generated" else None


def bubble_loop(client, budget: Dict[str, Any], slug: str, work: Path, report: Dict[str, Any]) -> Optional[Path]:
    """The bubble's loop: recorded once per presenter, then reused by every short.

    Looked up in the repo (``presenters/assets/<slug>-loop.mp4``), then the
    asset cache (kept between Actions runs), and generated only when both miss:
    Avatar IV on the plain photo with silent audio and a pointing gesture.
    """
    from plugins.shorts.studio import media

    name = f"{slug}-loop.mp4"
    for path in (PHOTO_DIR / "assets" / name, tip_config()["asset_dir"] / name):
        if path.exists() and path.stat().st_size > 0:
            report["loop"] = {"status": "reused", "path": str(path)}
            return path
    photo = PHOTO_DIR / f"{slug}.jpg"
    if client is None or not photo.exists():
        report["loop"] = {"status": "skipped", "reason": "offline or no photo"}
        return None
    silence = media.silence(work / "loop_silence.wav", LOOP_SECONDS)
    mp3 = media.to_mp3(silence, work / "loop_silence.mp3")
    clip = lipsync(client, budget, photo, mp3, LOOP_SECONDS, work, "loop", LOOP_MOTION, report)
    if clip is None:
        report["loop"] = {"status": "failed"}
        return None
    dest = tip_config()["asset_dir"] / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    media.boomerang(clip, dest)       # forward then back: loops without a jump
    report["loop"] = {"status": "generated", "path": str(dest)}
    return dest
