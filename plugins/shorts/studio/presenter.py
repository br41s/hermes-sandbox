"""Presenters: Lucía and Martín, the faces and voices of every short.

Tip format (the second half of this file, owner's design 2026-10-11): Lucía
opens on camera in her HeyGen voice, in a setting an image model builds for
the article with Martín at work behind her, and hands him the word; Martín
nods and narrates the rest in his free neural voice (``package.TIP_VOICES``)
from a bubble that plays a loop of him recorded once.

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
# Tip format: two presenters (owner's design, 2026-10-11)
#
# Lucía opens on camera in a setting built for the article, with Martín busy
# in the background: the joke, the hook, and the line that hands him the word.
# Those takes are Avatar IV with her HeyGen voice, the only part paid by the
# second. The camera then pushes in on Martín in the still setting, and he
# narrates the cards and the CTA in his free neural voice while a loop of him,
# recorded once by the owner, plays in the bubble.

HOST = ("lucia", "Lucía")
PARTNER = ("martin", "Martín")
# Lucía's HeyGen voices, chosen by the owner: Spain Spanish and English.
# The first Spanish one (a78e0a4d…) sounded too young on the avatar (owner, 2026-10-11).
HOST_VOICES = {"es": "ec971490059245d3953fd895ee37ab09", "en": "47aff89a92ce42edb3301f74e5aa7f6b"}
# Her lines in a render with no key (the push smoke test): a free voice of the same kind.
HOST_FALLBACK_VOICES = {"es": "es-ES-ElviraNeural", "en": "en-US-AvaMultilingualNeural"}

# Tried in order until one answers with an image. Gemini refused both paid
# tests with a 403 "violation of provider Terms Of Service" and no provider
# named; FLUX.2 Pro answered. A refused request costs nothing, so the chain
# moves on.
SETTING_MODELS = ("black-forest-labs/flux.2-pro", "google/gemini-3.1-flash-image-preview",
                  "openai/gpt-5-image")
# Image-only models answer no text and refuse a request that asks for it.
_TEXT_AND_IMAGE = ("google/", "openai/")
# General rules only (owner, 2026-10-11): every short is a different scene;
# what holds for all of them is that the picture obeys physics. Avatar IV
# moves her hands and leaves the rest of the picture still, so anything that
# would have to move with her hands rests on a surface instead (the first
# paid test left a tray of bread floating in the air).
SETTING_PROMPT = (
    "Two fictional presenters from the reference photos: the woman in the first photo and the man "
    "in the second, both recognisable. Scene: {setting}. The woman is in the foreground, medium "
    "shot from the chest up, face in the upper third of the frame, looking straight at the camera. "
    "The man is further back, busy with the work of this place, smaller in the frame but his face "
    "visible. Everything obeys physics; the woman's hands are free, and anything near her rests on "
    "a surface. Vertical 9:16 photograph, natural light, realistic, sharp focus on the woman. No "
    "text, no letters, no logos, no watermarks anywhere."
)
# Her hands move with her words and nothing else in the picture does, so the
# setting keeps her hands free and objects on surfaces (SETTING_PROMPT).
TAKE_MOTION = ("Speaks warmly and naturally straight to the camera, small, calm hand gestures with "
               "empty hands, steady posture.")
# She hands over by turning to Martín and saying the line to him, the way
# a presenter turns to a colleague; a frozen last frame with a zoom read as
# a stall (owner's review, 2026-10-13). Her face stays visible for the lips.
HANDOFF_MOTION = ("Turns her head and shoulders partway towards the man working behind her and says "
                  "the line to him, with a relaxed open-hand gesture in his direction, her face still "
                  "visible to the camera; she stays turned towards him at the end.")
# Her takes play this much faster, picture and voice together: at 1.0 she
# read slowly to the owner (2026-10-13). HeyGen's voice takes no rate.
TAKE_TEMPO = 1.12

# How each presenter's recorded loop sits in the bubble: its scale against a
# 1080x1920 frame and where to centre (fractions of the loop's width and
# height). Martín's (owner's recording, Google Flow, 2026-10-12) is
# framed like his photo; he looks up to the left, towards the cards, and
# points up with his hand on the left, so the crop sits left of his face to
# keep the hand in. Lucía's (same day) is framed tighter, for when she narrates.
LOOP_FRAMING = {"martin": {"scale": 0.34, "x": 0.40, "y": 0.24},
                "lucia": {"scale": 0.30, "x": 0.34, "y": 0.30}}

# Fraction of the frame height where the woman's face sits in a generated
# setting (the prompt asks for the upper third).
SETTING_FACE_Y = 0.3


def tip_config() -> Dict[str, Any]:
    return {
        "setting_models": [m.strip() for m in (os.environ.get("SHORTS_SETTING_IMAGE_MODEL") or "").split(",")
                           if m.strip()] or list(SETTING_MODELS),
        "setting_estimate": genvideo._env_float("SHORTS_SETTING_IMAGE_ESTIMATE_USD", 0.10),
        "host_voice": {lang: (os.environ.get(f"SHORTS_HOST_VOICE_{lang.upper()}") or v).strip()
                       for lang, v in HOST_VOICES.items()},
        "asset_dir": Path(os.environ.get("SHORTS_ASSET_DIR")
                          or Path.home() / ".cache" / "shorts-assets"),
    }


def _file_data_url(path: Path) -> str:
    mime = "image/png" if path.suffix == ".png" else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def setting_image(client, budget: Dict[str, Any], setting: str, dest: Path,
                  report: Dict[str, Any]) -> Optional[Path]:
    """Lucía in front and Martín at work behind her, in the article's world; or None.

    One image request through OpenRouter's chat API with both photos as the
    references, per model in ``setting_models`` until one answers. Cheap
    (cents), but still counted against the short's budget.
    """
    tcfg = tip_config()
    photos = [photo_data_url(HOST[0]), photo_data_url(PARTNER[0])]
    if not all(photos):
        report["setting"] = {"status": "skipped", "reason": "a presenter photo is missing"}
        return None
    prompt = SETTING_PROMPT.format(setting=setting.rstrip(". "))
    tried = []
    for model in tcfg["setting_models"]:
        why = genvideo.over_budget(budget, tcfg["setting_estimate"])
        if why:
            report["setting"] = {"status": "skipped", "reason": why, "tried": tried}
            return None
        image, outcome = _setting_attempt(client, budget, model, prompt, photos, tcfg["setting_estimate"])
        tried.append({"model": model, **outcome})
        report["spent_usd"] = round(report.get("spent_usd", 0.0) + outcome["cost_usd"], 4)
        if image:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(image)
            report["setting"] = {"status": "generated", "model": model,
                                 "cost_usd": outcome["cost_usd"], "tried": tried}
            genvideo._log(f"presenter setting: {model}, ${outcome['cost_usd']:.2f}")
            return dest
        genvideo._log(f"presenter setting ({model}): {outcome['reason']}")
    report["setting"] = {"status": "failed", "tried": tried}
    return None


def _setting_attempt(client, budget: Dict[str, Any], model: str, prompt: str, photos: List[str],
                     estimate: float):
    """One model's try: (image bytes, outcome) or (None, outcome with the reason)."""
    budget["committed"] += estimate
    body = {
        "model": model,
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": url}} for url in photos]}],
        "modalities": ["image", "text"] if model.startswith(_TEXT_AND_IMAGE) else ["image"],
        "image_config": {"aspect_ratio": "9:16"},
        "usage": {"include": True},
    }
    spent = 0.0
    try:
        resp = client.post(CHAT_API, headers=genvideo._headers(CHAT_API, budget["cfg"]["key"]), json=body)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {resp.text[:500]}")
        data = resp.json()
        spent = float((data.get("usage") or {}).get("cost") or estimate)
        images = ((data.get("choices") or [{}])[0].get("message") or {}).get("images") or []
        url = ((images[0] if images else {}).get("image_url") or {}).get("url") or ""
        if not url.startswith("data:image/"):
            raise RuntimeError("no image in the answer")
        return base64.b64decode(url.split(",", 1)[1]), {"status": "generated", "cost_usd": round(spent, 4)}
    except Exception as exc:
        return None, {"status": "failed", "reason": str(exc)[:500], "cost_usd": round(spent, 4)}
    finally:
        budget["committed"] += spent - estimate


def host_takes(client, budget: Dict[str, Any], image: Path, lines: Dict[int, str], lang: str,
               handoff: Optional[int], work: Path, report: Dict[str, Any]) -> Dict[int, Path]:
    """Lucía says each line to camera, one Avatar IV take per line, in her HeyGen voice.

    One take per line gives each beat its exact length and audio. Every take
    is planned before any is sent, so the budget refuses the short cleanly
    instead of leaving it with half its lines. Returns {beat: clip}.
    """
    cfg, tcfg = config(), tip_config()
    voice = tcfg["host_voice"].get(lang)
    image_url = _file_data_url(image)
    jobs: List[Dict[str, Any]] = []
    total = 0.0
    for i, line in lines.items():
        seconds = estimate_seconds(line, lang)
        estimate = round(seconds * cfg["price_per_s"], 4)
        jobs.append({"beat": i, "estimate": estimate, "seconds": seconds, "line": line})
        total += estimate
    why = genvideo.over_budget(budget, total) if voice else f"no HeyGen voice for {lang!r}"
    if why:
        report["takes"] = [{"beat": j["beat"], "status": "skipped", "reason": why} for j in jobs]
        genvideo._log(f"presenter takes: {why}")
        return {}
    budget["committed"] += total
    # One take at a time: OpenRouter holds credit for every video in progress,
    # and with all three sent at once it refused the third "Insufficient
    # credits" (402) on three shorts in a row on 2026-10-13, the account in credit.
    for n, job in enumerate(jobs):
        if n and jobs[n - 1].get("state") != "generated":
            # The short cannot pass QA without every line of hers: stop paying.
            job.update(state="skipped", reason=f"take {jobs[n - 1]['beat']} was not made")
            continue
        heygen = {"voice_id": voice, "motion_prompt": HANDOFF_MOTION if job["beat"] == handoff else TAKE_MOTION,
                  "expressiveness": "medium"}
        body = {"model": cfg["model"], "prompt": job.pop("line"), "aspect_ratio": "9:16",
                "input_references": [{"type": "image_url", "image_url": {"url": image_url}}],
                "provider": {"options": {"heygen": heygen}}}
        try:
            genvideo.submit_body(client, budget["cfg"]["key"], job, body)
            job["state"] = "pending"
        except Exception as exc:
            job.update(state="failed", reason=str(exc))
            continue
        genvideo.wait_for(client, budget["cfg"]["key"], [job], time.time() + cfg["timeout_s"], work, prefix="take")
    takes: Dict[int, Path] = {}
    report["takes"] = []
    for job in jobs:
        spent = genvideo.settle(job, budget)
        report["spent_usd"] = round(report.get("spent_usd", 0.0) + spent, 4)
        entry = {"beat": job["beat"], "status": job["state"], "id": job.get("id"), "cost_usd": round(spent, 4)}
        if job.get("reason"):
            entry["reason"] = job["reason"]
            genvideo._log(f"presenter take {job['beat']}: {job['reason']}")
        if job["state"] == "generated":
            takes[job["beat"]] = job["src"]
            genvideo._log(f"presenter take {job['beat']}: ${spent:.2f} (estimated ${job['estimate']:.2f})")
        report["takes"].append(entry)
    return takes


def bubble_loop(report: Dict[str, Any], slug: str = PARTNER[0]) -> Optional[Path]:
    """The narrator's loop for the bubble, recorded once by the owner and committed.

    Never generated here (owner, 2026-10-12): the image-to-video models that
    accept a realistic face cost more than the rest of the short, and the loop
    is the same in every short. ``presenters/assets/<slug>-loop.mp4`` first,
    then the asset cache; without either, the bubble shows the photo.
    """
    name = f"{slug}-loop.mp4"
    for path in (PHOTO_DIR / "assets" / name, tip_config()["asset_dir"] / name):
        if path.exists() and path.stat().st_size > 0:
            report["loop"] = {"status": "reused", "path": str(path)}
            return path
    report["loop"] = {"status": "missing", "reason": f"add presenters/assets/{name}"}
    return None
