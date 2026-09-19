"""Contract test: 03-biglobster-config leaves BYOK-image tenants on FAL.

The boot hook re-asserts ``("image_gen", "provider"): "openrouter"`` onto the
main config AND every per-profile config, on every restart. On 2026-09-15 a
container restart applied it to `bl-shoroban`, a rental that had been
generating covers through its own FAL key (fal-ai/flux-2/klein/9b). From that
boot on, generation went out through the OpenRouter plugin, hit that account's
18+ gate on `meta/muse-image` with a 403, and the Content Gap Hunter published
four articles with no cover at all — the prompt tolerates image failure by
design, so nothing reported it.

Two distinct harms, and the billing one outlives the outage: rerouting a
rental off its own FAL key moves its image spend onto our OpenRouter account.
That is issue #174 (EXA_API_KEY billing every tenant's searches to us) in a
second guise, which is why the fix reuses the same `_is_rented_tenant` marker.

Content-assertion style (matching tests/test_auditor_provider_pinning.py):
running the real cont-init script needs root + s6-setuidgid, so we assert the
reconcile block's invariants on the script text and replay its logic on
representative configs.
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


def test_override_still_exists_for_everyone_else(boot_text: str) -> None:
    # The override is correct for our own profiles; the fix is scoped, not a
    # removal. If this line goes, non-rented profiles lose their image backend.
    assert '("image_gen", "provider"): "openrouter"' in boot_text


def test_byok_tenants_skip_the_image_gen_override(boot_text: str) -> None:
    assert 'if byok_images and (section, key) == ("image_gen", "provider"):' in boot_text


def test_byok_requires_both_rented_and_own_fal_key(boot_text: str) -> None:
    # A rental with no FAL_KEY has no backend of its own and must KEEP the
    # override — losing image generation entirely would be a worse regression
    # than the billing leak this fixes.
    assert "_has_own_fal_key(prof / \".env\")" in boot_text
    idx = boot_text.index("_has_own_fal_key(prof / \".env\")")
    gate = boot_text.rindex("byok_images=", 0, idx)
    assert "_is_rented_tenant(prof / \".env\")" in boot_text[gate:idx]


def test_fal_key_marker_requires_a_value(boot_text: str) -> None:
    # `^FAL_KEY=.+` not `^FAL_KEY=`: a provisioned-but-empty key must not count
    # as BYOK, or the tenant is switched to a backend it cannot authenticate.
    assert 'r"^FAL_KEY=.+"' in boot_text


def test_curated_openrouter_model_not_applied_to_byok(boot_text: str) -> None:
    assert "ig = None if byok_images else cfg.get(\"image_gen\")" in boot_text


def _replay(cfg: dict, byok_images: bool) -> bool:
    """Replay the reconcile snippet's image_gen handling on one config."""
    overrides = {("image_gen", "provider"): "openrouter"}
    changed = False
    for (section, key), val in overrides.items():
        if byok_images and (section, key) == ("image_gen", "provider"):
            continue
        if not isinstance(cfg.get(section), dict):
            cfg[section] = {}
        if cfg[section].get(key) != val:
            cfg[section][key] = val
            changed = True
    if byok_images and isinstance(cfg.get("image_gen"), dict):
        for stale in ("provider", "openrouter"):
            if stale in cfg["image_gen"]:
                del cfg["image_gen"][stale]
                changed = True
        if not cfg["image_gen"]:
            del cfg["image_gen"]
    return changed


def test_byok_tenant_gets_image_gen_cleared() -> None:
    """The bl-shoroban case: a profile already poisoned by an earlier boot."""
    cfg = {
        "image_gen": {
            "provider": "openrouter",
            "openrouter": {"model": "x-ai/grok-imagine-image-quality"},
        },
        "model": {"default": "deepseek/deepseek-v4.1-flash"},
    }
    assert _replay(cfg, byok_images=True) is True
    # No image_gen section at all -> _read_configured_image_provider() returns
    # None -> the in-tree FAL path, DEFAULT_MODEL = fal-ai/flux-2/klein/9b.
    assert "image_gen" not in cfg
    assert cfg["model"]["default"] == "deepseek/deepseek-v4.1-flash"


def test_byok_tenant_is_idempotent_on_a_clean_config() -> None:
    # A second boot must not rewrite config.yaml: the reconcile only saves when
    # `changed`, and a needless save churns the volume on every restart.
    cfg = {"model": {"default": "x"}}
    assert _replay(cfg, byok_images=True) is False
    assert "image_gen" not in cfg


def test_byok_preserves_unrelated_image_gen_keys() -> None:
    # Only the two keys this hook imposes are removed; anything a tenant set
    # for itself survives.
    cfg = {"image_gen": {"provider": "openrouter", "model": "fal-ai/flux-2-pro"}}
    assert _replay(cfg, byok_images=True) is True
    assert cfg["image_gen"] == {"model": "fal-ai/flux-2-pro"}


def test_non_byok_profile_still_forced_to_openrouter() -> None:
    cfg: dict = {}
    assert _replay(cfg, byok_images=False) is True
    assert cfg["image_gen"]["provider"] == "openrouter"


def test_non_byok_profile_is_idempotent() -> None:
    cfg = {"image_gen": {"provider": "openrouter"}}
    assert _replay(cfg, byok_images=False) is False
