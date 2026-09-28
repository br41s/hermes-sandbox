"""The fork keeps ``gateway.multiplex_profiles: false`` as a real opt-out (fork-owned).

Upstream v2026.9.24 made one gateway per host serve every profile and RETIRED the
explicit ``false``: it is warned about, treated as unset, and rewritten to ``true`` in
config.yaml on the next boot. The only thing left that keeps a host standalone is an
incidental blocker (a duplicate bot token, a secondary still running its own gateway).

The fork is adopting multiplex in stages, and this opt-out is the rollback lever while
it does. Stage 2a (the boot hook in ``docker/cont-init.d/03-biglobster-config`` pins
the flag to ``true``) turns it on with inbound routing unchanged: group-topic
``auto_profile`` bindings still run in a per-turn subprocess until stage 2b adds
``gateway.profile_routes``. Rolling back is pinning ``false`` again, which this module
keeps meaning standalone. Once adoption is finished, drop this module, the two call
sites in ``gateway_multiplex_mode.py`` and ``test_gateway_multiplex_optout_fork.py``.
"""

from __future__ import annotations

from typing import Optional

REASON = "gateway.multiplex_profiles: false (fork keeps the opt-out; see hermes_cli/fork_ext/multiplex.py)"


def opted_out(flag: Optional[bool]) -> bool:
    """True when the operator explicitly chose standalone."""
    return flag is False
