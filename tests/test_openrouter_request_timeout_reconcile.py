"""Contract test: 03-biglobster-config bounds every OpenRouter request to 600s
on main AND every profile.

Without ``providers.openrouter.request_timeout_seconds`` the httpx timeout
falls back to ``HERMES_API_TIMEOUT`` (1800s), looser than the cron inactivity
watchdog (1200s): a hung non-streaming response was never a retryable SDK
timeout, it was always the watchdog killing the whole run. The value was set
by hand on 2026-09-21; the hook reconciles it so new profiles get it too.

Content-assertion style (matching tests/test_auditor_provider_pinning.py):
executing the real cont-init script needs root + s6-setuidgid, so we assert
the reconcile block's invariants on the script text and functionally replay
the snippet on sample configs.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOT_SCRIPT = REPO_ROOT / "docker" / "cont-init.d" / "03-biglobster-config"
SEED_CONFIG = REPO_ROOT / "docker" / "config.yaml"

TIMEOUT = 600
CRON_INACTIVITY_LIMIT = 1200


@pytest.fixture(scope="module")
def boot_text() -> str:
    if not BOOT_SCRIPT.exists():
        pytest.skip("docker/cont-init.d/03-biglobster-config not present")
    return BOOT_SCRIPT.read_text(encoding="utf-8")


def test_timeout_block_is_not_profile_gated(boot_text: str) -> None:
    idx = boot_text.index('orc["request_timeout_seconds"] = 600')
    # Must apply to main and every profile: no `label ==` gate may enclose it.
    # The nearest preceding gate is the auditor provider_routing pin, which
    # closes before this block starts; assert the block does not sit inside it.
    block_start = boot_text.rindex("provs = cfg.get(\"providers\")", 0, idx)
    preceding_gate = boot_text.rindex('if label == "auditor":', 0, block_start)
    between = boot_text[preceding_gate:block_start]
    assert 'pr["order"] = ["deepseek"]' in between, (
        "the timeout block must come after the auditor pin, at the function's "
        "top indentation, not inside a label gate"
    )
    line_start = boot_text.rfind("\n", 0, block_start) + 1
    indent = block_start - line_start
    gate_line_start = boot_text.rfind("\n", 0, preceding_gate) + 1
    gate_indent = preceding_gate - gate_line_start
    assert indent == gate_indent, "block must sit at the same depth as the gates, not under one"


def test_timeout_is_idempotent_guarded(boot_text: str) -> None:
    assert 'orc.get("request_timeout_seconds") != 600' in boot_text


def test_timeout_is_below_cron_inactivity_limit() -> None:
    assert TIMEOUT < CRON_INACTIVITY_LIMIT


def test_seed_config_matches_hook() -> None:
    seed = yaml.safe_load(SEED_CONFIG.read_text(encoding="utf-8"))
    assert seed["providers"]["openrouter"]["request_timeout_seconds"] == TIMEOUT


def test_reconcile_logic_on_sample_configs() -> None:
    """Functionally replay the reconcile snippet on representative configs."""
    def reconcile(cfg: dict) -> bool:
        changed = False
        provs = cfg.get("providers")
        if not isinstance(provs, dict):
            provs = {}
            cfg["providers"] = provs
        orc = provs.get("openrouter")
        if not isinstance(orc, dict):
            orc = {}
            provs["openrouter"] = orc
        if orc.get("request_timeout_seconds") != TIMEOUT:
            orc["request_timeout_seconds"] = TIMEOUT
            changed = True
        return changed

    # `providers: None` — what the live root/profile configs actually held.
    cfg: dict = {"providers": None, "model": {"default": "x"}}
    assert reconcile(cfg) is True
    assert cfg["providers"]["openrouter"]["request_timeout_seconds"] == TIMEOUT
    assert reconcile(cfg) is False  # second boot: no rewrite

    # `providers: {}` — the old first-boot seed.
    cfg2: dict = {"providers": {}}
    assert reconcile(cfg2) is True
    assert cfg2 == {"providers": {"openrouter": {"request_timeout_seconds": TIMEOUT}}}

    # Other providers and other openrouter keys survive untouched.
    cfg3: dict = {"providers": {"openrouter": {"models": [{"name": "m"}]}, "anthropic": {"k": 1}}}
    assert reconcile(cfg3) is True
    assert cfg3["providers"]["anthropic"] == {"k": 1}
    assert cfg3["providers"]["openrouter"]["models"] == [{"name": "m"}]

    # A hand-set different value is forced back, like every other override.
    cfg4: dict = {"providers": {"openrouter": {"request_timeout_seconds": 1800}}}
    assert reconcile(cfg4) is True
    assert cfg4["providers"]["openrouter"]["request_timeout_seconds"] == TIMEOUT
