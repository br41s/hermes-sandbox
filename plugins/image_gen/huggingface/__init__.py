"""HuggingFace Inference API image generation backend.

Uses the HF Serverless Inference API (free tier) to generate images.

Default model: ``black-forest-labs/FLUX.1-schnell``
  - Best free text-to-image model on HF Inference API as of 2026-05
  - Fast (~5-15s), high quality, no FAL account needed
  - Returns PNG bytes directly from the API

Auth: ``HUGGINGFACE_API_KEY`` env var (get one at https://huggingface.co/settings/tokens).
If the key is absent, ``is_available()`` returns False and the tool skips
this provider gracefully — no crash.

Model selection precedence:
  1. ``HUGGINGFACE_IMAGE_MODEL`` env var (escape hatch)
  2. ``image_gen.huggingface.model`` in config.yaml
  3. ``image_gen.model`` in config.yaml (when it matches our catalog)
  4. ``DEFAULT_MODEL``
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    save_b64_image,
    success_response,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------

_MODELS: dict[str, dict[str, Any]] = {
    "bytedance-seed/seedream-4.5": {
        "display": "SeedDream 4.5",
        "speed": "~10-30s",
        "strengths": "ByteDance SOTA, photorealism, multilingual prompts",
        "price": "free (HF Inference API tier)",
    },
    "black-forest-labs/FLUX.1-schnell": {
        "display": "FLUX.1-schnell",
        "speed": "~5-15s",
        "strengths": "Best free text-to-image on HF, fast, high fidelity",
        "price": "free (HF Inference API tier)",
    },
    "stabilityai/stable-diffusion-xl-base-1.0": {
        "display": "Stable Diffusion XL",
        "speed": "~15-30s",
        "strengths": "Reliable fallback, broad style range",
        "price": "free (HF Inference API tier)",
    },
}

DEFAULT_MODEL = "bytedance-seed/seedream-4.5"

# HF Inference API endpoint pattern
# HF deprecated api-inference.huggingface.co in favour of the inference router.
# New endpoint: https://router.huggingface.co/hf-inference/models/{model}/v1
_HF_API_URL = "https://router.huggingface.co/hf-inference/models/{model}/v1"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _load_hf_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:
        logger.debug("Could not load image_gen config: %s", exc)
        return {}


def _resolve_model() -> tuple[str, dict[str, Any]]:
    env_override = os.environ.get("HUGGINGFACE_IMAGE_MODEL", "").strip()
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


class HuggingFaceImageGenProvider(ImageGenProvider):
    """HuggingFace Serverless Inference API image generation backend."""

    @property
    def name(self) -> str:
        return "huggingface"

    @property
    def display_name(self) -> str:
        return "HuggingFace"

    def is_available(self) -> bool:
        if not os.environ.get("HUGGINGFACE_API_KEY"):
            logger.debug("image_gen/huggingface: HUGGINGFACE_API_KEY not set — provider unavailable")
            return False
        try:
            import requests
        except ImportError:
            logger.debug("image_gen/huggingface: requests package not installed")
            return False
        return True

    def list_models(self) -> list[dict[str, Any]]:
        return [
            {
                "id": model_id,
                "display": meta["display"],
                "speed": meta["speed"],
                "strengths": meta["strengths"],
                "price": meta["price"],
            }
            for model_id, meta in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "HuggingFace",
            "badge": "free",
            "tag": "FLUX.1-schnell and others via HF Inference API",
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
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        **kwargs: Any,
    ) -> dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)

        if not prompt:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument",
                provider="huggingface",
                aspect_ratio=aspect,
            )

        api_key = os.environ.get("HUGGINGFACE_API_KEY")
        if not api_key:
            logger.warning(
                "image_gen/huggingface: HUGGINGFACE_API_KEY not set — "
                "cannot generate image. Add the key to Zeabur Variables."
            )
            return error_response(
                error=(
                    "HUGGINGFACE_API_KEY not set. Add it to your environment "
                    "or Zeabur Variables (https://huggingface.co/settings/tokens)."
                ),
                error_type="auth_required",
                provider="huggingface",
                aspect_ratio=aspect,
            )

        try:
            import requests
        except ImportError:
            return error_response(
                error="requests package not installed (pip install requests)",
                error_type="missing_dependency",
                provider="huggingface",
                aspect_ratio=aspect,
            )

        model_id, _meta = _resolve_model()
        url = _HF_API_URL.format(model=model_id)
        headers = {"Authorization": f"Bearer {api_key}"}
        payload: dict[str, Any] = {"inputs": prompt}

        try:
            logger.debug("image_gen/huggingface: POST %s", url)
            response = requests.post(url, headers=headers, json=payload, timeout=30)
        except Exception as exc:
            logger.warning("image_gen/huggingface: request failed: %s", exc)
            return error_response(
                error=f"HuggingFace API request failed: {exc}",
                error_type="network_error",
                provider="huggingface",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        if response.status_code == 503:
            # Model is loading — surface a friendly message
            return error_response(
                error=(
                    f"HuggingFace model '{model_id}' is loading "
                    "(cold start). Retry in 20-60 seconds."
                ),
                error_type="model_loading",
                provider="huggingface",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        if response.status_code != 200:
            body = response.text[:500]
            logger.warning(
                "image_gen/huggingface: HTTP %d — %s", response.status_code, body
            )
            return error_response(
                error=f"HuggingFace API error {response.status_code}: {body}",
                error_type="api_error",
                provider="huggingface",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        # Response is raw image bytes (PNG/JPEG)
        import base64

        try:
            b64 = base64.b64encode(response.content).decode()
            saved_path = save_b64_image(b64, prefix=f"hf_{model_id.split('/')[-1]}", extension="png")
        except Exception as exc:
            logger.warning("image_gen/huggingface: could not save image: %s", exc)
            return error_response(
                error=f"Could not save image to cache: {exc}",
                error_type="io_error",
                provider="huggingface",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        return success_response(
            image=str(saved_path),
            model=model_id,
            prompt=prompt,
            aspect_ratio=aspect,
            provider="huggingface",
            extra={"size_bytes": len(response.content)},
        )


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Plugin entry point — wire HuggingFaceImageGenProvider into the registry."""
    ctx.register_image_gen_provider(HuggingFaceImageGenProvider())
