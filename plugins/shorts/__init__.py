"""Short-form vertical video assembly — bundled, auto-loaded.

Registers one tool, ``shorts_render``, into the ``shorts`` toolset. It powers
the rented ``shorts`` agent (``shorts/bl-site-package-shorts.prompt``), which
turns one blog post into 3-5 Instagram/TikTok-ready videos.

Why a plugin rather than a ``tools/`` file: every core tool's schema is sent
on every API call for every profile. Only profiles that rented this agent
need it, so it lives at the edge and costs nothing elsewhere. See the "narrow
waist" invariant in the repo's CLAUDE.md.

Two actions:

``stock_search``
    Free portrait B-roll from Pexels. Needs ``PEXELS_API_KEY``.

``render``
    Storyboard -> 1080x1920 MP4 with voiceover and burned-in captions, via
    ffmpeg. Needs no API key at all: voiceover uses the profile's configured
    TTS provider, which defaults to Edge TTS (free).

An AI-generated hook clip is not a separate concept here — it is just the
``clip_url`` of the first scene. The agent calls the existing
``video_generate`` tool (fal provider, ``aspect_ratio="9:16"``) and passes the
resulting URL through, falling back to stock when no ``FAL_KEY`` is set.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

SHORTS_RENDER_SCHEMA: Dict[str, Any] = {
    "name": "shorts_render",
    "description": (
        "Build vertical short-form videos for Instagram Reels and TikTok. "
        "action='stock_search' finds free portrait stock B-roll on Pexels for "
        "a visual search term. action='render' assembles a storyboard into a "
        "finished 1080x1920 MP4: each scene's 'vo' line is spoken by the "
        "configured TTS voice, the scene's clip is cropped to fill the frame "
        "for exactly as long as that line takes to say, and the captions are "
        "burned in. Scene 1's clip_url can be a video_generate result to open "
        "on an AI-generated hook. Output is capped at 60 seconds."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["stock_search", "render"],
                "description": "Which operation to perform.",
            },
            "query": {
                "type": "string",
                "description": (
                    "stock_search only. A concrete visual noun phrase, not an "
                    "abstract topic — 'watering plants at night' finds "
                    "footage, 'irrigation best practices' does not."
                ),
            },
            "per_page": {
                "type": "integer",
                "description": "stock_search only. Candidates to return (1-15, default 5).",
            },
            "min_duration": {
                "type": "integer",
                "description": "stock_search only. Skip clips shorter than this many seconds (default 3).",
            },
            "output_path": {
                "type": "string",
                "description": (
                    "render only. Where to write the MP4. A relative path like "
                    "'shorts/my-post/01-mistakes.mp4' is resolved inside this "
                    "profile's workspace folder, which is what you normally "
                    "want; the absolute path is returned in the result. An "
                    "absolute path is accepted but must stay inside this "
                    "profile's Hermes home."
                ),
            },
            "scenes": {
                "type": "array",
                "description": (
                    "render only. One entry per spoken line, in order. Max 12."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "vo": {
                            "type": "string",
                            "description": (
                                "The line to speak. One idea per scene — scene "
                                "length follows the speech."
                            ),
                        },
                        "caption": {
                            "type": "string",
                            "description": (
                                "On-screen text. Defaults to the 'vo' line. "
                                "Keep it short; it wraps at two lines."
                            ),
                        },
                        "clip_url": {
                            "type": "string",
                            "description": (
                                "Video URL for this scene, from stock_search "
                                "or video_generate. Omitted scenes render on a "
                                "plain dark background."
                            ),
                        },
                        "clip_path": {
                            "type": "string",
                            "description": "Local video file, as an alternative to clip_url.",
                        },
                    },
                    "required": ["vo"],
                },
            },
            "music_path": {
                "type": "string",
                "description": (
                    "render only. Optional local audio file for a music bed. "
                    "It is ducked under the voiceover and looped to length."
                ),
            },
        },
        "required": ["action"],
    },
}


def _workspace_root() -> Path:
    """This profile's workspace directory — the default home for renders."""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "workspace"


def _allowed_roots() -> list[Path]:
    """Directories the renderer may write into."""
    from hermes_constants import get_hermes_home

    # The system temp dir keeps ad-hoc renders and tests workable without
    # loosening the profile boundary that matters in production.
    return [Path(get_hermes_home()), Path(tempfile.gettempdir())]


def _validate_output_path(raw: str) -> Path:
    """Resolve and bound the requested output path. Raises ValueError.

    Relative paths resolve under the profile's ``workspace/`` folder so the
    agent never has to discover ``HERMES_HOME`` for itself.
    """
    from tools.path_security import has_traversal_component, validate_within_dir

    if not raw or not raw.strip():
        raise ValueError("output_path is required for action='render'")
    if has_traversal_component(raw):
        raise ValueError(f"output_path contains a '..' component: {raw}")

    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        path = _workspace_root() / path
    if path.suffix.lower() != ".mp4":
        raise ValueError(f"output_path must end in .mp4: {raw}")

    roots = _allowed_roots()
    if any(validate_within_dir(path, root) is None for root in roots):
        return path
    raise ValueError(
        f"output_path must stay inside {roots[0]}. Pass a relative path like "
        f"'shorts/<post-slug>/01-name.mp4' to write into the workspace, got: {raw}"
    )


def _handle_stock_search(args: Dict[str, Any]) -> str:
    from plugins.shorts.stock import StockError, search_clips

    try:
        clips = search_clips(
            args.get("query") or "",
            per_page=int(args.get("per_page") or 5),
            min_duration=int(args.get("min_duration") or 3),
        )
    except StockError as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        return json.dumps(
            {"success": False, "error": f"Invalid stock_search argument: {exc}"},
            ensure_ascii=False,
        )
    return json.dumps(
        {"success": True, "count": len(clips), "clips": clips}, ensure_ascii=False
    )


def _handle_render(args: Dict[str, Any]) -> str:
    from plugins.shorts.render import RenderError, render_short

    try:
        output_path = _validate_output_path(args.get("output_path") or "")
    except ValueError as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)

    storyboard = {
        "scenes": args.get("scenes"),
        "music_path": args.get("music_path"),
    }
    try:
        result = render_short(storyboard, output_path)
    except RenderError as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("shorts_render failed unexpectedly: %s", exc, exc_info=True)
        return json.dumps(
            {"success": False, "error": f"Unexpected render failure: {exc}"},
            ensure_ascii=False,
        )

    result["success"] = True
    return json.dumps(result, ensure_ascii=False)


def handle_shorts_render(args: Dict[str, Any], **_kwargs: Any) -> str:
    """Dispatch ``shorts_render``. Always returns a JSON string."""
    action = (args.get("action") or "").strip()
    if action == "stock_search":
        return _handle_stock_search(args)
    if action == "render":
        return _handle_render(args)
    return json.dumps(
        {
            "success": False,
            "error": f"Unknown action {action!r}. Use 'stock_search' or 'render'.",
        },
        ensure_ascii=False,
    )


def check_shorts_available() -> bool:
    """Rendering needs ffmpeg; stock search additionally needs a Pexels key.

    ffmpeg is the real gate — without it nothing here works. It ships in the
    container image, so this is effectively always true in production and
    only matters on a bare developer machine.
    """
    import shutil

    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def register(ctx) -> None:
    """Register the shorts tool. Called once by the plugin loader."""
    ctx.register_tool(
        name="shorts_render",
        toolset="shorts",
        schema=SHORTS_RENDER_SCHEMA,
        handler=handle_shorts_render,
        check_fn=check_shorts_available,
        emoji="🎬",
    )
