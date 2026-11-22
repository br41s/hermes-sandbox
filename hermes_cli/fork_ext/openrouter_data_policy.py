"""OpenRouter's data-collection policy for paths the main turn never covered (fork-owned).

Why: ``provider_routing.data_collection`` (pinned to ``deny`` at boot) reaches OpenRouter only on
the main turn, via ``agent/chat_completion_helpers.py::_provider_preferences_for_agent``. Auxiliary
calls and the image_gen/video_gen plugins build their own bodies, so they add it here.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def openrouter_provider_policy() -> dict[str, str]:
    """``{"data_collection": value}`` from ``provider_routing.data_collection``, else ``{}``.

    The value is the operator's privacy policy, so a non-empty string (``deny`` or ``allow``) is
    kept as-is. Any read failure returns ``{}``: a request must never fail over this, and boot
    already pins the value on every profile.
    """
    try:
        from hermes_cli.config import cfg_get, load_config_readonly
        value = cfg_get(load_config_readonly(), "provider_routing", "data_collection")
    except Exception:
        logger.debug("openrouter_provider_policy: config read failed", exc_info=True)
        return {}
    if not isinstance(value, str):
        return {}
    value = value.strip()
    return {"data_collection": value} if value else {}


def with_openrouter_data_policy(body: dict | None) -> dict:
    """``body`` with ``provider.data_collection`` set to the configured policy.

    Always returns a new dict (``load_config_readonly`` results are never mutated). The config
    value wins over a caller-supplied ``data_collection``; every other caller provider key
    (``only``, ``ignore``, ``order``, ``sort``, ``zdr``, …) is preserved.
    """
    policy = openrouter_provider_policy()
    result = dict(body or {})
    if not policy:
        return result
    existing = result.get("provider")
    result["provider"] = {**(existing if isinstance(existing, dict) else {}), **policy}
    return result
