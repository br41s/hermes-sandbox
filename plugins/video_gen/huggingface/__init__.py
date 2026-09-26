"""HuggingFace Inference API video generation backend.

Uses the HF Serverless Inference API (free tier) to generate short videos.

Default model: ``damo-vilab/text-to-video-ms-1.7b``
  - Best freely available text-to-video model on HF Inference API as of 2026-05
  - Generates ~2s clips at 256×256 (low resolution but functional, no cost)

Why not stabilityai/stable-video-diffusion-img2vid-xt?
  - SVD is image-to-video only (requires an input image, not text)
  - It is NOT available on the free HF Serverless Inference API tier
    (needs dedicated endpoint or Inference Endpoints subscription)
  - damo-vilab/text-to-video-ms-1.7b covers the text-to-video use case
    that the tool exposes and reliably runs on the free tier

Auth: ``HUGGINGFACE_API_KEY`` env var (https://huggingface.co/settings/tokens).
If the key is absent, ``is_available()`` returns False — no crash.

Model selection precedence:
  1. ``HUGGINGFACE_VIDEO_MODEL`` env var (escape hatch)
  2. ``video_gen.huggingface.model`` in config.yaml
  3. ``video_gen.model`` in config.yaml (when it matches our catalog)
  4. ``DEFAULT_MODEL``
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from agent.video_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    DEFAULT_RESOLUTION,
    VideoGenProvider,
    error_response,
    save_bytes_video,
    success_response,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------

_MODELS: Dict[str, Dict[str, Any]] = {
    "damo-vilab/text-to-video-ms-1.7b": {
        "display": "ModelScope Text-to-Video 1.7B",
        "speed": "~30-90s",
        "strengths": "Free tier, text-to-video, no input image needed",
        "price": "free (HF Inference API tier)",
        "modalities": ["text"],
        "note": (
            "Generates short low-resolution clips (~2s, 256×256). "
            "Best free text-to-video option on HF Serverless Inference API."
        ),
    },
}

DEFAULT_MODEL = "damo-vilab/text-to-video-ms-1.7b"

# HF deprecated api-inference.huggingface.co in favour of the inference router.
# New endpoint: https://router.huggingface.co/hf-inference/models/{model}/v1
_HF_API_URL = "https://router.huggingface.co/hf-inference/models/{model}/v1"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _load_hf_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("video_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:
        logger.debug("Could not load video_gen config: %s", exc)
        return {}


def _resolve_model() -> Tuple[str, Dict[str, Any]]:
    env_override = os.environ.get("HUGGINGFACE_VIDEO_MODEL", "").strip()
    if env_override and env_override in _MODELS:
        return env_override, _MODELS[env_override]

    cfg = _load_hf_config()
    hf_cfg = cfg.get("huggingface")
    if isinstance(hf_cfg, dict):
        candidate = hf_cfg.get("model", "").strip()
        if candidate in _MODELS:
            return candidate, _MODELS[candidate]

    top = cfg.get("model", "").strip() if isinstance(cfg.get("model"), str) else ""
    if top in _MODELS:
        return top, _MODELS[top]

    return DEFAULT_MODEL, _MODELS[DEFAULT_MODEL]


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class HuggingFaceVideoGenProvider(VideoGenProvider):
    """HuggingFace Serverless Inference API video generation backend."""

    @property
    def name(self) -> str:
        return "huggingface"

    @property
    def display_name(self) -> str:
        return "HuggingFace"

    def is_available(self) -> bool:
        if not os.environ.get("HUGGINGFACE_API_KEY"):
            logger.debug("video_gen/huggingface: HUGGINGFACE_API_KEY not set — provider unavailable")
            return False
        try:
            import requests  # noqa: F401
        except ImportError:
            logger.debug("video_gen/huggingface: requests package not installed")
            return False
        return True

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": model_id,
                "display": meta["display"],
                "speed": meta["speed"],
                "strengths": meta["strengths"],
                "price": meta["price"],
                "modalities": meta.get("modalities", ["text"]),
            }
            for model_id, meta in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def capabilities(self) -> Dict[str, Any]:
        return {
            "modalities": ["text"],
            "aspect_ratios": ["16:9"],
            "resolutions": ["256p"],
            "max_duration": 2,
            "min_duration": 2,
            "supports_audio": False,
            "supports_negative_prompt": False,
            "max_reference_images": 0,
        }

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "HuggingFace",
            "badge": "free",
            "tag": "Text-to-video via HF Inference API (damo-vilab/text-to-video-ms-1.7b)",
            "env_vars": [
                {
                    "key": "HUGGINGFACE_API_KEY",
                    "prompt": "HuggingFace API token",
                    "url": "https://huggingface.co/settings/tokens",
                },
            ],
        }

    def generate(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        duration: Optional[int] = None,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        resolution: str = DEFAULT_RESOLUTION,
        negative_prompt: Optional[str] = None,
        audio: Optional[bool] = None,
        seed=None,  # interface parity only: this backend has no seed control (supports_seed: False)
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()

        if not prompt:
            return error_response(
                error="Prompt is required for video generation",
                error_type="invalid_argument",
                provider="huggingface",
            )

        if image_url:
            logger.warning(
                "video_gen/huggingface: image_url provided but HF text-to-video "
                "model does not support image-to-video — ignoring image_url and "
                "generating from text only."
            )

        api_key = os.environ.get("HUGGINGFACE_API_KEY")
        if not api_key:
            logger.warning(
                "video_gen/huggingface: HUGGINGFACE_API_KEY not set — "
                "cannot generate video. Add the key to Zeabur Variables."
            )
            return error_response(
                error=(
                    "HUGGINGFACE_API_KEY not set. Add it to your environment "
                    "or Zeabur Variables (https://huggingface.co/settings/tokens)."
                ),
                error_type="auth_required",
                provider="huggingface",
            )

        try:
            import requests
        except ImportError:
            return error_response(
                error="requests package not installed (pip install requests)",
                error_type="missing_dependency",
                provider="huggingface",
            )

        active_model = model or _resolve_model()[0]
        url = _HF_API_URL.format(model=active_model)
        headers = {"Authorization": f"Bearer {api_key}"}
        payload: Dict[str, Any] = {"inputs": prompt}

        try:
            logger.debug("video_gen/huggingface: POST %s", url)
            # Video generation is slow — give it up to 5 minutes
            response = requests.post(url, headers=headers, json=payload, timeout=300)
        except Exception as exc:
            logger.warning("video_gen/huggingface: request failed: %s", exc)
            return error_response(
                error=f"HuggingFace API request failed: {exc}",
                error_type="network_error",
                provider="huggingface",
                model=active_model,
                prompt=prompt,
            )

        if response.status_code == 503:
            return error_response(
                error=(
                    f"HuggingFace model '{active_model}' is loading "
                    "(cold start). Retry in 20-60 seconds."
                ),
                error_type="model_loading",
                provider="huggingface",
                model=active_model,
                prompt=prompt,
            )

        if response.status_code != 200:
            body = response.text[:500]
            logger.warning(
                "video_gen/huggingface: HTTP %d — %s", response.status_code, body
            )
            return error_response(
                error=f"HuggingFace API error {response.status_code}: {body}",
                error_type="api_error",
                provider="huggingface",
                model=active_model,
                prompt=prompt,
            )

        # Response is raw video bytes (MP4 or GIF depending on model)
        content_type = response.headers.get("content-type", "")
        extension = "gif" if "gif" in content_type else "mp4"

        try:
            saved_path = save_bytes_video(
                response.content,
                prefix=f"hf_{active_model.split('/')[-1]}",
                extension=extension,
            )
        except Exception as exc:
            logger.warning("video_gen/huggingface: could not save video: %s", exc)
            return error_response(
                error=f"Could not save video to cache: {exc}",
                error_type="io_error",
                provider="huggingface",
                model=active_model,
                prompt=prompt,
            )

        return success_response(
            video=str(saved_path),
            model=active_model,
            prompt=prompt,
            modality="text",
            aspect_ratio="16:9",
            duration=2,
            provider="huggingface",
            extra={"size_bytes": len(response.content)},
        )


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Plugin entry point — wire HuggingFaceVideoGenProvider into the registry."""
    ctx.register_video_gen_provider(HuggingFaceVideoGenProvider())
