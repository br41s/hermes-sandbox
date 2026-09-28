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
second guise, which is why the fix reuses the same `is_rented_tenant` marker.

The reconcile lives in ``hermes_cli/fork_ext/boot_reconcile.py`` and is called
directly on representative configs and a fixture volume.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from hermes_cli.fork_ext import boot_reconcile as br


def _replay(cfg: dict, byok_images: bool) -> bool:
    return br.reconcile_cfg(cfg, "bl-shoroban", {}, is_rented=True, byok_images=byok_images)


def test_override_still_exists_for_everyone_else() -> None:
    # The override is correct for our own profiles; the fix is scoped, not a
    # removal. If it goes, non-rented profiles lose their image backend.
    assert br.OVERRIDES[("image_gen", "provider")] == "openrouter"


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
    _replay(cfg, byok_images=True)
    assert "image_gen" not in cfg
    assert _replay(cfg, byok_images=True) is False
    assert "image_gen" not in cfg


def test_byok_preserves_unrelated_image_gen_keys() -> None:
    # Only the two keys this hook imposes are removed; anything a tenant set
    # for itself survives.
    cfg = {"image_gen": {"provider": "openrouter", "model": "fal-ai/flux-2-pro"}}
    assert _replay(cfg, byok_images=True) is True
    assert cfg["image_gen"] == {"model": "fal-ai/flux-2-pro"}


def test_curated_openrouter_model_not_applied_to_byok() -> None:
    cfg = {"image_gen": {"model": "fal-ai/flux-2-pro"}}
    _replay(cfg, byok_images=True)
    assert "openrouter" not in cfg["image_gen"]


def test_non_byok_profile_still_forced_to_openrouter() -> None:
    cfg: dict = {}
    assert _replay(cfg, byok_images=False) is True
    assert cfg["image_gen"] == {
        "provider": "openrouter",
        "openrouter": {"model": br.CURATED_OPENROUTER_IMAGE_MODEL},
    }


def test_non_byok_profile_is_idempotent() -> None:
    cfg: dict = {}
    _replay(cfg, byok_images=False)
    assert _replay(cfg, byok_images=False) is False


def _volume(tmp_path: Path, envs: dict[str, str]) -> Path:
    home = tmp_path / "data"
    for name, env in envs.items():
        prof = home / "profiles" / name
        prof.mkdir(parents=True)
        (prof / "SOUL.md").write_text("soul", encoding="utf-8")
        (prof / ".env").write_text(env, encoding="utf-8")
        (prof / "config.yaml").write_text(
            yaml.dump({"image_gen": {"provider": "openrouter"}}), encoding="utf-8")
    return home


def test_byok_requires_both_rented_and_own_fal_key(tmp_path) -> None:
    # A rental with no FAL_KEY has no backend of its own and must KEEP the
    # override — losing image generation entirely would be a worse regression
    # than the billing leak this fixes. A provisioned-but-EMPTY FAL_KEY counts
    # as none, or the tenant is switched to a backend it cannot authenticate.
    home = _volume(tmp_path, {
        "rental-fal": "BL_SITE_URL=https://a\nFAL_KEY=fk\n",
        "rental-empty-fal": "BL_SITE_URL=https://b\nFAL_KEY=\n",
        "rental-no-fal": "BL_SITE_URL=https://c\n",
        "own-with-fal": "FAL_KEY=fk\n",
    })
    br.reconcile_configs(home, {}, profiles_src=tmp_path / "none")

    def image_gen(name):
        return yaml.safe_load((home / "profiles" / name / "config.yaml").read_text()).get("image_gen")

    assert image_gen("rental-fal") is None
    for keeps in ("rental-empty-fal", "rental-no-fal", "own-with-fal"):
        assert image_gen(keeps)["provider"] == "openrouter", keeps
