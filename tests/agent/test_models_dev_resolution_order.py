"""The documented data-resolution order must be the implemented one.

``agent/models_dev.py``'s module docstring advertised this until 2026-09-18:

    1. Bundled snapshot (ships with the package — offline-first)
    2. Disk cache (~/.hermes/models_dev_cache.json)
    3. Network fetch (https://models.dev/api.json)
    4. Background refresh every 60 minutes

Tier 1 did not exist — no snapshot file, no ``package-data`` entry under
``agent`` in ``pyproject.toml``, no loader. Neither did tier 4: the 60 minutes
is a lazy TTL checked on the next access, not a refresher thread. The disk path
was written as ``~/.hermes``, which is wrong under profiles (each has its own
``HERMES_HOME``).

The failure is in the dangerous direction. "Offline-first" invites someone to
assume a cold start works with no network — sizing a container, debugging an
air-gapped run, or reasoning about why a model's context window came back
``None``. It does not: the registry comes back empty.

The behavioural rules themselves are covered by ``TestFetchModelsDev`` in
``test_models_dev.py``. This file guards the *claim* — the thing that was
wrong — and the one behaviour nothing asserted, which is what actually
happens with neither a cache nor a network.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

import agent.models_dev as md

REPO_ROOT = Path(__file__).resolve().parents[2]


def _documented_tiers() -> list[str]:
    """The numbered resolution-order entries, and only those.

    Scoped deliberately: the prose around the list discusses the tiers that
    were wrong, so scanning the whole docstring matches the correction as
    readily as a regression.
    """
    return re.findall(r"^\s*\d+\.\s+(.*)$", md.__doc__ or "", re.MULTILINE)


@pytest.fixture(autouse=True)
def _restore_cache():
    saved_cache = md._models_dev_cache
    saved_time = md._models_dev_cache_time
    yield
    md._models_dev_cache = saved_cache
    md._models_dev_cache_time = saved_time


# ── the claim ────────────────────────────────────────────────────────────────

def test_no_resolution_tier_is_documented_without_something_implementing_it() -> None:
    """A bundled/offline tier may only be claimed once one exists.

    Adding one is fine — ship the snapshot, declare it as package data, write
    the loader, then say so here. What this stops is the docstring getting
    there first and staying there for months on its own.
    """
    tiers = _documented_tiers()
    assert tiers, "the resolution order must stay a numbered list"

    claims_bundled = any(
        re.search(r"bundled|snapshot|offline", tier, re.IGNORECASE) for tier in tiers
    )
    has_loader = any(
        "bundled" in name.lower() or "snapshot" in name.lower()
        for name in dir(md)
    )

    if claims_bundled:
        assert has_loader, (
            "the docstring advertises a bundled/offline-first tier but the "
            "module has no loader for one — if you are adding it, also add a "
            "package-data entry for `agent` in pyproject.toml"
        )
    else:
        assert not has_loader, (
            "a bundled-snapshot loader exists but the docstring no longer "
            "documents that tier"
        )


def test_no_background_refresher_is_documented_without_a_thread() -> None:
    """The TTL is lazy. Nothing refreshes on a timer."""
    source = Path(md.__file__).read_text()

    spawns_thread = bool(
        re.search(r"\bthreading\.|\bThread\(|start_background", source)
    )
    claims_background = any(
        re.search(r"background", tier, re.IGNORECASE) for tier in _documented_tiers()
    )

    assert claims_background == spawns_thread, (
        "documented background refresh and an actual refresher thread must "
        "appear or disappear together"
    )


def test_the_documented_disk_cache_path_is_profile_aware() -> None:
    """``~/.hermes`` is wrong under profiles — each has its own HERMES_HOME.

    Mirrors the standing rule in AGENTS.md's Known Pitfalls; it applies to
    what the docs say as much as to what the code does.
    """
    docstring = md.__doc__ or ""
    assert "~/.hermes" not in docstring, (
        "document the cache under $HERMES_HOME, not a hardcoded ~/.hermes"
    )


# ── the behaviour the claim got wrong ────────────────────────────────────────

def test_a_cold_start_with_no_cache_and_no_network_returns_an_empty_registry() -> None:
    """The honest answer to "is this offline-first?". It is not."""
    md._models_dev_cache = {}
    md._models_dev_cache_time = 0

    with patch.object(md, "_disk_cache_age_seconds", return_value=None), \
         patch.object(md, "_load_disk_cache", return_value={}), \
         patch.object(md.requests, "get", side_effect=OSError("no network")):
        result = md.fetch_models_dev()

    assert result == {}, "no snapshot exists to fall back to"


def test_callers_degrade_to_none_rather_than_raising_when_offline() -> None:
    """The empty registry has to stay survivable for its callers — a cold
    offline start must not take the agent down on a metadata lookup."""
    md._models_dev_cache = {}
    md._models_dev_cache_time = 0

    with patch.object(md, "fetch_models_dev", return_value={}):
        assert md.lookup_models_dev_context("anthropic", "claude-opus-5") is None
