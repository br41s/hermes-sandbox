"""Deterministic ffmpeg assembly of a storyboard into a vertical MP4.

Everything here shells out to ``ffmpeg``/``ffprobe`` via subprocess, which is
the established convention in this repo (see ``tools/tts_tool.py`` and
``tools/transcription_tools.py``) — there is no ``moviepy``/``pydub``/
``ffmpeg-python`` dependency anywhere and none is added.

Pipeline, per storyboard:

1. Synthesise each scene's voiceover separately and probe its exact duration.
   Scene length follows the speech, never the other way round.
2. Normalise each scene's B-roll to 1080x1920 at 30fps, looping short clips
   and trimming long ones to the scene length.
3. Concatenate scenes, then lay the voiceover (and optional ducked music bed)
   underneath.
4. Burn the captions and normalise loudness to -14 LUFS, the level both
   Instagram and TikTok normalise toward.

All intermediates live in one temp directory and every ffmpeg invocation runs
with that directory as its cwd, so filter arguments reference bare filenames
and never need path escaping.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from plugins.shorts import captions as captions_mod
from plugins.shorts.voice import VoiceError, probe_duration, require_ffmpeg, synthesize

logger = logging.getLogger(__name__)

WIDTH = 1080
HEIGHT = 1920
FPS = 30

# Silence appended after each spoken line so scenes do not cut on the last
# consonant. Small enough that a 6-scene video only gains ~2s.
SCENE_TAIL_PAD = 0.35

# Hard ceilings. Reels and TikTok both reward sub-60s; beyond that the format
# stops being a short. Exceeding either is an error, not a silent truncation —
# a clipped video would ship with a missing punchline and nobody would notice.
MAX_TOTAL_DURATION = 60.0
MAX_SCENES = 12

FFMPEG_TIMEOUT = 300
DOWNLOAD_TIMEOUT = 90
MAX_CLIP_BYTES = 80 * 1024 * 1024

# Neutral dark background for scenes with no B-roll — captions stay legible.
FALLBACK_COLOR = "0x101418"

MUSIC_VOLUME = 0.18


class RenderError(RuntimeError):
    """Raised when a storyboard cannot be rendered."""


def _run(cmd: List[str], *, cwd: Path, what: str) -> None:
    """Run an ffmpeg command, raising RenderError with stderr on failure."""
    logger.debug("shorts: %s -> %s", what, " ".join(cmd))
    try:
        subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or "").strip().splitlines()[-8:]
        raise RenderError(f"{what} failed: {' / '.join(tail)}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"{what} timed out after {FFMPEG_TIMEOUT}s") from exc


def _check_redirect_target(response: Any) -> None:
    """Re-validate a redirect hop, so a safe URL can't 302 to a private one.

    ``is_safe_url`` at the top of :func:`_download_clip` only checks the
    original URL; ``follow_redirects=True`` would otherwise let a public URL
    302 to ``http://169.254.169.254/...`` unchecked. Same guard as
    ``gateway/platforms/yuanbao_media.py`` and the platform adapters
    (``_ssrf_redirect_guard``). Takes ``Any`` rather than ``httpx.Response``
    so it is testable without a real httpx dependency in the test.
    """
    from tools.url_safety import is_safe_url

    if response.is_redirect and response.next_request:
        redirect_url = str(response.next_request.url)
        if not is_safe_url(redirect_url):
            raise RenderError(
                f"Blocked redirect to private/internal address: {redirect_url}"
            )


def _download_clip(url: str, dest: Path) -> None:
    """Fetch a remote clip, refusing unsafe URLs and oversized bodies."""
    from tools.url_safety import is_safe_url

    if not is_safe_url(url):
        raise RenderError(f"Refusing to fetch unsafe or private clip URL: {url}")

    import httpx

    written = 0
    try:
        with httpx.stream(
            "GET",
            url,
            timeout=DOWNLOAD_TIMEOUT,
            follow_redirects=True,
            event_hooks={"response": [_check_redirect_target]},
        ) as response:
            response.raise_for_status()
            with open(dest, "wb") as handle:
                for chunk in response.iter_bytes(chunk_size=1 << 16):
                    written += len(chunk)
                    if written > MAX_CLIP_BYTES:
                        raise RenderError(
                            f"Clip at {url} exceeds the "
                            f"{MAX_CLIP_BYTES // (1024 * 1024)}MB limit"
                        )
                    handle.write(chunk)
    except RenderError:
        raise
    except Exception as exc:
        raise RenderError(f"Could not download clip {url}: {exc}") from exc

    if not dest.exists() or dest.stat().st_size == 0:
        raise RenderError(f"Clip download produced an empty file: {url}")


def _validate_local_path(raw: str, what: str) -> Path:
    """Bound a scene-supplied local path to this profile's reachable dirs.

    ``clip_path``/``music_path`` come from LLM-generated scene data, same as
    ``output_path`` in ``plugins/shorts/__init__.py`` — a prompt-injected
    agent must not be able to read arbitrary files off disk by pointing here.
    Mirrors ``_validate_output_path``'s roots (profile home + system temp).
    """
    from hermes_constants import get_hermes_home
    from tools.path_security import has_traversal_component, validate_within_dir

    if has_traversal_component(raw):
        raise RenderError(f"{what} contains a '..' component: {raw}")

    path = Path(raw).expanduser()
    roots = [Path(get_hermes_home()), Path(tempfile.gettempdir())]
    if any(validate_within_dir(path, root) is None for root in roots):
        return path
    raise RenderError(f"{what} must stay inside {roots[0]}: {raw}")


def _resolve_clip(scene: Dict[str, Any], index: int, workdir: Path) -> Optional[str]:
    """Return the local filename of this scene's B-roll, or None for none."""
    local = (scene.get("clip_path") or "").strip()
    if local:
        source = _validate_local_path(local, f"Scene {index + 1}: clip_path")
        if not source.exists():
            raise RenderError(f"Scene {index + 1}: clip_path does not exist: {local}")
        dest = workdir / f"src_{index:02d}{source.suffix or '.mp4'}"
        shutil.copyfile(source, dest)
        return dest.name

    url = (scene.get("clip_url") or "").strip()
    if url:
        dest = workdir / f"src_{index:02d}.mp4"
        _download_clip(url, dest)
        return dest.name

    return None


def _build_scene_video(
    ffmpeg: str, clip: Optional[str], duration: float, index: int, workdir: Path
) -> str:
    """Normalise one scene's visuals to the render canvas. Returns filename."""
    out = f"scene_{index:02d}.mp4"
    if clip:
        # -stream_loop before -i loops a clip shorter than the scene; -t then
        # bounds the output either way, so short and long sources both work.
        source_args = ["-stream_loop", "-1", "-i", clip]
        video_filter = (
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},fps={FPS},setsar=1,format=yuv420p"
        )
    else:
        source_args = [
            "-f", "lavfi",
            "-i", f"color=c={FALLBACK_COLOR}:s={WIDTH}x{HEIGHT}:r={FPS}",
        ]
        video_filter = "format=yuv420p"

    _run(
        [
            ffmpeg, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
            *source_args,
            "-t", f"{duration:.3f}",
            "-vf", video_filter,
            "-an",
            "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            out,
        ],
        cwd=workdir,
        what=f"scene {index + 1} video",
    )
    return out


def _build_scene_audio(
    ffmpeg: str, vo_file: str, duration: float, index: int, workdir: Path
) -> str:
    """Pad the scene's voiceover out to the exact scene length."""
    out = f"audio_{index:02d}.wav"
    _run(
        [
            ffmpeg, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
            "-i", vo_file,
            "-af", "apad",
            "-t", f"{duration:.3f}",
            "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
            out,
        ],
        cwd=workdir,
        what=f"scene {index + 1} audio",
    )
    return out


def _write_concat_list(names: List[str], path: Path) -> None:
    """Write an ffmpeg concat demuxer list of bare filenames."""
    path.write_text(
        "".join(f"file '{name}'\n" for name in names), encoding="utf-8"
    )


def _mix_music(
    ffmpeg: str, voice: str, music: str, workdir: Path
) -> str:
    """Duck a looping music bed under the voiceover."""
    out = "mixed.wav"
    _run(
        [
            ffmpeg, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
            "-i", voice,
            "-stream_loop", "-1", "-i", music,
            "-filter_complex",
            (
                f"[1:a]volume={MUSIC_VOLUME},aresample=48000[bed];"
                "[bed][0:a]sidechaincompress="
                "threshold=0.05:ratio=8:attack=5:release=250[duck];"
                "[0:a][duck]amix=inputs=2:duration=first:dropout_transition=0[out]"
            ),
            "-map", "[out]",
            "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
            out,
        ],
        cwd=workdir,
        what="music mix",
    )
    return out


def render_short(storyboard: Dict[str, Any], output_path: Path) -> Dict[str, Any]:
    """Render one storyboard to ``output_path``. Returns a result summary."""
    ffmpeg, _ = require_ffmpeg()

    scenes = storyboard.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise RenderError("storyboard needs a non-empty 'scenes' list")
    if len(scenes) > MAX_SCENES:
        raise RenderError(
            f"{len(scenes)} scenes exceeds the {MAX_SCENES}-scene limit. "
            "Merge short lines or split the idea into two videos."
        )

    music_path = (storyboard.get("music_path") or "").strip()
    music_path_resolved: Optional[Path] = None
    if music_path:
        music_path_resolved = _validate_local_path(music_path, "music_path")
        if not music_path_resolved.exists():
            raise RenderError(f"music_path does not exist: {music_path}")

    workdir = Path(tempfile.mkdtemp(prefix="shorts_"))
    try:
        video_names: List[str] = []
        audio_names: List[str] = []
        timed_captions: List[tuple] = []
        cursor = 0.0

        for index, scene in enumerate(scenes):
            if not isinstance(scene, dict):
                raise RenderError(f"Scene {index + 1} is not an object")
            vo_text = (scene.get("vo") or "").strip()
            if not vo_text:
                raise RenderError(f"Scene {index + 1} has no 'vo' line to speak")

            vo_path = synthesize(vo_text, workdir / f"vo_{index:02d}.mp3")
            duration = probe_duration(vo_path) + SCENE_TAIL_PAD

            cursor_end = cursor + duration
            if cursor_end > MAX_TOTAL_DURATION:
                raise RenderError(
                    f"Script runs to {cursor_end:.1f}s, past the "
                    f"{MAX_TOTAL_DURATION:.0f}s limit, at scene {index + 1}. "
                    "Cut lines or shorten them."
                )

            clip = _resolve_clip(scene, index, workdir)
            video_names.append(
                _build_scene_video(ffmpeg, clip, duration, index, workdir)
            )
            audio_names.append(
                _build_scene_audio(
                    ffmpeg, Path(vo_path).name, duration, index, workdir
                )
            )

            caption = scene.get("caption")
            timed_captions.append(
                (cursor, duration, caption if caption is not None else vo_text)
            )
            cursor = cursor_end

        _write_concat_list(video_names, workdir / "videos.txt")
        _write_concat_list(audio_names, workdir / "audios.txt")

        _run(
            [
                ffmpeg, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", "videos.txt",
                "-c", "copy", "silent.mp4",
            ],
            cwd=workdir,
            what="video concat",
        )
        _run(
            [
                ffmpeg, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", "audios.txt",
                "-c:a", "pcm_s16le", "voice.wav",
            ],
            cwd=workdir,
            what="audio concat",
        )

        audio_track = "voice.wav"
        if music_path_resolved:
            shutil.copyfile(music_path_resolved, workdir / "music.src")
            audio_track = _mix_music(ffmpeg, "voice.wav", "music.src", workdir)

        (workdir / "captions.ass").write_text(
            captions_mod.build_ass(timed_captions), encoding="utf-8"
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        _run(
            [
                ffmpeg, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
                "-i", "silent.mp4",
                "-i", audio_track,
                "-filter_complex",
                "[0:v]ass=captions.ass[v];"
                "[1:a]loudnorm=I=-14:TP=-1.5:LRA=11[a]",
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
                "-pix_fmt", "yuv420p", "-r", str(FPS),
                "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
                "-movflags", "+faststart",
                "-shortest",
                str(output_path),
            ],
            cwd=workdir,
            what="final encode",
        )

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RenderError("ffmpeg reported success but wrote no output file")

        return {
            "output_path": str(output_path),
            "duration_seconds": round(cursor, 2),
            "scene_count": len(scenes),
            "width": WIDTH,
            "height": HEIGHT,
            "fps": FPS,
            "size_bytes": output_path.stat().st_size,
            "has_music": bool(music_path),
        }
    except VoiceError as exc:
        raise RenderError(str(exc)) from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
