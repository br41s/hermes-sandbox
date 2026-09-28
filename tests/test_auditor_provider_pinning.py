"""Contract test: 03-biglobster-config pins the auditor profile's OpenRouter
provider routing to DeepSeek's own endpoint.

The auditor orchestrator (deepseek-v4-flash cron agent) cached erratically
(56% hit) despite a stable session_id — OpenRouter session-stickiness is
best-effort, so an explicit ``provider_routing.order: ["deepseek"]`` in the
auditor profile config.yaml is required for a warm DeepSeek prompt cache
(tasks/token-optimization.md, ORCHESTRATOR item).

The reconcile lives in ``hermes_cli/fork_ext/boot_reconcile.py`` and is called
directly.
"""
from __future__ import annotations

import pytest

from hermes_cli.fork_ext import boot_reconcile as br


@pytest.mark.parametrize("label", ["main", "biglobster", "grow-shop"])
def test_pin_is_auditor_only(label: str) -> None:
    # The main profile's pinning is deliberately deferred (CEO decision recorded
    # in tasks/token-optimization.md) and must NOT be applied.
    cfg: dict = {}
    br.reconcile_cfg(cfg, label, {})
    assert "provider_routing" not in cfg


def test_pin_on_the_auditor() -> None:
    # Fresh config: section created, pin applied.
    cfg: dict = {"model": {"default": "deepseek/deepseek-v4-flash"}}
    br.reconcile_cfg(cfg, "auditor", {})
    assert cfg["provider_routing"] == {"order": ["deepseek"]}

    # Second boot: no change (idempotent), so config.yaml is not rewritten.
    assert br.reconcile_cfg(cfg, "auditor", {}) is False

    # Existing unrelated provider_routing keys survive.
    cfg2: dict = {"provider_routing": {"sort": "price"}}
    br.reconcile_cfg(cfg2, "auditor", {})
    assert cfg2["provider_routing"] == {"sort": "price", "order": ["deepseek"]}


def test_auditor_orchestrator_is_the_dated_slug() -> None:
    cfg: dict = {}
    br.reconcile_cfg(cfg, "auditor", {"HERMES_DEFAULT_MODEL": "some/free-model"})
    assert cfg["model"]["default"] == "deepseek/deepseek-v4-flash-0731"
