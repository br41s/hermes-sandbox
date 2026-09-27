"""ffmpeg/ffprobe helpers for the render side.

Subprocess argv only, like the rest of ``plugins/shorts``. Every helper takes
absolute paths so callers never depend on the working directory.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

WIDTH, HEIGHT, FPS = 1080, 1920, 30
SAMPLE_RATE = 48000
DEFAULT_TIMEOUT = 600

# Target loudness for Reels/Shorts. Both platforms normalise toward about
# -14 LUFS; delivering louder just gets turned down.
TARGET_I, TARGET_TP, TARGET_LRA = -14.0, -1.5, 11.0

# Voiceover polish: rumble cut, gentle compression, a little air. Same chain
# the manual checklist used, now in one place.
VO_POLISH = (
    "highpass=f=90,"
    "acompressor=threshold=-18dB:ratio=3:attack=5:release=50,"
    "treble=g=2:f=6000"
)


class MediaError(RuntimeError):
    pass


def binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise MediaError(f"{name} not found on PATH")
    return path


def run(cmd: List[str], what: str, timeout: int = DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, check=True)
    except subprocess.CalledProcessError as exc:
        tail = " / ".join((exc.stderr or "").strip().splitlines()[-8:])
        raise MediaError(f"{what} failed: {tail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{what} timed out after {timeout}s") from exc


def ffmpeg(*args: str, what: str, timeout: int = DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    return run([binary("ffmpeg"), "-y", "-hide_banner", "-nostdin", "-loglevel", "error", *args],
               what, timeout)


def probe(path: Path) -> Dict[str, Any]:
    out = run([binary("ffprobe"), "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", str(path)], f"ffprobe {path.name}", 60)
    return json.loads(out.stdout or "{}")


def duration(path: Path) -> float:
    info = probe(path)
    try:
        value = float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MediaError(f"no duration for {path}") from exc
    if value <= 0:
        raise MediaError(f"non-positive duration for {path}")
    return value


def has_audio(path: Path) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe(path).get("streams", []))


def to_wav(src: Path, dst: Path) -> Path:
    """Decode anything to 48 kHz mono PCM — the voiceover working format."""
    ffmpeg("-i", str(src), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
           str(dst), what=f"decode {src.name}")
    return dst


def pad_segment(src: Path, dst: Path, lead: float, total: float) -> Path:
    """Place ``src`` after ``lead`` seconds of silence, padded to exactly ``total``.

    Scenes are cut on frame boundaries, so ``total`` is always a whole number
    of frames; padding each segment to it keeps audio and video in lockstep
    by construction instead of by arithmetic.
    """
    delay_ms = max(0, int(round(lead * 1000)))
    ffmpeg("-i", str(src),
           "-af", f"adelay={delay_ms}:all=1,apad",
           "-t", f"{total:.6f}", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
           str(dst), what=f"pad {src.name}")
    return dst


def silence(dst: Path, seconds: float) -> Path:
    ffmpeg("-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=mono", "-t", f"{seconds:.6f}",
           "-c:a", "pcm_s16le", str(dst), what="silence")
    return dst


def concat_wavs(parts: List[Path], dst: Path) -> Path:
    """Sample-exact concatenation of same-format WAVs (re-encoded, never -c copy)."""
    listing = dst.with_suffix(".txt")
    listing.write_text("".join(f"file '{p.resolve()}'\n" for p in parts), encoding="utf-8")
    ffmpeg("-f", "concat", "-safe", "0", "-i", str(listing),
           "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(dst), what="concat voice")
    return dst


def polish_voice(src: Path, dst: Path) -> Path:
    ffmpeg("-i", str(src), "-af", VO_POLISH, "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
           str(dst), what="polish voice")
    return dst


def normalize_clip(src: Path, dst: Path, seconds: float, *, start: float = 0.0) -> Path:
    """Fill-crop a clip to 1080x1920@30 for exactly ``seconds``, looping if short.

    Trimmed to the scene rather than to a fixed 15s loop, so a clip never
    visibly restarts or holds its last frame inside a scene.
    """
    ffmpeg("-stream_loop", "-1", "-ss", f"{max(0.0, start):.3f}", "-i", str(src),
           "-t", f"{seconds:.3f}",
           "-vf", (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
                   f"crop={WIDTH}:{HEIGHT},fps={FPS},setsar=1,format=yuv420p"),
           "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p",
           str(dst), what=f"normalise {src.name}")
    return dst


def extract_audio(src: Path, dst: Path) -> Path:
    ffmpeg("-i", str(src), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
           str(dst), what=f"extract audio {src.name}")
    return dst


def _loudnorm_measure(src: Path) -> Dict[str, str]:
    out = run([binary("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(src), "-af",
               f"loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}:print_format=json",
               "-f", "null", "-"], "loudness measure")
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", out.stderr or "", re.S)
    if not match:
        raise MediaError("loudnorm printed no measurement")
    return json.loads(match.group(0))


def master_audio(voice: Path, bed: Optional[Path], dst: Path, *, bed_volume: float = 0.35) -> Path:
    """Duck the bed under the voice, then two-pass loudnorm to -14 LUFS.

    Single-pass loudnorm runs in dynamic mode and audibly pumps on speech;
    measuring first and applying linearly does not.
    """
    mix = dst.with_name("mix_pre.wav")
    if bed is not None:
        ffmpeg("-i", str(voice), "-stream_loop", "-1", "-i", str(bed), "-filter_complex",
               ("[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,asplit=2[v][sc];"
                f"[1:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,volume={bed_volume}[bed];"
                "[bed][sc]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=300[duck];"
                "[v][duck]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[out]"),
               "-map", "[out]", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(mix),
               what="duck music bed")
    else:
        ffmpeg("-i", str(voice), "-ac", "2", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(mix),
               what="voice to stereo")

    m = _loudnorm_measure(mix)
    ffmpeg("-i", str(mix), "-af",
           (f"loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}"
            f":measured_I={m['input_i']}:measured_TP={m['input_tp']}"
            f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
            f":offset={m['target_offset']}:linear=true"),
           "-ar", str(SAMPLE_RATE), "-ac", "2", "-c:a", "pcm_s16le", str(dst), what="loudness normalise")
    return dst


def mux(video: Path, audio: Path, dst: Path) -> Path:
    ffmpeg("-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", str(SAMPLE_RATE),
           "-movflags", "+faststart", "-shortest", str(dst), what="mux master")
    return dst


def story_cut(master: Path, dst: Path, cut_at: float, *, fade: float = 0.5) -> Path:
    """Re-encode the first ``cut_at`` seconds with a clean fade.

    ``-t 58.9 -c copy`` cuts on the nearest keyframe and can end mid-word;
    the caller passes a beat boundary and this fades picture and sound out.
    """
    start_fade = max(0.0, cut_at - fade)
    ffmpeg("-i", str(master), "-t", f"{cut_at:.3f}",
           "-vf", f"fade=t=out:st={start_fade:.3f}:d={fade}",
           "-af", f"afade=t=out:st={start_fade:.3f}:d={fade}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dst), what="story cut")
    return dst
