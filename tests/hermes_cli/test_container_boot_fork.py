"""Fork's own tests for hermes_cli.container_boot, kept out of upstream's file so upstream merges do not conflict."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli.container_boot import (
    ReconcileAction,
    reconcile_profile_gateways,
)


@pytest.fixture(autouse=True)
def _hermetic_container_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default ``_read_container_argv()`` to empty for the whole module.

    ``_read_container_argv()`` walks the entire ``/proc`` table looking for
    a process whose argv contains ``main-wrapper.sh`` (the s6-overlay v3
    fallback). On a host that is *also* running hermes containers, those
    containers' ``main-wrapper.sh`` processes are visible in the host's
    ``/proc`` (shared PID view), so the scan would pick up a foreign
    ``gateway run`` argv and make ``_maybe_migrate_legacy_gateway_run_state``
    synthesize ``running`` state — flaking any test that reconciles without
    injecting ``container_argv``. Inside the real container ``/proc`` is the
    container's own PID namespace, so production is unaffected; this fixture
    just makes the unit suite hermetic. Tests that need a specific argv
    either pass ``container_argv=`` to ``reconcile_profile_gateways`` or
    monkeypatch ``_read_container_argv`` themselves (both override this).
    """
    monkeypatch.setattr(
        "hermes_cli.container_boot._read_container_argv",
        lambda: (),
    )


def _make_profile(
    hermes_home: Path,
    name: str,
    *,
    state: str | None,
    involuntary_exit: bool | None = None,
    desired_state: str | None = None,
    with_pid: bool = False,
    config: bool = True,
) -> Path:
    """Create a fake profile directory under hermes_home/profiles/<name>/.

    ``involuntary_exit``: when not None, write the gateway_state.json field
    of the same name (the flag the gateway sets when an external SIGTERM
    recycles a healthy gateway). Left absent by default so existing tests
    exercise the pre-field behavior.
    """
    p = hermes_home / "profiles" / name
    p.mkdir(parents=True)
    if config:
        # SOUL.md is what the reconciler keys on — it's always seeded by
        # `hermes profile create`. See container_boot._render_run_script.
        (p / "SOUL.md").write_text("# fake profile\n")
    if state is not None or desired_state is not None:
        payload: dict[str, object] = {"timestamp": 1234567890}
        if state is not None:
            payload["gateway_state"] = state
        if involuntary_exit is not None:
            payload["involuntary_exit"] = involuntary_exit
        if desired_state is not None:
            payload["desired_state"] = desired_state
        (p / "gateway_state.json").write_text(json.dumps(payload))
    if with_pid:
        (p / "gateway.pid").write_text(json.dumps(
            {"pid": 99999, "host": "old-container"},
        ))
        (p / "processes.json").write_text("[]")
    return p


def _named_actions(actions: list[ReconcileAction]) -> list[ReconcileAction]:
    """Drop the always-present default-profile action so tests that
    only care about named profiles can assert against a clean list."""
    return [a for a in actions if a.profile != "default"]


# ---------------------------------------------------------------------------
# Involuntary-exit autostart — graceful SIGTERM recycle of a healthy gateway
# (e.g. Zeabur/K8s periodically recycling the container) must come back up,
# while a deliberate `hermes gateway stop` stays down.
# ---------------------------------------------------------------------------


def test_involuntary_stopped_autostarts(tmp_path: Path) -> None:
    """A healthy gateway killed by an external SIGTERM persists
    state=stopped with involuntary_exit=True — reconcile must revive it
    (the user never asked it to stop)."""
    scandir = tmp_path / "run-service"; scandir.mkdir()
    _make_profile(tmp_path, "coder", state="stopped", involuntary_exit=True)

    actions = reconcile_profile_gateways(
        hermes_home=tmp_path, scandir=scandir, dry_run=False,
    )

    assert _named_actions(actions) == [ReconcileAction(
        profile="coder", prior_state="stopped", action="started",
    )]
    assert not (scandir / "gateway-coder" / "down").exists()


def test_gatewayless_profile_stays_down_even_when_involuntary(tmp_path: Path) -> None:
    """An automation-only profile (e.g. `auditor`) must NEVER autostart a
    gateway — even with state=stopped + involuntary_exit=True, which revives a
    normal profile. It must not seize the shared bot token on a container
    recycle (prod incident 2026-06-24)."""
    scandir = tmp_path / "run-service"; scandir.mkdir()
    _make_profile(tmp_path, "auditor", state="stopped", involuntary_exit=True)
