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

import ast
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOT_SCRIPT = REPO_ROOT / "docker" / "cont-init.d" / "03-biglobster-config"
SEED_CONFIG = REPO_ROOT / "docker" / "config.yaml"

EXPECTED = {("delegation", "max_iterations"): 50,
            ("delegation", "max_concurrent_children"): 3}


@pytest.fixture(scope="module")
def boot_text() -> str:
    if not BOOT_SCRIPT.exists():
        pytest.skip("docker/cont-init.d/03-biglobster-config not present")
    return BOOT_SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def pins(boot_text: str) -> dict:
    start = boot_text.index("pin_if_missing = {")
    end = boot_text.index("}", start) + 1
    return ast.literal_eval(boot_text[start + len("pin_if_missing = "):end])


def _apply(boot_text: str, pins: dict, cfg: dict) -> bool:
    start = boot_text.index("        for (section, key), val in pin_if_missing.items():")
    end = boot_text.index("        # Undo what earlier boots imposed.", start)
    ns = {"pin_if_missing": pins, "cfg": cfg, "changed": False}
    exec(textwrap.dedent(boot_text[start:end]), ns)  # noqa: S102 — replaying our own script text
    return ns["changed"]


def test_pins_are_the_expected_ceilings(pins: dict) -> None:
    assert pins == EXPECTED


def test_pins_match_the_seed_config(pins: dict) -> None:
    seed = yaml.safe_load(SEED_CONFIG.read_text(encoding="utf-8"))
    for (section, key), val in pins.items():
        assert seed[section][key] == val


def test_absent_keys_are_written(boot_text: str, pins: dict) -> None:
    cfg = {"agent": {"max_turns": 90}}
    assert _apply(boot_text, pins, cfg) is True
    assert cfg["delegation"] == {"max_iterations": 50, "max_concurrent_children": 3}


def test_a_profiles_own_choice_is_kept(boot_text: str, pins: dict) -> None:
    cfg = {"delegation": {"max_iterations": 120, "max_concurrent_children": 5}}
    assert _apply(boot_text, pins, cfg) is False
    assert cfg["delegation"] == {"max_iterations": 120, "max_concurrent_children": 5}


def test_is_idempotent(boot_text: str, pins: dict) -> None:
    cfg: dict = {}
    _apply(boot_text, pins, cfg)
    assert _apply(boot_text, pins, cfg) is False
