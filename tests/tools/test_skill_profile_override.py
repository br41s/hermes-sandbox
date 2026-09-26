"""Regression: skill tools must honor a runtime per-job profile override.

A cron job with a ``profile`` field runs under a context-local Hermes home
override (``set_hermes_home_override``). The skill tools used to cache
``HERMES_HOME`` / ``SKILLS_DIR`` at *import* time, so in the long-lived
gateway/scheduler process — imported while the default profile was active —
they searched the DEFAULT skills dir even when a biglobster/finview profile
cron was running. The job could never see its own profile-scoped skills and
looped on ``skill_view`` / ``skill_manage`` "not found" errors.

These tests pin the dynamic resolution: import the modules while ``default``
is active, then activate a profile override and confirm the tools resolve the
profile's skills dir at call time.
"""

from __future__ import annotations

import json

import pytest

import hermes_constants as hc


# Module path-attribute names that other test files patch via
# ``monkeypatch.setattr``. pytest's monkeypatch restores by re-assigning the
# captured value, which (for a PEP 562 ``__getattr__``-served attribute) leaves
# a stale concrete value in the module ``__dict__`` after teardown. Production
# never patches, so it always sees the dynamic value — but a prior test in the
# same process can leave pollution here. Clear it so these tests assert against
# genuine dynamic resolution rather than a leftover from an earlier test.
_DYNAMIC_PATH_NAMES = (
    "HERMES_HOME", "SKILLS_DIR", "HUB_DIR", "LOCK_FILE", "QUARANTINE_DIR",
    "AUDIT_LOG", "TAPS_FILE", "INDEX_CACHE_DIR", "HERMES_INDEX_CACHE_FILE",
    "MANIFEST_FILE",
)
# tools.skills_sync is not here: since v2026.8.31 it is upstream's module,
# whose path names are real constants read by per-call resolvers, not
# __getattr__-served — popping them would break the module.
_SKILL_MODULES = (
    "tools.skills_tool", "tools.skill_manager_tool",
    "tools.skills_hub",
)


@pytest.fixture(autouse=True)
def _clear_module_path_pollution():
    """Drop any cross-test pollution of the dynamic path attributes."""
    import importlib

    for modname in _SKILL_MODULES:
        try:
            mod = importlib.import_module(modname)
        except Exception:
            continue
        for name in _DYNAMIC_PATH_NAMES:
            mod.__dict__.pop(name, None)
    yield


@pytest.fixture()
def profile_layout(tmp_path, monkeypatch):
    """A Hermes root with a default profile and a `biglobster` profile skill."""
    root = tmp_path / "data"
    (root / "skills").mkdir(parents=True)  # default profile skills (empty)
    skill_dir = root / "profiles" / "biglobster" / "skills" / "seo-geo" / "seo-geo-audit"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: seo-geo-audit\ndescription: audit the site\n---\nbody\n",
        encoding="utf-8",
    )
    # Process default home == the root (mirrors prod: HERMES_HOME=/opt/data).
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root, root / "profiles" / "biglobster"


def test_skill_view_resolves_overridden_profile(profile_layout):
    import tools.skills_tool as st

    root, profile_home = profile_layout

    # Without the override the skill lives in another profile -> not found.
    res = json.loads(st.skill_view("seo-geo-audit"))
    assert res["success"] is False

    token = hc.set_hermes_home_override(profile_home)
    try:
        res = json.loads(st.skill_view("seo-geo-audit"))
    finally:
        hc.reset_hermes_home_override(token)

    assert res["success"] is True, res


def test_skill_manage_edit_resolves_overridden_profile(profile_layout):
    import tools.skill_manager_tool as sm

    root, profile_home = profile_layout
    valid = "---\nname: seo-geo-audit\ndescription: audit v2\n---\nupdated body\n"

    # Without the override: the helpful cross-profile error names biglobster.
    res = json.loads(sm.skill_manage(action="edit", name="seo-geo-audit", content=valid))
    assert res["success"] is False
    assert "default" in res["error"] and "biglobster" in res["error"]

    token = hc.set_hermes_home_override(profile_home)
    try:
        res = json.loads(sm.skill_manage(action="edit", name="seo-geo-audit", content=valid))
    finally:
        hc.reset_hermes_home_override(token)

    assert res["success"] is True, res
    assert "biglobster" in res["path"]


@pytest.mark.parametrize(
    "module_name, attr",
    [
        ("tools.skills_tool", "SKILLS_DIR"),
        ("tools.skill_manager_tool", "SKILLS_DIR"),
        ("tools.skills_hub", "HUB_DIR"),
        # v2026.8.31: upstream resolves this per call instead of via a module
        # attribute (tools/skills_sync.py:_manifest_file), which covers the
        # profile-cron case the fork's __getattr__ used to.
        ("tools.skills_sync", "_manifest_file"),
    ],
)
def test_module_paths_track_override(profile_layout, module_name, attr):
    """The exported path attributes resolve dynamically under an override."""
    import importlib

    root, profile_home = profile_layout
    module = importlib.import_module(module_name)

    def _resolve():
        val = getattr(module, attr)
        return val() if callable(val) else val

    default_val = _resolve()
    assert str(root) in str(default_val)

    token = hc.set_hermes_home_override(profile_home)
    try:
        overridden = _resolve()
    finally:
        hc.reset_hermes_home_override(token)

    assert str(profile_home) in str(overridden), (attr, overridden)
