"""Contract test: 03-biglobster-config pins the delegation cost ceilings.

Upstream v2026.9.x raises the delegation defaults from 50 to 250 iterations
per subagent and from 3 to 10 parallel children. A profile whose config.yaml
never set them would silently inherit a ~5x cost ceiling on the next upstream
merge. The hook writes today's values where the keys are ABSENT and never
overrides a value a profile chose for itself.

Content-assertion style (matching tests/test_openrouter_request_timeout_reconcile.py):
executing the real cont-init script needs root + s6-setuidgid, so we pull the
dict and the loop out of the script text and replay them on sample configs.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from hermes_cli.fork_ext import boot_reconcile as br

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_CONFIG = REPO_ROOT / "docker" / "config.yaml"

EXPECTED = {("delegation", "max_iterations"): 50,
            ("delegation", "max_concurrent_children"): 3,
            ("agent", "max_turns"): 90}


def _apply(cfg: dict) -> dict:
    br.reconcile_cfg(cfg, "some-profile", {})
    return cfg


def test_pins_are_the_expected_ceilings() -> None:
    assert br.PIN_IF_MISSING == EXPECTED


def test_pins_match_the_seed_config() -> None:
    seed = yaml.safe_load(SEED_CONFIG.read_text(encoding="utf-8"))
    for (section, key), val in br.PIN_IF_MISSING.items():
        assert seed[section][key] == val


def test_absent_keys_are_written() -> None:
    cfg = _apply({"agent": {"max_turns": 90, "reasoning_effort": "low"}})
    assert cfg["delegation"] == {"max_iterations": 50, "max_concurrent_children": 3}
    # An existing agent section is extended, never replaced: its own keys survive, and
    # the only additions are the boot-pinned shutdown drain budgets (OVERRIDES).
    assert cfg["agent"] == {"max_turns": 90, "reasoning_effort": "low",
                            "restart_drain_timeout": br.OVERRIDES[("agent", "restart_drain_timeout")],
                            "cron_drain_timeout": br.OVERRIDES[("agent", "cron_drain_timeout")]}


def test_a_profiles_own_delegation_choice_is_kept() -> None:
    cfg = _apply({"delegation": {"max_iterations": 120, "max_concurrent_children": 5}})
    assert cfg["delegation"] == {"max_iterations": 120, "max_concurrent_children": 5}


def test_is_idempotent() -> None:
    cfg = _apply({})
    assert br.reconcile_cfg(cfg, "some-profile", {}) is False
