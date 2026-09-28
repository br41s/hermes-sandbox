"""Fork: an explicit ``gateway.multiplex_profiles: false`` still means standalone.

Upstream v2026.9.24 retired the opt-out (hermes_cli/fork_ext/multiplex.py explains
why the fork keeps it: it is the rollback lever while multiplex is being adopted).
These pin the boot verdict, what CLI/dashboard processes read, and the boot hook
that writes the value.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import gateway_multiplex_mode as mm


def _no_standalone_launcher(monkeypatch):
    monkeypatch.setattr(mm, "_standalone_launcher", lambda: False)


def test_explicit_false_stays_standalone_without_preflight_or_rewrite(monkeypatch):
    _no_standalone_launcher(monkeypatch)

    def _boom(*_a, **_k):
        raise AssertionError("an explicit false must not reach upstream's multiplex path")

    monkeypatch.setattr(mm, "implicit_multiplex_blocker", _boom)
    monkeypatch.setattr(mm, "persist_resolved_default", _boom)
    cfg = SimpleNamespace(multiplex_profiles=False)

    decision = mm.resolve_multiplex_mode(cfg)

    assert decision.enabled is False
    assert decision.source == "config"
    assert cfg.multiplex_profiles is False


def test_unset_still_follows_upstream(monkeypatch):
    _no_standalone_launcher(monkeypatch)
    monkeypatch.setattr(mm, "implicit_multiplex_blocker", lambda: None)
    cfg = SimpleNamespace(multiplex_profiles=None)

    decision = mm.resolve_multiplex_mode(cfg)

    assert decision.enabled is True and decision.source == "default"


@pytest.mark.parametrize("value, expected", [("false", False), ("true", True)])
def test_cli_view_matches_the_boot_verdict(tmp_path, monkeypatch, value, expected):
    monkeypatch.delenv("GATEWAY_MULTIPLEX_PROFILES", raising=False)
    monkeypatch.setattr(
        "hermes_cli.gateway_multiplex_served.recorded_served_profiles", lambda _root: None)
    (tmp_path / "config.yaml").write_text(
        f"gateway:\n  multiplex_profiles: {value}\n", encoding="utf-8")

    assert mm.default_gateway_multiplexes(tmp_path) is expected


def test_boot_hook_pins_multiplex_on():
    """Stage 2a of the adoption: the pin is explicit, so boot never decides it by itself.
    Rolling back is flipping this to False, which the opt-out above still honours."""
    from hermes_cli.fork_ext.boot_reconcile import OVERRIDES

    assert OVERRIDES[("gateway", "multiplex_profiles")] is True
