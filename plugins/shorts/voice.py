"""Voiceover synthesis and duration probing.

Synthesis goes through :func:`tools.tts_tool.text_to_speech_tool` rather than
calling a TTS library directly, so the user's configured provider and voice
(``tts:`` in config.yaml) are honoured. The default is Edge TTS — free, no
API key — but a profile can point ``tts.provider`` at Kokoro via a
``type: command`` provider, or at any of the built-ins, with no change here.

Each scene's line is synthesised *separately* and then probed for its exact
duration. That is what lets captions line up with speech without needing
word-level timestamps from the TTS engine: the caption for scene *n* runs for
exactly as long as scene *n*'s audio. It also keeps the pipeline
provider-agnostic, since no engine-specific timing metadata is involved.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 30
SYNTH_TIMEOUT = 180


class VoiceError(RuntimeError):
    """Raised when a voiceover line cannot be synthesised or measured."""


def find_binary(name: str) -> Optional[str]:
    """Locate ffmpeg/ffprobe on PATH. Returns None when absent."""
    return shutil.which(name)


def require_ffmpeg() -> tuple[str, str]:
    """Return ``(ffmpeg, ffprobe)`` paths, raising when either is missing."""
    ffmpeg = find_binary("ffmpeg")
    ffprobe = find_binary("ffprobe")
    if not ffmpeg or not ffprobe:
        raise VoiceError(
            "ffmpeg and ffprobe are required to render shorts but were not "
            "found on PATH. They ship in the Hermes container image."
        )
    return ffmpeg, ffprobe


def probe_duration(path: str | Path) -> float:
    """Return the duration of a media file in seconds, via ffprobe."""
    _, ffprobe = require_ffmpeg()
    cmd = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT, check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise VoiceError(f"ffprobe failed on {path}: {exc.stderr.strip()}") from exc
    except subprocess.TimeoutExpired as exc:
        raise VoiceError(f"ffprobe timed out on {path}") from exc

    raw = (result.stdout or "").strip()
    try:
        duration = float(raw)
    except ValueError as exc:
        raise VoiceError(f"ffprobe returned no duration for {path}: {raw!r}") from exc
    if duration <= 0:
        raise VoiceError(f"ffprobe reported a non-positive duration for {path}")
    return duration


def synthesize(text: str, output_path: str | Path) -> str:
    """Synthesise ``text`` to ``output_path``, returning the real output path.

    The path is returned rather than assumed: ``text_to_speech_tool``
    transcodes to Opus when the session platform is Telegram, which changes
    the extension out from under the caller.
    """
    if not text or not text.strip():
        raise VoiceError("Cannot synthesise an empty voiceover line")

    from tools.tts_tool import text_to_speech_tool

    raw = text_to_speech_tool(text=text.strip(), output_path=str(output_path))
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise VoiceError(f"TTS returned unparseable output: {raw!r}") from exc

    if not payload.get("success"):
        raise VoiceError(f"TTS failed: {payload.get('error') or 'unknown error'}")

    path = payload.get("file_path")
    if not path or not Path(path).exists():
        raise VoiceError(f"TTS reported success but produced no file: {path!r}")
    return path
