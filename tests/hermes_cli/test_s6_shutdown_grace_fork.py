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
