"""OpenRouter video generation backend.

Uses OpenRouter's chat completions API with ``modalities: ["video"]`` —
same OPENROUTER_API_KEY already used by the Hermes gateway for LLM calls,
so no extra credentials needed.

Endpoint: ``https://openrouter.ai/api/v1/chat/completions``
Default model: ``alibaba/happyhorse-1.1``

Auth: ``OPENROUTER_API_KEY`` env var (already configured in Zeabur).
If the key is absent, ``is_available()`` returns False — no crash.

Model selection precedence:
  1. ``OPENROUTER_VIDEO_MODEL`` env var (escape hatch)
  2. ``video_gen.openrouter.model`` in config.yaml
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
    success_response,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------

_MODELS: Dict[str, Dict[str, Any]] = {
    "alibaba/happyhorse-1.1": {
        "display": "HappyHorse 1.1",
        "speed": "~30-60s",
        "strengths": "Alibaba text-to-video via OpenRouter",
        "price": "~$0.10/video",
        "modalities": ["text"],
    },
}

DEFAULT_MODEL = "alibaba/happyhorse-1.1"

_OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _load_or_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("video_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:
        logger.debug("Could not load video_gen config: %s", exc)
        return {}


def _resolve_model() -> Tuple[str, Dict[str, Any]]:
    env_override = os.environ.get("OPENROUTER_VIDEO_MODEL", "").strip()
    if env_override and env_override in _MODELS:
        return env_override, _MODELS[env_override]

    cfg = _load_or_config()
    or_cfg = cfg.get("openrouter")
    if isinstance(or_cfg, dict):
        candidate = or_cfg.get("model", "").strip()
        if candidate in _MODELS:
            return candidate, _MODELS[candidate]

    top = cfg.get("model", "").strip() if isinstance(cfg.get("model"), str) else ""
    if top in _MODELS:
        return top, _MODELS[top]

    return DEFAULT_MODEL, _MODELS[DEFAULT_MODEL]


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class OpenRouterVideoGenProvider(VideoGenProvider):
    """OpenRouter video generation via chat completions + modalities: [video]."""

    @property
    def name(self) -> str:
        return "openrouter"

    @property
    def display_name(self) -> str:
        return "OpenRouter"

    def is_available(self) -> bool:
        from agent.secret_scope import get_secret
        if not get_secret("OPENROUTER_API_KEY"):
            logger.debug("video_gen/openrouter: OPENROUTER_API_KEY not set — provider unavailable")
            return False
        try:
            import requests  # noqa: F401
        except ImportError:
            logger.debug("video_gen/openrouter: requests package not installed")
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

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "OpenRouter",
            "badge": "paid",
            "tag": "HappyHorse 1.1 via openrouter.ai — uses existing OPENROUTER_API_KEY",
            "env_vars": [
                {
                    "key": "OPENROUTER_API_KEY",
                    "prompt": "OpenRouter API key",
                    "url": "https://openrouter.ai/keys",
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
                provider="openrouter",
            )

        from agent.secret_scope import get_secret
        api_key = get_secret("OPENROUTER_API_KEY")
        if not api_key:
            logger.warning("video_gen/openrouter: OPENROUTER_API_KEY not set")
            return error_response(
                error="OPENROUTER_API_KEY not set.",
                error_type="auth_required",
                provider="openrouter",
            )

        try:
            import requests
        except ImportError:
            return error_response(
                error="requests package not installed (pip install requests)",
                error_type="missing_dependency",
                provider="openrouter",
            )

        model_id, _meta = _resolve_model()
        if model and model in _MODELS:
            model_id = model

        modality = "image" if image_url else "text"
        user_content: Any = prompt
        if image_url:
            user_content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url}},
            ]

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "model": model_id,
            "modalities": ["video"],
            "messages": [{"role": "user", "content": user_content}],
        }

        try:
            logger.debug("video_gen/openrouter: POST %s model=%s", _OPENROUTER_CHAT_URL, model_id)
            response = requests.post(
                _OPENROUTER_CHAT_URL,
                headers=headers,
                json=payload,
                timeout=120,
            )
        except Exception as exc:
            logger.warning("video_gen/openrouter: request failed: %s", exc)
            return error_response(
                error=f"OpenRouter API request failed: {exc}",
                error_type="network_error",
                provider="openrouter",
                model=model_id,
                prompt=prompt,
            )

        if response.status_code != 200:
            body = response.text[:500]
            logger.warning("video_gen/openrouter: HTTP %d — %s", response.status_code, body)
            return error_response(
                error=f"OpenRouter API error {response.status_code}: {body}",
                error_type="api_error",
                provider="openrouter",
                model=model_id,
                prompt=prompt,
            )

        try:
            data = response.json()
            video_url = data["choices"][0]["message"]["videos"][0]["video_url"]["url"]
            if not video_url:
                raise ValueError("empty video_url")
        except Exception as exc:
            logger.warning(
                "video_gen/openrouter: could not parse response: %s — body: %s",
                exc, response.text[:300],
            )
            return error_response(
                error=f"Could not parse OpenRouter response: {exc}",
                error_type="parse_error",
                provider="openrouter",
                model=model_id,
                prompt=prompt,
            )

        return success_response(
            video=video_url,
            model=model_id,
            prompt=prompt,
            modality=modality,
            aspect_ratio=aspect_ratio,
            duration=duration or 0,
            provider="openrouter",
        )


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Plugin entry point — wire OpenRouterVideoGenProvider into the registry."""
    ctx.register_video_gen_provider(OpenRouterVideoGenProvider())
