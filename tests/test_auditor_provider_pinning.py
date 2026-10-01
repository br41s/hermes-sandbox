"""Contract test: 03-biglobster-config routes the auditor profile like the rest
of the fleet, minus OpenInference.

The auditor used to carry ``provider_routing.order: ["deepseek"]`` for a warm
DeepSeek prompt cache. It never took effect: the OpenRouter account refuses
providers that train on paid prompts, so routing drops DeepSeek's own API
(probed 2026-10-01). Instead it put the auditor in a fallback pool led by
OpenInference's fp4 endpoint, which trickles responses for 10-50 minutes. The
reconcile now removes that pin from existing configs and ignores OpenInference.

The reconcile lives in ``hermes_cli/fork_ext/boot_reconcile.py`` and is called
directly.
"""
from __future__ import annotations

import pytest

from auditor import llm
from hermes_cli.fork_ext import boot_reconcile as br

IGNORE = ["open-inference"]


@pytest.mark.parametrize("label", ["main", "biglobster", "grow-shop"])
def test_routing_is_auditor_only(label: str) -> None:
    cfg: dict = {}
    br.reconcile_cfg(cfg, label, {})
    assert "provider_routing" not in cfg


def test_auditor_ignores_openinference() -> None:
    cfg: dict = {"model": {"default": "deepseek/deepseek-v4-flash"}}
    br.reconcile_cfg(cfg, "auditor", {})
    assert cfg["provider_routing"] == {"ignore": IGNORE}
    # Second boot: no change (idempotent), so config.yaml is not rewritten.
    assert br.reconcile_cfg(cfg, "auditor", {}) is False


def test_the_dead_deepseek_pin_is_removed_from_existing_configs() -> None:
    # What every live auditor config.yaml holds until this reconcile runs.
    cfg: dict = {"provider_routing": {"order": ["deepseek"], "sort": "price"}}
    assert br.reconcile_cfg(cfg, "auditor", {}) is True
    assert cfg["provider_routing"] == {"sort": "price", "ignore": IGNORE}


def test_a_hand_set_order_survives() -> None:
    cfg: dict = {"provider_routing": {"order": ["together", "atlas-cloud"]}}
    br.reconcile_cfg(cfg, "auditor", {})
    assert cfg["provider_routing"] == {"order": ["together", "atlas-cloud"], "ignore": IGNORE}


def test_the_judge_ignores_the_same_providers() -> None:
    assert list(llm.IGNORED_PROVIDERS) == br.AUDITOR_IGNORED_PROVIDERS == IGNORE


def test_auditor_orchestrator_is_the_dated_slug() -> None:
    cfg: dict = {}
    br.reconcile_cfg(cfg, "auditor", {"HERMES_DEFAULT_MODEL": "some/free-model"})
    assert cfg["model"]["default"] == "deepseek/deepseek-v4-flash-0731"
