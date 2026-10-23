"""Fork: the Langfuse catalog plugin baked into the image and placed at boot (owner, 2026-10-23).

Upstream v0.22 moved Langfuse out of core into the ``langfuse`` catalog plugin, which its
left-core migration installs at gateway start by cloning GitHub. The Dockerfile's catalog_plugins
stage bakes the tree at plugin-catalog/langfuse.yaml's sha; boot_reconcile places it with the
install record a catalog install writes, so upstream's migration finds it installed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import hermes_yaml as yaml
import pytest

from hermes_cli.fork_ext import boot_reconcile as br

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG = yaml.safe_load((REPO_ROOT / "plugin-catalog" / "langfuse.yaml").read_text(encoding="utf-8"))
SHA = CATALOG["sha"]


@pytest.fixture
def vendor(tmp_path) -> Path:
    root = tmp_path / "vendor-plugins"
    tree = root / "langfuse"
    tree.mkdir(parents=True)
    (tree / "plugin.yaml").write_text("name: langfuse\nversion: 2.0.0\n", encoding="utf-8")
    (tree / "__init__.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")
    (tree / "pyproject.toml").write_text(
        '[project]\nname = "hermes-langfuse"\nversion = "2.0.0"\ndependencies = ["langfuse==4.15.2"]\n',
        encoding="utf-8")
    (root / "langfuse.sha").write_text(SHA + "\n", encoding="utf-8")
    return root


def _home(path: Path, enabled: list[str] | None) -> Path:
    path.mkdir(parents=True)
    cfg = {"plugins": {"enabled": enabled}} if enabled is not None else {}
    (path / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


def _profile(home: Path, name: str, enabled: list[str] | None) -> Path:
    prof = _home(home / "profiles" / name, enabled)
    (prof / "SOUL.md").write_text("soul", encoding="utf-8")
    return prof


def _record(home: Path) -> dict:
    return json.loads((home / "plugins" / ".install-metadata.json").read_text(encoding="utf-8"))["langfuse"]


def test_placed_like_a_catalog_install_in_every_home_that_uses_it(tmp_path, vendor):
    home = _home(tmp_path / "data", ["observability/langfuse"])
    grow = _profile(home, "grow-shop", ["langfuse"])
    quiet = _profile(home, "quiet", [])
    br.place_catalog_plugins(home, vendor_root=vendor)

    for h in (home, grow):
        assert (h / "plugins" / "langfuse" / "plugin.yaml").is_file()
        # Exactly the record `hermes plugins install langfuse` writes (checked against a real install).
        assert _record(h) == {
            "pinned": True, "revision": SHA,
            "source": "https://github.com/NousResearch/hermes-official-plugins#langfuse",
            "catalog": {"name": "langfuse", "repo": "https://github.com/NousResearch/hermes-official-plugins",
                        "tier": "official", "sha": SHA, "pin": SHA},
        }
        sidecar = json.loads((h / "plugins" / "langfuse" / ".hermes-catalog.json").read_text(encoding="utf-8"))
        assert sidecar["catalog_name"] == "langfuse" and sidecar["sha"] == SHA
    assert not (quiet / "plugins" / "langfuse").exists()


def test_upstream_migration_then_finds_it_installed_and_fetches_nothing(tmp_path, vendor):
    from hermes_cli import left_core_migration as lcm

    home = _home(tmp_path / "data", ["observability/langfuse"])
    br.place_catalog_plugins(home, vendor_root=vendor)

    def no_network_install(name):
        raise AssertionError(f"the migration tried to install {name}")

    assert lcm.migrate_home(home, install=no_network_install, say=lambda _m: None) == []
    assert "langfuse" in (yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")).get("_left_core_installed") or [])


def test_a_later_boot_changes_nothing_and_a_newer_bake_replaces_an_older_one(tmp_path, vendor):
    home = _home(tmp_path / "data", ["observability/langfuse"])
    br.place_catalog_plugins(home, vendor_root=vendor)
    meta = home / "plugins" / ".install-metadata.json"
    first = meta.read_text(encoding="utf-8")
    br.place_catalog_plugins(home, vendor_root=vendor)
    assert meta.read_text(encoding="utf-8") == first

    old = "0" * 40
    record = json.loads(first)
    record["langfuse"]["revision"] = old
    meta.write_text(json.dumps(record), encoding="utf-8")
    (home / "plugins" / "langfuse" / "stale.py").write_text("x", encoding="utf-8")
    br.place_catalog_plugins(home, vendor_root=vendor)
    assert _record(home)["revision"] == SHA
    assert not (home / "plugins" / "langfuse" / "stale.py").exists()


def test_a_users_own_install_is_never_touched(tmp_path, vendor):
    home = _home(tmp_path / "data", ["langfuse"])
    own = home / "plugins" / "langfuse"
    own.mkdir(parents=True)
    (own / "plugin.yaml").write_text("name: langfuse\n", encoding="utf-8")
    (home / "plugins" / ".install-metadata.json").write_text(
        json.dumps({"langfuse": {"pinned": False, "revision": "a" * 40, "source": "https://example.com/fork.git"}}),
        encoding="utf-8")
    br.place_catalog_plugins(home, vendor_root=vendor)
    assert _record(home)["source"] == "https://example.com/fork.git"
    assert not (own / ".hermes-catalog.json").exists()


def test_no_bake_is_a_no_op(tmp_path):
    home = _home(tmp_path / "data", ["observability/langfuse"])
    br.place_catalog_plugins(home, vendor_root=tmp_path / "absent")
    assert not (home / "plugins").exists()


def test_the_venv_pin_is_the_plugins_own():
    """pyproject's observability extra carries the plugin's one dependency into the venv, so the plugin
    needs nothing from the network. The image build checks the fetched plugin still requires exactly
    that pin (Dockerfile catalog_plugins stage); here, that the lock and the image carry it."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    extra = re.search(r'^observability = \["langfuse==([^"]+)"\]$', pyproject, re.MULTILINE)
    assert extra, "pyproject's observability extra is gone"
    lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    assert re.search(r'name = "langfuse"\nversion = "' + re.escape(extra.group(1)) + '"', lock)
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "--extra observability" in dockerfile and "AS catalog_plugins" in dockerfile
    assert "/opt/hermes/vendor-plugins" in dockerfile
