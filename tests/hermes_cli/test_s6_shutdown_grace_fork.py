"""Fork: the image gives the gateway time to shut down cleanly.

s6-overlay's 3s default killed the gateway mid-shutdown on every deploy; the next boot saw an
unclean exit and ran an integrity check on the 2.2 GB state.db before connecting Telegram
(4m12s of silence on 2026-09-29). Both waits must stay inside Kubernetes' default 30s
termination grace, or the kubelet's SIGKILL lands first and nothing is gained.
"""

from __future__ import annotations

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"
K8S_DEFAULT_TERMINATION_GRACE_MS = 30_000


def _env(name: str) -> int:
    match = re.search(rf"^ENV {name}=(\d+)$", DOCKERFILE.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"Dockerfile does not set {name}"
    return int(match.group(1))


def test_the_gateway_gets_longer_than_the_s6_default_to_stop():
    assert _env("S6_SERVICES_GRACETIME") > 3_000


def test_both_waits_fit_inside_the_kubernetes_grace():
    total = _env("S6_SERVICES_GRACETIME") + _env("S6_KILL_GRACETIME")
    assert total < K8S_DEFAULT_TERMINATION_GRACE_MS


# ── the gateway's own shutdown drain must fit inside that grace ─────────────────
# 2026-09-29 13:30: a deploy made while an auditor run was stuck waited in the drain
# (restart_drain_timeout 180s from the seed config) and was SIGKILLed at +25s. The
# 12:38 deploy the same day was clean only because nothing was in flight.

DRAIN_KEYS = (("agent", "restart_drain_timeout"), ("agent", "cron_drain_timeout"))
# What the stop path still does after the drain: adapters, tool kill, executor, SessionDB.
TEARDOWN_MARGIN_S = 5


def test_boot_pins_both_drain_budgets_inside_the_s6_grace():
    from hermes_cli.fork_ext.boot_reconcile import OVERRIDES

    grace_s = _env("S6_SERVICES_GRACETIME") / 1000
    for key in DRAIN_KEYS:
        assert key in OVERRIDES, f"{key} must be pinned on every boot, not only seeded"
        assert OVERRIDES[key] + TEARDOWN_MARGIN_S <= grace_s, key


def test_the_effective_cron_drain_stays_inside_the_s6_grace():
    """The cron budget is resolved at stop time; it only ever extends the chat drain."""
    from gateway.restart import resolve_cron_drain_budget
    from gateway.shutdown_watchdog import resolve_shutdown_watchdog_delay
    from hermes_cli.fork_ext.boot_reconcile import OVERRIDES

    drain = OVERRIDES[("agent", "restart_drain_timeout")]
    budget = resolve_cron_drain_budget(
        drain, OVERRIDES[("agent", "cron_drain_timeout")],
        watchdog_delay=resolve_shutdown_watchdog_delay(drain))
    assert budget + TEARDOWN_MARGIN_S <= _env("S6_SERVICES_GRACETIME") / 1000


def test_the_seed_config_agrees_with_the_boot_pins():
    import yaml

    from hermes_cli.fork_ext.boot_reconcile import OVERRIDES

    seed = yaml.safe_load((DOCKERFILE.parent / "docker" / "config.yaml").read_text(encoding="utf-8"))
    for section, key in DRAIN_KEYS:
        assert seed[section][key] == OVERRIDES[(section, key)], key
