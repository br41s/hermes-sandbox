"""Automatic QA for a rendered short — what used to be eyeballed.

Hard failures stop the short from being published; warnings are reported
but do not. Every check reads the finished file, never the render inputs,
so it catches what the pipeline actually produced.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

from plugins.shorts.studio import media

MIN_SECONDS, MAX_SECONDS = 20.0, 90.0          # hard
TARGET_MIN, TARGET_MAX = 45.0, 70.0            # warn outside
STORY_MAX = 59.9
LUFS_TOLERANCE = 1.5
MAX_TRUE_PEAK = -0.5
FREEZE_MAX = 2.0         # seconds of identical frames that count as a freeze
BLACK_MAX = 0.4
SILENCE_MAX = 1.6        # a longer gap reads as a dropout


def _intervals(stderr: str, start_key: str, end_key: str) -> List[Dict[str, float]]:
    starts = [float(x) for x in re.findall(rf"{start_key}:\s*([\d.]+)", stderr)]
    ends = [float(x) for x in re.findall(rf"{end_key}:\s*([\d.]+)", stderr)]
    return [{"start": s, "end": e, "length": round(e - s, 3)} for s, e in zip(starts, ends)]


def _analyse(path: Path) -> str:
    """One decode pass for every detector; they all log to stderr."""
    out = media.run(
        [media.binary("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(path),
         "-filter_complex",
         ("[0:v]freezedetect=n=0.001:d=1.0,blackdetect=d=0.3:pix_th=0.08[v];"
          "[0:a]silencedetect=n=-45dB:d=1.0,ebur128=peak=true[a]"),
         "-map", "[v]", "-map", "[a]", "-f", "null", "-"],
        f"analyse {path.name}", timeout=900,
    )
    return out.stderr or ""


def _loudness(stderr: str) -> Dict[str, float]:
    summary = stderr[stderr.rfind("Summary:"):] if "Summary:" in stderr else stderr
    result: Dict[str, float] = {}
    m = re.search(r"I:\s*(-?[\d.]+)\s*LUFS", summary)
    if m:
        result["integrated_lufs"] = float(m.group(1))
    m = re.search(r"LRA:\s*([\d.]+)\s*LU", summary)
    if m:
        result["lra"] = float(m.group(1))
    m = re.search(r"Peak:\s*(-?[\d.]+|-inf)\s*dBFS", summary)
    if m and m.group(1) != "-inf":
        result["true_peak_db"] = float(m.group(1))
    return result


def check_master(path: Path, *, expected_seconds: float | None = None) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    info = media.probe(path)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    seconds = float(info.get("format", {}).get("duration") or 0)

    if (video.get("width"), video.get("height")) != (media.WIDTH, media.HEIGHT):
        errors.append(f"resolution {video.get('width')}x{video.get('height')}, expected 1080x1920")
    rate = video.get("r_frame_rate", "0/1")
    num, _, den = rate.partition("/")
    fps = float(num) / float(den or 1) if num else 0
    if abs(fps - media.FPS) > 0.01:
        errors.append(f"frame rate {fps:.2f}, expected {media.FPS}")
    if video.get("codec_name") != "h264" or video.get("pix_fmt") != "yuv420p":
        errors.append(f"video must be h264/yuv420p, got {video.get('codec_name')}/{video.get('pix_fmt')}")
    if not audio:
        errors.append("no audio stream")
    elif audio.get("codec_name") != "aac":
        errors.append(f"audio must be aac, got {audio.get('codec_name')}")

    if not MIN_SECONDS <= seconds <= MAX_SECONDS:
        errors.append(f"duration {seconds:.1f}s outside {MIN_SECONDS:.0f}-{MAX_SECONDS:.0f}s")
    elif not TARGET_MIN <= seconds <= TARGET_MAX:
        warnings.append(f"duration {seconds:.1f}s outside the {TARGET_MIN:.0f}-{TARGET_MAX:.0f}s target")
    if expected_seconds and abs(seconds - expected_seconds) > 0.3:
        errors.append(f"duration {seconds:.2f}s differs from the timeline's {expected_seconds:.2f}s")

    stderr = _analyse(path)
    loud = _loudness(stderr)
    lufs = loud.get("integrated_lufs")
    if lufs is None:
        errors.append("could not measure loudness")
    elif abs(lufs - media.TARGET_I) > LUFS_TOLERANCE:
        errors.append(f"loudness {lufs:.1f} LUFS, target {media.TARGET_I:.0f}±{LUFS_TOLERANCE}")
    peak = loud.get("true_peak_db")
    if peak is not None and peak > MAX_TRUE_PEAK:
        warnings.append(f"true peak {peak:.1f} dBFS above {MAX_TRUE_PEAK}")

    freezes = _intervals(stderr, "lavfi.freezedetect.freeze_start", "lavfi.freezedetect.freeze_end")
    if not freezes:
        freezes = _intervals(stderr, "freeze_start", "freeze_end")
    blacks = _intervals(stderr, "black_start", "black_end")
    silences = _intervals(stderr, "silence_start", "silence_end")
    for f in freezes:
        if f["length"] > FREEZE_MAX:
            errors.append(f"frozen picture for {f['length']:.1f}s at {f['start']:.1f}s")
    for b in blacks:
        if b["length"] > BLACK_MAX:
            errors.append(f"black frames for {b['length']:.1f}s at {b['start']:.1f}s")
    for s in silences:
        # The CTA's closing tail is allowed to breathe.
        if s["length"] > SILENCE_MAX and s["end"] < seconds - 0.3:
            errors.append(f"{s['length']:.1f}s of silence at {s['start']:.1f}s")

    return {
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
        "duration_s": round(seconds, 3),
        "loudness": loud,
        "freezes": freezes,
        "black": blacks,
        "silences": silences,
    }


def check_story(path: Path) -> Dict[str, Any]:
    seconds = media.duration(path)
    errors = [] if seconds <= STORY_MAX else [f"story is {seconds:.1f}s, max {STORY_MAX}s"]
    return {"passed": not errors, "errors": errors, "duration_s": round(seconds, 3)}


def check_still(path: Path, *, max_bytes: int = 2 * 1024 * 1024) -> Dict[str, Any]:
    """Covers must exist, be 1080x1920, and fit YouTube's 2 MB thumbnail cap."""
    errors: List[str] = []
    if not path.exists() or path.stat().st_size == 0:
        return {"passed": False, "errors": [f"{path.name} missing"]}
    info = media.probe(path)
    stream = (info.get("streams") or [{}])[0]
    if (stream.get("width"), stream.get("height")) != (media.WIDTH, media.HEIGHT):
        errors.append(f"{path.name} is {stream.get('width')}x{stream.get('height')}")
    if path.stat().st_size > max_bytes:
        errors.append(f"{path.name} is {path.stat().st_size >> 10} KB, max {max_bytes >> 10} KB")
    return {"passed": not errors, "errors": errors, "bytes": path.stat().st_size}
