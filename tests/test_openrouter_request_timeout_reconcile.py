"""Contract test: 03-biglobster-config bounds every OpenRouter request to 600s
on main AND every profile.

Without ``providers.openrouter.request_timeout_seconds`` the httpx timeout
falls back to ``HERMES_API_TIMEOUT`` (1800s), looser than the cron inactivity
watchdog (1200s): a hung non-streaming response was never a retryable SDK
timeout, it was always the watchdog killing the whole run. The value was set
by hand on 2026-09-21; the hook reconciles it so new profiles get it too.

The reconcile lives in ``hermes_cli/fork_ext/boot_reconcile.py`` and is called
directly.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hermes_cli.fork_ext import boot_reconcile as br

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_CONFIG = REPO_ROOT / "docker" / "config.yaml"

TIMEOUT = 600
CRON_INACTIVITY_LIMIT = 1200


def _timeout(cfg: dict):
    return cfg["providers"]["openrouter"]["request_timeout_seconds"]


@pytest.mark.parametrize("label", ["main", "auditor", "biglobster", "bl-client"])
def test_timeout_applies_to_every_profile(label: str) -> None:
    cfg: dict = {}
    br.reconcile_cfg(cfg, label, {}, is_rented=label.startswith("bl-"))
    assert _timeout(cfg) == TIMEOUT


def test_timeout_is_below_cron_inactivity_limit() -> None:
    assert br.OPENROUTER_REQUEST_TIMEOUT == TIMEOUT < CRON_INACTIVITY_LIMIT


def test_seed_config_matches_hook() -> None:
    seed = yaml.safe_load(SEED_CONFIG.read_text(encoding="utf-8"))
    assert seed["providers"]["openrouter"]["request_timeout_seconds"] == TIMEOUT


def test_reconcile_on_sample_configs() -> None:
    # `providers: None` — what the live root/profile configs actually held.
    cfg: dict = {"providers": None, "model": {"default": "x"}}
    br.reconcile_cfg(cfg, "main", {})
    assert _timeout(cfg) == TIMEOUT
    assert br.reconcile_cfg(cfg, "main", {}) is False  # second boot: no rewrite

    # Other providers and other openrouter keys survive untouched.
    cfg3: dict = {"providers": {"openrouter": {"models": [{"name": "m"}]}, "anthropic": {"k": 1}}}
    br.reconcile_cfg(cfg3, "main", {})
    assert cfg3["providers"]["anthropic"] == {"k": 1}
    assert cfg3["providers"]["openrouter"] == {"models": [{"name": "m"}],
                                               "request_timeout_seconds": TIMEOUT}

    # A hand-set different value is forced back, like every other override.
    cfg4: dict = {"providers": {"openrouter": {"request_timeout_seconds": 1800}}}
    br.reconcile_cfg(cfg4, "main", {})
    assert _timeout(cfg4) == TIMEOUT
