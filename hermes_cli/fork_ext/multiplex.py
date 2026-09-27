"""The fork keeps ``gateway.multiplex_profiles: false`` as a real opt-out (fork-owned).

Upstream v2026.9.24 made one gateway per host serve every profile and RETIRED the
explicit ``false``: it is warned about, treated as unset, and rewritten to ``true`` in
config.yaml on the next boot. The only thing left that keeps a host standalone is an
incidental blocker (a duplicate bot token, a secondary still running its own gateway).

Production does not run that topology. One default gateway serves Telegram; profile
work reaches other profiles through subprocesses (group-topic ``auto_profile`` routing,
profile-scoped delegation, cron's sequential lane), and the web server is pinned to
``default`` (CLAUDE.md, "How Hermes manages the other projects"). Flipping that at
boot, inside a 14k-commit upstream merge, is not a change to make by accident, so an
explicit ``false`` still means standalone here. The boot hook
(``docker/cont-init.d/03-biglobster-config``) pins it. Adopting multiplex is a
separate, deliberate step: drop the pin and this module together.
"""

from __future__ import annotations

from typing import Optional

REASON = "gateway.multiplex_profiles: false (fork keeps the opt-out; see hermes_cli/fork_ext/multiplex.py)"


def opted_out(flag: Optional[bool]) -> bool:
    """True when the operator explicitly chose standalone."""
    return flag is False
