"""Fork: the image gives the gateway time to shut down cleanly.

A gateway SIGKILLed mid-shutdown leaves an unclean exit, and the next boot runs an
integrity check on the 2.2 GB state.db before connecting Telegram (4m12s of silence on
2026-09-29, ~4 min again on 2026-09-30).

The gateway runs in a dynamic ``/run/service/gateway-default`` slot, not an s6-rc service
and not a legacy ``/etc/services.d`` one. So s6-rc never stops it, and
``S6_SERVICES_GRACETIME`` (read only by s6-overlay's ``services-down``, for legacy
services) never applied. The gateway's SIGTERM comes at the very end of the shutdown,
from ``s6-linux-init-shutdownd``, and its SIGKILL comes ``S6_KILL_GRACETIME`` later
(``-g`` in its run script). That grace is the gateway's whole budget. At 5s, every
deploy made while a cron run was in flight died mid-drain, including the 2026-09-30
rollout that already had the 10s drain pins.
"""

from __future__ import annotations

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"
K8S_DEFAULT_TERMINATION_GRACE_MS = 30_000
# Kubernetes' clock starts before the gateway's: s6-rc first stops main-hermes and the
# dashboard (well under a second in the pod's own logs).
RC_SHUTDOWN_RESERVE_MS = 5_000


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _env(name: str) -> int:
    match = re.search(rf"^ENV {name}=(\d+)$", _dockerfile(), re.MULTILINE)
    assert match, f"Dockerfile does not set {name}"
    return int(match.group(1))


def test_the_kill_grace_fits_inside_the_kubernetes_grace():
    assert _env("S6_KILL_GRACETIME") + RC_SHUTDOWN_RESERVE_MS <= K8S_DEFAULT_TERMINATION_GRACE_MS


def test_services_gracetime_is_not_mistaken_for_the_gateway_budget():
    """It only waits on /etc/services.d, and the image ships none; PR #370 sized against it."""
    instructions = [ln for ln in _dockerfile().splitlines() if not ln.lstrip().startswith("#")]
    assert not any("/etc/services.d" in ln for ln in instructions)
    assert not any(ln.startswith("ENV S6_SERVICES_GRACETIME=") for ln in instructions)


# ── the gateway's own shutdown must fit inside that grace ───────────────────────
# The stop path (gateway/run_shutdown.py _stop_impl): drain, then on a timeout the
# post-interrupt grace, then adapters, tool kill, executor quiesce, SessionDB close.

DRAIN_KEYS = (("agent", "restart_drain_timeout"), ("agent", "cron_drain_timeout"))
# Teardown after the drain and the interrupt grace. The slowest in the pod's logs is
# 3.49s (Telegram disconnect, 2026-09-29 10:31).
TEARDOWN_MARGIN_S = 5


def _gateway_grace_s() -> float:
    return _env("S6_KILL_GRACETIME") / 1000


def _after_drain_s() -> float:
    # An s6 stop is an unexpected signal, so the stop path takes the signal grace
    # (gateway/run_config_loaders.py _post_interrupt_grace_timeout).
    from gateway.restart import DEFAULT_GATEWAY_SIGNAL_INTERRUPT_GRACE_TIMEOUT

    return DEFAULT_GATEWAY_SIGNAL_INTERRUPT_GRACE_TIMEOUT + TEARDOWN_MARGIN_S


def test_boot_pins_both_drain_budgets_inside_the_kill_grace():
    from hermes_cli.fork_ext.boot_reconcile import OVERRIDES

    for key in DRAIN_KEYS:
        assert key in OVERRIDES, f"{key} must be pinned on every boot, not only seeded"
        assert OVERRIDES[key] + _after_drain_s() <= _gateway_grace_s(), key


def test_the_effective_cron_drain_stays_inside_the_kill_grace():
    """The cron budget is resolved at stop time; it only ever extends the chat drain."""
    from gateway.restart import resolve_cron_drain_budget
    from gateway.shutdown_watchdog import resolve_shutdown_watchdog_delay
    from hermes_cli.fork_ext.boot_reconcile import OVERRIDES

    drain = OVERRIDES[("agent", "restart_drain_timeout")]
    budget = resolve_cron_drain_budget(
        drain, OVERRIDES[("agent", "cron_drain_timeout")],
        watchdog_delay=resolve_shutdown_watchdog_delay(drain))
    assert budget + _after_drain_s() <= _gateway_grace_s()


def test_the_seed_config_agrees_with_the_boot_pins():
    import yaml

    from hermes_cli.fork_ext.boot_reconcile import OVERRIDES

    seed = yaml.safe_load((DOCKERFILE.parent / "docker" / "config.yaml").read_text(encoding="utf-8"))
    for section, key in DRAIN_KEYS:
        assert seed[section][key] == OVERRIDES[(section, key)], key
    assert "signal_interrupt_grace_timeout" not in (seed.get("gateway") or {}), (
        "the seed overrides the signal grace; size the test against that value")
