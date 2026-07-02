"""Contract test: 03-biglobster-config pins the auditor profile's OpenRouter
provider routing to DeepSeek's own endpoint.

The auditor orchestrator (deepseek-v4-flash cron agent) cached erratically
(56% hit) despite a stable session_id — OpenRouter session-stickiness is
best-effort, so an explicit ``provider_routing.order: ["deepseek"]`` in the
auditor profile config.yaml is required for a warm DeepSeek prompt cache
(tasks/token-optimization.md, ORCHESTRATOR item).

Content-assertion style (matching tests/test_biglobster_git_credentials.py):
executing the real cont-init script needs root + s6-setuidgid. We assert the
reconcile block's invariants on the script text, plus functionally exercise
the pinning logic on a sample config.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOT_SCRIPT = REPO_ROOT / "docker" / "cont-init.d" / "03-biglobster-config"


@pytest.fixture(scope="module")
def boot_text() -> str:
    if not BOOT_SCRIPT.exists():
        pytest.skip("docker/cont-init.d/03-biglobster-config not present")
    return BOOT_SCRIPT.read_text(encoding="utf-8")


def test_pinning_block_exists_and_is_auditor_gated(boot_text: str) -> None:
    idx = boot_text.index('pr["order"] = ["deepseek"]')
    # The pin must sit inside the `if label == "auditor":` branch — the main
    # profile's pinning is deliberately deferred (CEO decision recorded in
    # tasks/token-optimization.md) and must NOT be applied by this block.
    gate = boot_text.rindex('if label == "auditor":', 0, idx)
    # No other profile-label gate may sit between the auditor gate and the pin.
    between = boot_text[gate:idx]
    assert 'label ==' not in between.replace('if label == "auditor":', "", 1)


def test_pinning_is_idempotent_guarded(boot_text: str) -> None:
    # Must check current value before writing so unchanged boots don't
    # rewrite config.yaml (the reconcile only saves when changed).
    assert 'pr.get("order") != ["deepseek"]' in boot_text


def test_pinning_logic_on_sample_config() -> None:
    """Functionally replay the reconcile snippet on representative configs."""
    def reconcile(cfg: dict) -> bool:
        changed = False
        pr = cfg.get("provider_routing")
        if not isinstance(pr, dict):
            pr = {}
            cfg["provider_routing"] = pr
        if pr.get("order") != ["deepseek"]:
            pr["order"] = ["deepseek"]
            changed = True
        return changed

    # Fresh config: section created, pin applied.
    cfg: dict = {"model": {"default": "deepseek/deepseek-v4-flash"}}
    assert reconcile(cfg) is True
    assert cfg["provider_routing"]["order"] == ["deepseek"]

    # Second boot: no change (idempotent).
    assert reconcile(cfg) is False

    # Existing unrelated provider_routing keys survive.
    cfg2: dict = {"provider_routing": {"sort": "price"}}
    assert reconcile(cfg2) is True
    assert cfg2["provider_routing"] == {"sort": "price", "order": ["deepseek"]}
