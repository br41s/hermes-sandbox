"""Provider metadata must survive with models.dev unavailable.

``hermes_cli.providers.get_provider()`` sources a provider's
``api_key_env_vars`` from models.dev. On a cold install with no network and
no disk cache that lookup used to return nothing, so
``is_provider_explicitly_configured()`` answered False for a provider whose
API key WAS set and the desktop model picker hid it. The first successful
fetch writes a disk cache that masks the bug, which is why it never showed
up in normal use.

``openrouter`` is the sharpest case: it is deliberately excluded from
``hermes_cli.auth.PROVIDER_REGISTRY`` (adding it there breaks
``runtime_provider`` resolution), so models.dev is its *only* source of
``OPENROUTER_API_KEY``.

These tests do not stub the fetch. ``tests/conftest.py`` installs an autouse
guard that rejects every non-loopback socket, so the network leg fails for
real — which is the condition under test.
"""

import json
import tomllib
from pathlib import Path

import pytest

import agent.models_dev

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = REPO_ROOT / "agent" / "models_dev_snapshot.json"


@pytest.fixture(autouse=True)
def _offline_cold_install(tmp_path, monkeypatch):
    """A process that has never reached models.dev: no caches, no network."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    saved_cache = agent.models_dev._models_dev_cache
    saved_time = agent.models_dev._models_dev_cache_time
    saved_snapshot = agent.models_dev._bundled_snapshot

    agent.models_dev._models_dev_cache = {}
    agent.models_dev._models_dev_cache_time = 0
    agent.models_dev._bundled_snapshot = None

    yield hermes_home

    agent.models_dev._models_dev_cache = saved_cache
    agent.models_dev._models_dev_cache_time = saved_time
    agent.models_dev._bundled_snapshot = saved_snapshot


def test_provider_resolves_api_key_env_var_with_models_dev_unavailable():
    """The reported regression, at the layer that broke."""
    from hermes_cli.providers import get_provider

    pdef = get_provider("openrouter")

    assert pdef is not None, "openrouter did not resolve at all while offline"
    assert "OPENROUTER_API_KEY" in pdef.api_key_env_vars


def test_explicit_config_gate_sees_the_key_with_models_dev_unavailable(monkeypatch):
    """The user-visible consequence: shown in, or hidden from, the picker.

    Asserted as a differential so a gate that returns True for unrelated
    reasons (an auth.json entry, a config.yaml provider) cannot pass this.
    """
    from hermes_cli.auth import is_provider_explicitly_configured

    # v2026.8.31's default config ships a MoA preset naming openrouter, which
    # counts as an explicit selection on its own; isolate the env-key path.
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: {"model": {}})
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert is_provider_explicitly_configured("openrouter") is False

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test-key-not-real")
    assert is_provider_explicitly_configured("openrouter") is True


@pytest.mark.parametrize("provider_id", ["openrouter", "anthropic", "deepseek", "groq"])
def test_snapshot_covers_the_common_providers(provider_id):
    """Not openrouter-only — every provider offline gets its env vars."""
    info = agent.models_dev.get_provider_info(provider_id)

    assert info is not None, f"{provider_id} unresolvable offline"
    assert info.env, f"{provider_id} resolved with no API-key env vars"


def test_snapshot_carries_provider_metadata_only():
    """The snapshot is a provider floor, not a copy of the catalog.

    api.json is ~4.7 MB across 4000+ models. Shipping that in the wheel to
    fix an env-var lookup would be absurd, and a snapshot that grew model
    data would also start shadowing the live catalog's freshness. Keep it
    small and keep it provider-scoped.
    """
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))

    assert len(snapshot) >= 50
    assert SNAPSHOT_PATH.stat().st_size < 256 * 1024

    for provider_id, entry in snapshot.items():
        assert "models" not in entry, f"{provider_id} carries model payload"
        assert entry.get("env"), f"{provider_id} has no env vars — it buys nothing"


def test_snapshot_ships_in_the_wheel():
    """A wheel that drops the file silently restores the original bug."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]

    assert "models_dev_snapshot.json" in package_data.get("agent", [])


def test_disk_cache_still_takes_precedence_over_the_snapshot(_offline_cold_install):
    """The snapshot is the floor, not the first tier.

    If it ever short-circuits ahead of the cache, every model-level lookup
    (context windows, pricing, capabilities) silently degrades to "unknown"
    on machines that can reach models.dev perfectly well.
    """
    fresh = {
        "openrouter": {
            "id": "openrouter",
            "name": "OpenRouter",
            "env": ["OPENROUTER_API_KEY"],
            "models": {"some/model": {"id": "some/model", "limit": {"context": 123456}}},
        }
    }
    (_offline_cold_install / "models_dev_cache.json").write_text(json.dumps(fresh))

    data = agent.models_dev.fetch_models_dev()

    assert data["openrouter"].get("models"), "bundled snapshot shadowed the disk cache"
    assert agent.models_dev.lookup_models_dev_context("openrouter", "some/model") == 123456
