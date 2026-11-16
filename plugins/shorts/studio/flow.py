"""Render the ``flow`` short format: the owner's own Google Flow footage.

Until 2026-11-15 VideoDesk finished these clips by hand; the Studio now takes
one vertical clip with its own spoken audio and produces the finished short:
karaoke subtitles of what faster-whisper hears, the small brand mark, the
animated outro card, audio mastered to -14 LUFS, a titled cover grabbed from a
frame, sidecar captions, and a transcript with doubtful words flagged.

Because the repository is public on GitHub, real dispatches upload the footage
Fernet-encrypted as a GitHub release asset and pass its ``api.github.com`` URL;
the runner decrypts it in ``$RUNNER_TEMP`` using ``FLOW_ASSET_KEY``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import urllib.parse
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
import httpx

from plugins.shorts.studio import media, qa, sources, voice
from plugins.shorts.studio.build import (
    BRANDS,
    FONT_FILE,
    FPS,
    BuildError,
    _github_headers,
    _log,
    _remotion,
)

OUTRO_SECONDS = 5.0
XFADE_SECONDS = 0.5
# QA's hard floor for a master is 12 s (qa.MIN_SECONDS); footage plus the 4.5 s the outro adds
# (the 5 s card over a 0.5 s crossfade) must clear it.
FOOTAGE_MIN_S, FOOTAGE_MAX_S = 8.0, 60.0
FLOW_MAX_BYTES = 250 * 1024 * 1024
DOUBTFUL_BELOW = 0.6          # whisper word probability under this is flagged for the owner
LATE_START_S = 0.5            # VideoDesk's rule: the first word is heard before 0.5 s
LONG_FOOTAGE_S = 30.0         # Guionista packs aim at <=30 s; longer is flagged, never cut
ALLOWED_HOSTS = {"biglobster.top", "www.biglobster.top"}
FOOTAGE_FINDING_PREFIXES = ("frozen picture", "black frames")
FOOTAGE_FINDING_SILENCE = re.compile(r"^\d+(\.\d+)?s of silence at")


def _check_lang(raw: dict[str, Any], problems: list[str]) -> str:
    lang = raw.get("lang")
    normalised = lang.strip().lower() if isinstance(lang, str) else ""
    if normalised not in {"es", "en"}:
        problems.append(f"lang: must be 'es' or 'en', got {lang!r}")
    return normalised


def _check_article(raw: dict[str, Any], problems: list[str]) -> str:
    raw_url = raw.get("article_url")
    article_url = raw_url.strip() if isinstance(raw_url, str) else ""
    parsed = urllib.parse.urlparse(article_url)
    if parsed.scheme != "https":
        problems.append(f"article_url: scheme must be https, got {article_url!r}")
    elif (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        problems.append(f"article_url: host must be in {sorted(ALLOWED_HOSTS)}, got {article_url!r}")
    return article_url


def _check_cover(raw: dict[str, Any], problems: list[str]) -> dict[str, Any]:
    cover: dict[str, Any] = {"title": "", "kicker": "", "frame_s": 2.5}
    raw_cover = raw.get("cover")
    if not isinstance(raw_cover, dict):
        problems.append("cover: must be an object")
        return cover

    raw_title = raw_cover.get("title")
    title = raw_title.strip() if isinstance(raw_title, str) else ""
    if not (3 <= len(title) <= 60):
        problems.append(f"cover.title: must be 3..60 chars after strip, got {len(title)}")
    cover["title"] = title

    raw_kicker = raw_cover.get("kicker", "")
    if raw_kicker is None:
        raw_kicker = ""
    if not isinstance(raw_kicker, str):
        problems.append("cover.kicker: must be a string")
    else:
        kicker = raw_kicker.strip()
        if len(kicker) > 40:
            problems.append(f"cover.kicker: max 40 chars, got {len(kicker)}")
        cover["kicker"] = kicker

    raw_frame_s = raw_cover.get("frame_s", 2.5)
    if raw_frame_s is None:
        raw_frame_s = 2.5
    if (
        isinstance(raw_frame_s, bool)
        or not isinstance(raw_frame_s, (int, float))
        or not (0.0 <= float(raw_frame_s) <= 60.0)
    ):
        problems.append(f"cover.frame_s: must be a number in 0..60, got {raw_frame_s!r}")
    else:
        cover["frame_s"] = float(raw_frame_s)
    return cover


def _check_footage_path(raw_footage: dict[str, Any], problems: list[str]) -> dict[str, Any]:
    rel = raw_footage.get("path")
    if not isinstance(rel, str) or not rel.strip():
        problems.append("footage.path: must be a non-empty relative path")
        return {}
    rel = rel.strip()
    parts = rel.replace("\\", "/").split("/")
    if rel.startswith("/") or rel.startswith("\\") or Path(rel).is_absolute() or ".." in parts:
        problems.append(f"footage.path: must be relative without '..', got {rel!r}")
        return {}
    return {"path": rel}


def _check_footage_url(raw_footage: dict[str, Any], problems: list[str]) -> dict[str, Any]:
    url = raw_footage.get("url")
    url_str = url.strip() if isinstance(url, str) else ""
    parsed = urllib.parse.urlparse(url_str)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "api.github.com":
        problems.append(f"footage.url: must be https://api.github.com/..., got {url!r}")
    if raw_footage.get("encrypted") is not True:
        problems.append("footage.encrypted: must be true when footage.url is used")
    if url_str and raw_footage.get("encrypted") is True:
        return {"url": url_str, "encrypted": True}
    return {}


def _check_footage(raw: dict[str, Any], problems: list[str]) -> dict[str, Any]:
    raw_footage = raw.get("footage")
    if not isinstance(raw_footage, dict):
        problems.append("footage: must be an object")
        return {}
    has_path = "path" in raw_footage and raw_footage.get("path") is not None
    has_url = "url" in raw_footage and raw_footage.get("url") is not None
    if has_path == has_url:
        problems.append("footage: must specify exactly one of 'path' or 'url'")
        return {}
    if has_path:
        return _check_footage_path(raw_footage, problems)
    return _check_footage_url(raw_footage, problems)


def _check_brand(raw: dict[str, Any], problems: list[str]) -> str:
    raw_brand = raw.get("brand")
    brand = raw_brand if raw_brand is not None else "biglobster"
    if not isinstance(brand, str) or brand not in BRANDS:
        problems.append(f"brand: must be one of {sorted(BRANDS)}, got {brand!r}")
        return ""
    return brand


def validate(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalise a flow package, or raise :class:`BuildError`."""
    if not isinstance(raw, dict):
        raise BuildError("package must be a JSON object")

    problems: list[str] = []

    fmt = raw.get("format")
    if fmt != "flow":
        problems.append(f"format: expected 'flow', got {fmt!r}")

    lang = _check_lang(raw, problems)
    article_url = _check_article(raw, problems)
    cover = _check_cover(raw, problems)
    footage = _check_footage(raw, problems)

    request_id = raw.get("request_id")
    if request_id is not None and (not isinstance(request_id, str) or not request_id.strip()):
        problems.append("request_id: must be a non-empty string when provided")

    brand = _check_brand(raw, problems)

    if problems:
        raise BuildError("\n".join(problems))

    norm: dict[str, Any] = {
        "format": "flow",
        "lang": lang,
        "article_url": article_url,
        "cover": cover,
        "footage": footage,
        "brand": brand,
    }
    if isinstance(request_id, str) and request_id.strip():
        norm["request_id"] = request_id.strip()
    return norm


def decrypt(data: bytes, key: str) -> bytes:
    """Decrypt Fernet-encrypted footage bytes with ``FLOW_ASSET_KEY``."""
    cleaned = key.strip()
    if not cleaned:
        raise BuildError("cannot decrypt the footage: FLOW_ASSET_KEY missing or wrong")
    try:
        return Fernet(cleaned).decrypt(data)
    except (InvalidToken, ValueError, TypeError) as exc:
        raise BuildError("cannot decrypt the footage: FLOW_ASSET_KEY missing or wrong") from exc


def words_from_heard(heard: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert faster-whisper word timings into paginated karaoke words."""
    words: list[dict[str, Any]] = []
    for item in heard:
        raw = str(item.get("text") or "").strip()
        text = voice.clean_word(raw)
        if not text:
            continue
        words.append({
            "text": text,
            "raw": raw,
            "startMs": round(float(item["start"]) * 1000),
            "endMs": round(float(item["end"]) * 1000),
            "beat": 0,
            "probability": float(item.get("probability", 1.0)),
        })
    return voice.paginate(words)


def flags(words: list[dict[str, Any]], footage_s: float) -> dict[str, Any]:
    """Editorial/QA flags surfaced in the manifest for the owner."""
    first_word_s = words[0]["startMs"] / 1000 if words else None
    doubtful = [
        {
            "word": w["raw"],
            "at_s": round(w["startMs"] / 1000, 1),
            "probability": round(float(w["probability"]), 2),
        }
        for w in words
        if float(w.get("probability", 1.0)) < DOUBTFUL_BELOW
    ]
    return {
        "first_word_s": first_word_s,
        "late_start": first_word_s is not None and first_word_s > LATE_START_S,
        "doubtful": doubtful,
        "over_30s": footage_s > LONG_FOOTAGE_S,
        "no_speech": not words,
    }


def timing(footage_s: float) -> dict[str, Any]:
    """Frame counts for the footage, the crossfade, and the 5 s outro card."""
    footage_frames = round(footage_s * FPS)
    xfade = round(XFADE_SECONDS * FPS)
    outro = round(OUTRO_SECONDS * FPS)
    outro_from = footage_frames - xfade
    total = outro_from + outro
    return {
        "footage_frames": footage_frames,
        "outro_from": outro_from,
        "outro_frames": outro,
        "total_frames": total,
        "total_s": total / FPS,
    }


def resolve_path(package_path: Path, rel: str) -> Path:
    """Resolve ``rel`` inside ``package_path.parent`` and refuse traversal."""
    base = package_path.parent.resolve()
    target = (package_path.parent / rel).resolve()
    if target == base or not target.is_relative_to(base):
        raise BuildError(f"footage path {rel!r} escapes {base}")
    return target


def _download_footage(url: str, work: Path, dest: Path) -> None:
    enc_path = work / "footage.enc"
    headers = {"User-Agent": sources.USER_AGENT, **_github_headers(url)}
    try:
        with httpx.Client(timeout=120, follow_redirects=True, headers=headers) as client, \
                client.stream("GET", url) as resp:
            if resp.status_code != 200:
                raise BuildError(f"flow footage {url}: HTTP {resp.status_code}")
            written = 0
            with open(enc_path, "wb") as handle:
                for chunk in resp.iter_bytes(1 << 16):
                    written += len(chunk)
                    if written > FLOW_MAX_BYTES:
                        raise BuildError(f"flow footage {url} is too large")
                    handle.write(chunk)
        dest.write_bytes(decrypt(enc_path.read_bytes(), os.environ.get("FLOW_ASSET_KEY", "")))
    finally:
        enc_path.unlink(missing_ok=True)


def footage_findings_to_warnings(report: dict[str, Any]) -> dict[str, Any]:
    """Demote the master's footage-derived findings to warnings, in place.

    The owner reviews his footage before anything is published and a re-render cannot change
    it, so a finding that comes from the clip itself must not cost one of the three render
    attempts. Technical checks (resolution, frame rate, codecs, loudness, duration vs timeline)
    still fail the render, because those are ours.
    """
    kept: list[str] = []
    for error in report["errors"]:
        if error.startswith(FOOTAGE_FINDING_PREFIXES) or FOOTAGE_FINDING_SILENCE.match(error):
            report["warnings"].append(f"footage: {error}")
        else:
            kept.append(error)
    report["errors"] = kept
    report["passed"] = not kept
    return report


def build(package_path: Path, raw: dict[str, Any], out: Path, remotion_dir: Path, *,
          offline: bool = False) -> int:
    pkg = validate(raw)
    request_id = pkg.get("request_id") or f"local-flow-{pkg['lang']}"

    out.mkdir(parents=True, exist_ok=True)
    work = out.parent / "work"
    public = work / "public"
    for d in (work, public / "fonts", public / "flow"):
        d.mkdir(parents=True, exist_ok=True)
    font_src = remotion_dir / "node_modules" / "@fontsource-variable" / "inter" / "files" / FONT_FILE
    if not font_src.exists():
        raise BuildError(f"{font_src} missing — run npm ci in {remotion_dir}")
    shutil.copyfile(font_src, public / "fonts" / FONT_FILE)

    t0 = time.time()
    dest = public / "flow" / "footage.mp4"
    if "path" in pkg["footage"]:
        src = resolve_path(package_path, pkg["footage"]["path"])
        if not src.exists():
            raise BuildError(f"footage file missing: {src}")
        shutil.copyfile(src, dest)
    else:
        _download_footage(pkg["footage"]["url"], work, dest)

    info = media.probe(dest)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise BuildError("footage has no video stream")
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if height <= width:
        raise BuildError(f"footage must be vertical (height > width), got {width}x{height}")
    if not media.has_audio(dest):
        raise BuildError("footage has no audio stream")
    footage_s = media.duration(dest)
    if not (FOOTAGE_MIN_S <= footage_s <= FOOTAGE_MAX_S):
        raise BuildError(
            f"footage duration {footage_s:.1f}s outside {FOOTAGE_MIN_S:.0f}..{FOOTAGE_MAX_S:.0f}s"
        )

    wav = media.to_wav(dest, work / "footage.wav")
    heard = voice.heard_words(wav, pkg["lang"])
    words = words_from_heard(heard)
    _log(f"flow: {footage_s:.2f}s footage, {len(words)} words")

    t = timing(footage_s)
    brand = BRANDS[pkg["brand"]]
    props = {
        "lang": pkg["lang"],
        "footage": "flow/footage.mp4",
        "footageFrames": t["footage_frames"],
        "outroFrom": t["outro_from"],
        "outroFrames": t["outro_frames"],
        "durationInFrames": t["total_frames"],
        "words": [{k: w[k] for k in ("text", "startMs", "endMs", "page")} for w in words],
        "articleUrl": pkg["article_url"],
        "brand": brand,
    }
    props_path = work / "flow_props.json"
    props_path.write_text(json.dumps(props, ensure_ascii=False, indent=1), encoding="utf-8")
    silent = work / "master_silent.mp4"
    concurrency = os.environ.get("REMOTION_CONCURRENCY", "100%")
    _remotion(
        remotion_dir,
        [
            "render", "src/index.ts", "Flow", str(silent),
            "--codec=h264", "--crf=18", "--muted",
            "--pixel-format=yuv420p", "--color-space=bt709",
            f"--concurrency={concurrency}",
            f"--props={props_path}", f"--public-dir={public}", "--log=error",
        ],
        "render",
    )

    frame_s = min(pkg["cover"]["frame_s"], footage_s - 0.1)
    media.ffmpeg(
        "-ss", f"{frame_s:.3f}", "-i", str(dest),
        "-frames:v", "1", "-q:v", "2", str(public / "flow" / "frame.jpg"),
        what="cover frame",
    )
    cover_props_data = {
        "lang": pkg["lang"],
        "format": "classic",
        "palette": "brand",
        "motif": "diamonds",
        "brand": brand,
        "beats": [],
        "words": [],
        "durationInFrames": 1,
        "cover": {"title": pkg["cover"]["title"], "kicker": pkg["cover"]["kicker"]},
        "thumb": {"title": pkg["cover"]["title"]},
        "presenterPhoto": "flow/frame.jpg",
        "faceY": 0.25,
    }
    cover_props = work / "cover_props.json"
    cover_props.write_text(json.dumps(cover_props_data, ensure_ascii=False, indent=1), encoding="utf-8")
    cover_path = out / "cover.jpg"
    _remotion(
        remotion_dir,
        [
            "still", "src/index.ts", "Cover", str(cover_path),
            "--image-format=jpeg", "--jpeg-quality=88",
            f"--props={cover_props}", f"--public-dir={public}", "--log=error",
        ],
        "cover",
    )

    faded = work / "faded.wav"
    media.ffmpeg(
        "-i", str(wav),
        "-af", f"afade=t=out:st={max(0.0, footage_s - XFADE_SECONDS):.3f}:d={XFADE_SECONDS}",
        "-c:a", "pcm_s16le", str(faded),
        what="fade footage audio",
    )
    padded = media.pad_segment(faded, work / "voice.wav", 0.0, t["total_s"])
    audio = media.master_audio(padded, None, work / "master_audio.wav")
    master = media.mux(silent, audio, out / "master.mp4")

    (out / "captions.srt").write_text(voice.build_srt(words), encoding="utf-8")
    (out / "transcript.txt").write_text(" ".join(w["raw"] for w in words), encoding="utf-8")
    (out / "words.json").write_text(json.dumps(words, indent=1, ensure_ascii=False), encoding="utf-8")

    report = qa.check_master(
        master,
        expected_seconds=t["total_s"],
        target=(FOOTAGE_MIN_S, FOOTAGE_MAX_S + OUTRO_SECONDS),
        quiet=[(t["outro_from"] / FPS, t["total_s"])],
    )
    # The owner's footage is his to fix; a re-render cannot change it.
    report = footage_findings_to_warnings(report)
    report["cover"] = qa.check_still(cover_path)
    if not report["cover"]["passed"]:
        report["passed"] = False
        report["errors"] += [f"cover: {e}" for e in report["cover"]["errors"]]
    (out / "qa.json").write_text(json.dumps(report, indent=1), encoding="utf-8")

    manifest = {
        "request_id": request_id,
        "format": "flow",
        "lang": pkg["lang"],
        "article_url": pkg["article_url"],
        "duration_s": round(t["total_s"], 3),
        "footage_s": round(footage_s, 3),
        "files": {
            "master": "master.mp4",
            "cover": "cover.jpg",
            "srt": "captions.srt",
            "transcript": "transcript.txt",
            "words": "words.json",
        },
        "flags": flags(words, footage_s),
        "cover": pkg["cover"],
        "synthetic": True,
        "qa": {
            "passed": report["passed"],
            "errors": report["errors"],
            "warnings": report["warnings"],
            "loudness": report.get("loudness"),
        },
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
