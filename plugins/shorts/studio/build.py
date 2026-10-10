"""Render one short package into finished deliverables. Runs in GitHub Actions.

    python -m plugins.shorts.studio.build --package pkg.json --out out/ \
        --remotion shorts/studio/remotion

Outputs in ``--out``:

    master.mp4       1080x1920, 30fps, h264/aac, -14 LUFS — Reels, Shorts, FB, X
    story.mp4        <=59.9s cut at a beat boundary, faded — Instagram Story
    cover.jpg        titled cover (IG Reel cover)
    thumb.jpg        titled thumbnail (YouTube)
    captions.srt     sidecar captions (YouTube)
    manifest.json    everything Hermes needs to publish, incl. QA
    qa.json          the QA report on its own

Exit code 0 = rendered and QA passed; 2 = rendered but QA failed (the
outputs are still written, so the report can say why); 1 = could not render.

Order, and why:
1. Voice first. Every scene is as long as its line takes to say, so nothing
   visual can be timed before the audio exists.
   The presenter's lines come first of all (``presenter``, paid): a talking
   clip brings its own voice, so its length is the beat's.
2. Generated scenes for the beats that ask for one (``genvideo``, paid and
   capped, sharing the presenter's budget), then free footage for every other
   beat, and music — all trimmed to the scene lengths the voice fixed.
3. Remotion renders picture only (muted); audio is mixed by ffmpeg, where
   ducking and two-pass loudness are reliable.
4. QA reads the finished file, never the inputs.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from plugins.shorts.studio import genvideo, media, presenter, qa, sources, voice
from plugins.shorts.studio.package import PackageError, validate_package

FPS = media.FPS
LEAD_IN = 0.20        # silence before the first word
GAP = 0.28            # breath between beats
CTA_TAIL = 1.4        # the CTA stays on screen after the last word
AVATAR_MAX = 12.0     # seconds of avatar clip used per beat
PRESENTER_MAX = 15.0  # a presenter line is <=24 words; anything longer is a model fault
TAKE_MAX = 40.0       # the tip take is <=55 words (~19 s); anything past this is a model fault
STORY_LIMIT = 59.5

BRANDS = {
    "biglobster": {"name": "BigLobster", "site": "biglobster.top"},
}

FONT_FILE = "inter-latin-wght-normal.woff2"


class BuildError(RuntimeError):
    pass


def _log(msg: str) -> None:
    print(f"[studio] {msg}", flush=True)


def _frames(seconds: float) -> int:
    return int(round(seconds * FPS))


# Hosts that may receive the workflow token. Exact match on the parsed host —
# a substring test would hand it to "github.com.evil.example" too. The asset
# download 302s to githubusercontent storage, and httpx drops Authorization on
# that cross-origin hop, so the token never needs to go there.
_GITHUB_TOKEN_HOSTS = {"api.github.com", "github.com"}


def _github_headers(url: str) -> Dict[str, str]:
    """Private-repo release assets need the workflow token to download."""
    token = os.environ.get("GITHUB_TOKEN", "")
    parsed = urllib.parse.urlparse(url)
    if token and parsed.scheme == "https" and (parsed.hostname or "").lower() in _GITHUB_TOKEN_HOSTS:
        return {"Authorization": f"Bearer {token}", "Accept": "application/octet-stream"}
    return {}


def _download_avatar(url: str, dest: Path) -> Path:
    import httpx

    headers = {"User-Agent": sources.USER_AGENT, **_github_headers(url)}
    with httpx.Client(timeout=120, follow_redirects=True, headers=headers) as client, \
            client.stream("GET", url) as resp:
        if resp.status_code != 200:
            raise BuildError(f"avatar clip {url}: HTTP {resp.status_code}")
        written = 0
        with open(dest, "wb") as handle:
            for chunk in resp.iter_bytes(1 << 16):
                written += len(chunk)
                if written > sources.MAX_DOWNLOAD_BYTES:
                    raise BuildError(f"avatar clip {url} is too large")
                handle.write(chunk)
    return dest


# ---------------------------------------------------------------------------
# 1. Voice and timeline
# ---------------------------------------------------------------------------

def build_timeline(pkg: Dict[str, Any], work: Path, public: Path, *, fake_voice: bool,
                   presenter_clips: Optional[Dict[int, Path]] = None) -> Dict[str, Any]:
    """Synthesize every beat and lay them end to end on frame boundaries.

    A beat in ``presenter_clips`` is voiced by its clip; its video runs from
    the first frame of the beat, so it gets no lead-in silence (lips and voice
    stay together) and holds its last frame through the tail.
    """
    presenter_clips = presenter_clips or {}
    beats = pkg["beats"]
    segments: List[Path] = []
    timeline: List[Dict[str, Any]] = []
    words: List[Dict[str, Any]] = []
    cursor_frames = 0

    for i, beat in enumerate(beats):
        raw = work / f"vo_{i:02d}.mp3"
        clip_rel: Optional[str] = None
        talking = presenter_clips.get(i)
        if talking is not None and not media.has_audio(talking):
            _log(f"presenter beat {i}: the clip has no audio track; using the voice-over")
            talking = None
        if talking is not None:
            wav = media.extract_audio(talking, work / f"vo_{i:02d}.wav")
            speech = min(media.duration(talking), PRESENTER_MAX)
            beat_words = voice.spread_words(beat["vo"], speech)
        elif beat["kind"] == "avatar":
            src = _download_avatar(beat["clip_url"], work / f"avatar_src_{i:02d}.mp4")
            if not media.has_audio(src):
                raise BuildError(f"beat {i}: the avatar clip has no audio track")
            clip_len = min(media.duration(src), AVATAR_MAX)
            (public / "avatar").mkdir(parents=True, exist_ok=True)
            clip_rel = f"avatar/a{i:02d}.mp4"
            media.normalize_clip(src, public / clip_rel, clip_len)
            wav = media.extract_audio(src, work / f"vo_{i:02d}.wav")
            speech = clip_len
            beat_words = voice.spread_words(beat["vo"], speech)
        else:
            if fake_voice:
                beat_words = voice.fake_synthesize(beat["vo"], raw)
            else:
                beat_words = voice.synthesize(beat["vo"], pkg["voice"], pkg["rate"], raw)
            wav = media.to_wav(raw, work / f"vo_{i:02d}.wav")
            speech = media.duration(wav)

        voice.attach_raw(beat_words, beat["vo"])
        lead = LEAD_IN if i == 0 and talking is None else 0.0
        tail = CTA_TAIL if i == len(beats) - 1 else GAP
        frames = _frames(lead + speech + tail)
        seconds = frames / FPS
        presenter_rel: Optional[str] = None
        if talking is not None:
            (public / "presenter").mkdir(parents=True, exist_ok=True)
            presenter_rel = f"presenter/p{i:02d}.mp4"
            media.normalize_clip(talking, public / presenter_rel, seconds, hold=True)
        segments.append(media.pad_segment(wav, work / f"seg_{i:02d}.wav", lead, seconds))

        start_ms = cursor_frames / FPS * 1000 + lead * 1000
        for w in beat_words:
            text = voice.clean_word(str(w["text"]))
            if text:
                words.append({"text": text, "raw": str(w.get("raw") or text),
                              "startMs": round(start_ms + w["start"] * 1000),
                              "endMs": round(start_ms + w["end"] * 1000), "beat": i})
        timeline.append({"from": cursor_frames, "duration": frames, "avatar": clip_rel,
                         "presenter": presenter_rel})
        cursor_frames += frames

    voice_raw = media.concat_wavs(segments, work / "voice_raw.wav")
    voice_polished = media.polish_voice(voice_raw, work / "voice.wav")
    voice.paginate(words)
    return {"timeline": timeline, "words": words, "frames": cursor_frames, "voice": voice_polished}


def _weight(text: str) -> int:
    return sum(max(2, len(t)) for t in text.split())


def plan_cuts(lines: List[str], total: float, pauses: List[Tuple[float, float]],
              *, snap: float = 1.2) -> List[Tuple[float, float]]:
    """Where each line starts and ends inside one take: (start, end) seconds.

    The take is one recording of every line in order, so where one line ends
    has to be found. Each cut is first placed by the share of the script said
    so far, then moved to the middle of the nearest pause within ``snap``
    seconds (a sentence ends in a breath). Pure, so it is tested directly.
    """
    speech_start = next((e for s0, e in pauses if s0 <= 0.05), 0.0)
    speech_end = next((s0 for s0, e in reversed(pauses) if e >= total - 0.05), total)
    if speech_end <= speech_start:
        speech_start, speech_end = 0.0, total
    weights = [_weight(t) for t in lines]
    whole = float(sum(weights)) or 1.0
    mids = [(a + b) / 2 for a, b in pauses if a > speech_start + 0.1 and b < speech_end - 0.1]
    cuts: List[float] = []
    done = 0.0
    for w in weights[:-1]:
        done += w
        guess = speech_start + (speech_end - speech_start) * done / whole
        floor = (cuts[-1] if cuts else speech_start) + 0.5
        near = [m for m in mids if abs(m - guess) <= snap and m > floor]
        cut = min(near, key=lambda m: abs(m - guess)) if near else max(guess, floor)
        cuts.append(cut)
    edges = [0.0] + cuts + [total]
    return [(edges[i], edges[i + 1]) for i in range(len(lines))]


def build_take_timeline(pkg: Dict[str, Any], work: Path, public: Path, take: Path) -> Dict[str, Any]:
    """The tip format's timeline: one take, cut into the beats at its pauses.

    The take's own audio is the whole voice track, untouched: beats only say
    which picture shows while each line is said. The video holds its last
    frame through the CTA's tail.
    """
    beats = pkg["beats"]
    wav = media.extract_audio(take, work / "take.wav")
    speech_total = min(media.duration(wav), TAKE_MAX)
    spans = plan_cuts([b["vo"] for b in beats], speech_total, media.silences(wav))
    timeline: List[Dict[str, Any]] = []
    words: List[Dict[str, Any]] = []
    cursor = 0
    for i, (beat, (start, end)) in enumerate(zip(beats, spans)):
        stop = end + (CTA_TAIL if i == len(beats) - 1 else 0.0)
        frames = max(_frames(stop) - cursor, FPS // 2)
        beat_words = voice.spread_words(beat["vo"], end - start)
        voice.attach_raw(beat_words, beat["vo"])
        for w in beat_words:
            text = voice.clean_word(str(w["text"]))
            if text:
                words.append({"text": text, "raw": str(w.get("raw") or text),
                              "startMs": round((start + w["start"]) * 1000),
                              "endMs": round((start + w["end"]) * 1000), "beat": i})
        timeline.append({"from": cursor, "duration": frames, "avatar": None, "presenter": None,
                         "show": beat.get("show")})
        cursor += frames
    seconds = cursor / FPS
    (public / "presenter").mkdir(parents=True, exist_ok=True)
    media.normalize_clip(take, public / "presenter" / "take.mp4", seconds, hold=True)
    padded = media.pad_segment(wav, work / "voice_raw.wav", 0.0, seconds)
    voice_polished = media.polish_voice(padded, work / "voice.wav")
    voice.paginate(words)
    out = {"timeline": timeline, "words": words, "frames": cursor, "voice": voice_polished,
           "take": "presenter/take.mp4", "photo": None}
    person = presenter.who(pkg["lang"])
    photo = presenter.PHOTO_DIR / f"{person[0]}.jpg" if person else None
    if photo and photo.exists():     # the cover shows who the viewer will hear
        shutil.copyfile(photo, public / "presenter" / "photo.jpg")
        out["photo"] = "presenter/photo.jpg"
    return out


# ---------------------------------------------------------------------------
# 2. Footage and music
# ---------------------------------------------------------------------------

def gather_broll(pkg: Dict[str, Any], timeline: List[Dict[str, Any]], work: Path, public: Path,
                 seed: str, *, offline: bool) -> List[Dict[str, Any]]:
    """One clip per distinct query; beats that share a query continue the clip.

    A beat that already has a generated scene keeps it.
    """
    used: List[Dict[str, Any]] = []
    by_query: Dict[str, Dict[str, Any]] = {}
    avoid = list(pkg.get("avoid_broll_ids") or [])
    query: Optional[str] = None
    (public / "broll").mkdir(parents=True, exist_ok=True)

    for i, (beat, slot) in enumerate(zip(pkg["beats"], timeline)):
        slot.setdefault("broll", None)
        if beat["kind"] == "avatar" or slot.get("presenter"):
            continue
        query = beat.get("broll") or query
        if slot["broll"] or not query or offline:
            continue
        entry = by_query.get(query)
        if entry is None:
            clip = sources.find_broll(query, avoid + [u["id"] for u in used], f"{seed}-{query}")
            entry = {"clip": clip, "path": None, "offset": 0.0, "length": 0.0}
            if clip:
                try:
                    src = sources.download(clip["url"], work / f"broll_src_{len(by_query):02d}.mp4",
                                           fallback_url=clip.get("fallback_url"))
                    entry["path"] = src
                    entry["length"] = media.duration(src)
                    used.append({"id": clip["id"], "source": clip["source"], "query": query,
                                 "url": clip["url"]})
                except Exception as exc:  # footage is decoration: degrade, never fail
                    _log(f"broll '{query}': {exc}")
            else:
                _log(f"broll '{query}': no clip found")
            by_query[query] = entry
        if not entry["path"]:
            continue
        seconds = slot["duration"] / FPS
        rel = f"broll/b{i:02d}.mp4"
        start = entry["offset"] % max(entry["length"], 0.1)
        try:
            media.normalize_clip(entry["path"], public / rel, seconds, start=start)
            slot["broll"] = rel
            entry["offset"] += seconds
        except media.MediaError as exc:
            _log(f"broll beat {i}: {exc}")
    return used


def gather_music(pkg: Dict[str, Any], work: Path, seed: str, *, offline: bool) -> Optional[Dict[str, Any]]:
    if offline:
        return None
    music = pkg.get("music") or {}
    choice = sources.find_music(music.get("mood") or "corporate", music.get("avoid_ids") or [], seed)
    if not choice:
        return None
    try:
        path = sources.download(choice["url"], work / "bed.mp3", fallback_url=choice.get("fallback_url"))
        media.duration(path)
    except Exception as exc:
        _log(f"music: {exc} — rendering without a bed")
        return None
    return {**choice, "path": path}


# ---------------------------------------------------------------------------
# 3. Remotion
# ---------------------------------------------------------------------------

def remotion_props(pkg: Dict[str, Any], tl: Dict[str, Any]) -> Dict[str, Any]:
    beats = []
    number = 0
    for beat, slot in zip(pkg["beats"], tl["timeline"]):
        middle = beat["kind"] not in ("hook", "cta")
        number += 1 if middle else 0
        entry = {
            "kind": beat["kind"], "from": slot["from"], "duration": slot["duration"],
            "number": number if middle else 0, "broll": slot.get("broll"),
            "avatar": slot.get("avatar"),
        }
        if slot.get("presenter"):
            entry["presenter"] = slot["presenter"]
            entry["presenterName"] = (presenter.who(pkg["lang"]) or ("", ""))[1]
        if slot.get("show"):
            entry["show"] = slot["show"]
        for key in ("onscreen", "kicker", "value", "label", "items", "url", "before", "after"):
            if beat.get(key):
                entry[key] = beat[key]
        if beat["kind"] == "avatar":
            entry["avatarName"] = beat["avatar"].capitalize()
        beats.append(entry)
    hook = pkg["beats"][0]
    return {
        "lang": pkg["lang"],
        "format": pkg.get("format", "classic"),
        "take": tl.get("take"),
        "presenterPhoto": tl.get("photo"),
        "presenterName": (presenter.who(pkg["lang"]) or ("", ""))[1],
        "faceY": presenter.FACE_Y.get((presenter.who(pkg["lang"]) or ("", ""))[0], 0.25),
        "palette": pkg["style"]["palette"],
        "motif": pkg["style"]["motif"],
        "brand": BRANDS["biglobster"],
        "beats": beats,
        "words": [{k: w[k] for k in ("text", "startMs", "endMs", "page")} for w in tl["words"]],
        "durationInFrames": tl["frames"],
        "cover": {"title": pkg["covers"]["cover_title"], "kicker": hook.get("kicker", "")},
        "thumb": {"title": pkg["covers"]["thumb_title"]},
    }


def _remotion(remotion_dir: Path, args: List[str], what: str, timeout: int = 1800) -> None:
    npx = shutil.which("npx")
    if not npx:
        raise BuildError("npx not found — the render side needs Node")
    cmd = [npx, "--no-install", "remotion", *args]
    browser = os.environ.get("REMOTION_BROWSER_EXECUTABLE")
    if browser:
        cmd.append(f"--browser-executable={browser}")
    started = time.time()
    proc = subprocess.run(cmd, cwd=str(remotion_dir), stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-25:])
        raise BuildError(f"remotion {what} failed:\n{tail}")
    _log(f"remotion {what}: {time.time() - started:.0f}s")


def render_visuals(remotion_dir: Path, props_path: Path, public: Path, out: Path) -> Dict[str, Path]:
    common = [f"--props={props_path}", f"--public-dir={public}", "--log=error"]
    # Remotion defaults to half the cores — one on a 2-vCPU Actions runner.
    concurrency = os.environ.get("REMOTION_CONCURRENCY", "100%")
    silent = out.parent / "work" / "master_silent.mp4"
    silent.parent.mkdir(parents=True, exist_ok=True)
    _remotion(remotion_dir, ["render", "src/index.ts", "Short", str(silent), "--codec=h264",
                             "--crf=18", "--muted", "--pixel-format=yuv420p", "--color-space=bt709",
                             f"--concurrency={concurrency}", *common], "render")
    cover, thumb = out / "cover.jpg", out / "thumb.jpg"
    _remotion(remotion_dir, ["still", "src/index.ts", "Cover", str(cover), "--image-format=jpeg",
                             "--jpeg-quality=88", *common], "cover")
    _remotion(remotion_dir, ["still", "src/index.ts", "Thumb", str(thumb), "--image-format=jpeg",
                             "--jpeg-quality=88", *common], "thumb")
    return {"silent": silent, "cover": cover, "thumb": thumb}


# ---------------------------------------------------------------------------
# 4. Story cut
# ---------------------------------------------------------------------------

def story_cut_point(timeline: List[Dict[str, Any]], total_seconds: float) -> float:
    """Whole video when it fits; else the last beat boundary under the limit."""
    if total_seconds <= STORY_LIMIT:
        return total_seconds
    best = 0.0
    for slot in timeline:
        end = (slot["from"] + slot["duration"]) / FPS
        if end <= STORY_LIMIT:
            best = end
    return best if best >= 15.0 else STORY_LIMIT


# ---------------------------------------------------------------------------

def build(package_path: Path, out: Path, remotion_dir: Path, *, fake_voice: bool = False,
          offline: bool = False) -> int:
    raw = json.loads(package_path.read_text(encoding="utf-8"))
    try:
        pkg = validate_package(raw)
    except PackageError as exc:
        raise BuildError(f"invalid package:\n{exc}") from exc
    request_id = pkg.get("request_id") or f"local-{pkg['lang']}-{pkg['article']['slug']}"[:90]
    seed = request_id

    out.mkdir(parents=True, exist_ok=True)
    work = out.parent / "work"
    public = work / "public"
    for d in (work, public / "fonts"):
        d.mkdir(parents=True, exist_ok=True)
    font_src = remotion_dir / "node_modules" / "@fontsource-variable" / "inter" / "files" / FONT_FILE
    if not font_src.exists():
        raise BuildError(f"{font_src} missing — run npm ci in {remotion_dir}")
    shutil.copyfile(font_src, public / "fonts" / FONT_FILE)

    t0 = time.time()
    budget = genvideo.open_budget(offline=offline)
    talking = presenter.render(pkg, work, budget, offline=offline)
    tip = pkg.get("format") == "tip"
    take = talking["clips"].get(0) if tip else None
    if take is not None and not media.has_audio(take):
        _log("presenter take has no audio track; the script goes to the voice-over")
        take = None
    if take is not None:
        tl = build_take_timeline(pkg, work, public, take)
        talking["report"]["used"] = list(range(len(pkg["beats"])))
    else:
        # The tip format without its take: the same beats and cards, voiced by Edge.
        tl = build_timeline(pkg, work, public, fake_voice=fake_voice,
                            presenter_clips={} if tip else talking["clips"])
        for slot, beat in zip(tl["timeline"], pkg["beats"]):
            slot["show"] = beat.get("show")
        talking["report"]["used"] = [i for i, s in enumerate(tl["timeline"]) if s.get("presenter")]
    total_seconds = tl["frames"] / FPS
    _log(f"voice: {len(pkg['beats'])} beats, {total_seconds:.2f}s, {len(tl['words'])} words")

    ai_video = genvideo.render_scenes(pkg, tl["timeline"], work, public, seed, fps=FPS,
                                      offline=offline, budget=budget)
    # The take fills every frame (full or in its bubble over a card): no stock footage.
    broll = [] if take is not None else gather_broll(pkg, tl["timeline"], work, public, seed, offline=offline)
    music = gather_music(pkg, work, seed, offline=offline)
    _log(f"sources: {len(broll)} clips, music={'none' if not music else music['id']}")

    props = remotion_props(pkg, tl)
    props_path = work / "props.json"
    props_path.write_text(json.dumps(props, ensure_ascii=False, indent=1), encoding="utf-8")
    visuals = render_visuals(remotion_dir, props_path, public, out)

    audio = media.master_audio(tl["voice"], music["path"] if music else None, work / "master_audio.wav")
    master = media.mux(visuals["silent"], audio, out / "master.mp4")
    cut = story_cut_point(tl["timeline"], total_seconds)
    story = out / "story.mp4"
    if cut >= total_seconds - 0.01:
        shutil.copyfile(master, story)
    else:
        media.story_cut(master, story, cut)
    (out / "captions.srt").write_text(voice.build_srt(tl["words"]), encoding="utf-8")

    report = qa.check_master(master, expected_seconds=total_seconds,
                             target=qa.TIP_TARGET if tip else (qa.TARGET_MIN, qa.TARGET_MAX))
    if tip and take is None and budget["cfg"]["key"]:
        # The presenter is the same person with the same voice in every short:
        # a tip short in the narrator's voice instead is not published.
        why = "; ".join(b.get("reason") or b.get("status") for b in talking["report"]["beats"]) or "not made"
        report["passed"] = False
        report["errors"].append(f"presenter take missing ({why}): a tip short is published only in "
                                "the presenter's own voice")
    report["story"] = qa.check_story(story)
    report["cover"] = qa.check_still(visuals["cover"])
    report["thumb"] = qa.check_still(visuals["thumb"])
    for part in ("story", "cover", "thumb"):
        if not report[part]["passed"]:
            report["passed"] = False
            report["errors"] += [f"{part}: {e}" for e in report[part]["errors"]]
    (out / "qa.json").write_text(json.dumps(report, indent=1), encoding="utf-8")

    manifest = {
        "request_id": request_id,
        "lang": pkg["lang"],
        "article": pkg["article"],
        "style": pkg["style"],
        "duration_s": round(total_seconds, 3),
        "story_duration_s": round(cut, 3),
        "files": {"master": "master.mp4", "story": "story.mp4", "cover": "cover.jpg",
                  "thumb": "thumb.jpg", "srt": "captions.srt"},
        "broll": broll,
        "music": {k: music[k] for k in ("id", "source", "url")} if music else None,
        "avatars": [b["avatar"] for b in pkg["beats"] if b["kind"] == "avatar"],
        "ai_video": ai_video,
        "presenter": talking["report"],
        # Realistic generated footage, a generated presenter or an AI avatar:
        # YouTube's containsSyntheticMedia.
        "synthetic": (bool(ai_video["generated"]) or bool(talking["report"]["used"])
                      or any(b["kind"] == "avatar" for b in pkg["beats"])),
        "covers": pkg["covers"],
        "social": pkg["social"],
        "qa": {"passed": report["passed"], "errors": report["errors"], "warnings": report["warnings"],
               "loudness": report.get("loudness")},
        "render_seconds": round(time.time() - t0, 1),
        "rendered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    _log(f"done in {manifest['render_seconds']}s — QA {'PASSED' if report['passed'] else 'FAILED'}")
    for e in report["errors"]:
        _log(f"QA error: {e}")
    for w in report["warnings"]:
        _log(f"QA warning: {w}")
    return 0 if report["passed"] else 2


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--remotion", required=True, type=Path)
    parser.add_argument("--fake-voice", action="store_true", help="offline tone instead of Edge TTS")
    parser.add_argument("--offline", action="store_true", help="skip footage and music downloads")
    args = parser.parse_args(argv)
    try:
        return build(args.package, args.out.resolve(), args.remotion.resolve(),
                     fake_voice=args.fake_voice, offline=args.offline)
    except (BuildError, PackageError, media.MediaError, voice.VoiceError) as exc:
        print(f"[studio] FAILED: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
