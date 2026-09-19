"""Safety net #3 — the dashboard auth gate stays locked.

Why this exists: the cryptominer incident injected 25 fake MCP servers through
an *unauthenticated* public panel. The root-cause fix was the OAuth gate
(``hermes_cli.dashboard_auth.middleware``). This net locks that fix so it can't
silently regress — specifically, that the set of paths reachable WITHOUT auth
stays minimal and never grows to include a sensitive or mutation endpoint
(MCP/tools/config-write/exec), which is exactly the injection vector.

It reuses the real gate decision (``_path_is_public``) and the real allowlist
(``PUBLIC_API_PATHS``) rather than re-implementing them.
"""
from __future__ import annotations

from typing import Iterable, List

from hermes_cli.dashboard_auth.middleware import _path_is_public
from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS

# The exact set of /api paths that are ALLOWED to be public (unauthenticated).
# Each is read-only or carries its own auth (delegate). Anything in the live
# allowlist that is NOT here is a regression of the dashboard lockdown.
EXPECTED_PUBLIC: frozenset[str] = frozenset({
    "/api/status",            # liveness probe (portal wildcard health)
    "/api/config/defaults",   # read-only schema/defaults for the SPA
    "/api/config/schema",
    "/api/model/info",        # read-only model metadata
    "/api/dashboard/themes",  # read-only skin manifests
    "/api/dashboard/plugins",
    # External orchestrator endpoint (BigLobster COO). Mutates — it runs an
    # arbitrary agent prompt — so it is on this list only because it fails
    # closed: hermes_cli/web_server.py's _verify_delegate_secret() refuses
    # every request unless HERMES_CALLBACK_SECRET is set, and then requires
    # an x-hermes-secret header matching it (hmac.compare_digest). That
    # header, not this allowlist, is the boundary.
    "/api/delegate",
    # Chronos managed-cron fire webhook (upstream v2026.7.20). Bypasses the
    # cookie gate because it carries its own short-lived NAS-minted JWT
    # (purpose=cron_fire) which the handler verifies — the JWT, not this
    # allowlist, is the boundary. Inert on this deployment: no cron.chronos.*
    # config is set, so verify_nas_fire_token() fails closed on an empty
    # expected_audience and every request 401s.
    "/api/cron/fire",
    # BigLobster payment-confirmed rental provisioning (fork-specific; see
    # AGENT_RENTAL_SETUP.md). This one DOES mutate — it creates a Hermes
    # profile and cron jobs — so it is on this list only because it fails
    # closed: hermes_cli/bl_rental_webhook.py refuses every request unless
    # BL_RENTAL_WEBHOOK_SECRET is set, and then requires an HMAC-SHA256
    # signature over "<timestamp>.<raw body>" inside a 300 s replay window.
    # The signature, not this allowlist, is the boundary. If that handler ever
    # grows an unauthenticated path, remove this entry with it.
    "/api/bl/rental/provision",
})

# Substrings that must NEVER appear in any public path. These mark write /
# mutation / capability endpoints — the kind that let the miner inject config.
SENSITIVE_MARKERS: tuple[str, ...] = (
    "mcp", "tool", "secret", "token", "env", "exec", "shell",
    "write", "admin", "install", "upload",
)


def gate_decision(path: str) -> str:
    """'PUBLIC' if ``path`` bypasses the auth gate, else 'GATED'."""
    return "PUBLIC" if _path_is_public(path) else "GATED"


def audit_public_allowlist(paths: Iterable[str] = PUBLIC_API_PATHS) -> List[str]:
    """Return problems with the public allowlist (empty list == locked-down).

    Flags any path that is unexpected (not in EXPECTED_PUBLIC) or that contains
    a sensitive marker. Accepts an explicit ``paths`` set so the regression
    test can prove the tripwire fires on a bad allowlist.
    """
    problems: List[str] = []
    for path in paths:
        low = path.lower()
        if path not in EXPECTED_PUBLIC:
            problems.append(f"unexpected public path: {path}")
        hit = next((m for m in SENSITIVE_MARKERS if m in low), None)
        if hit:
            problems.append(f"sensitive marker '{hit}' in public path: {path}")
    return problems


def audit_summary(paths: Iterable[str] = PUBLIC_API_PATHS) -> str:
    """Summary used as eval-case output.

    Lists the allowlist it audited rather than only its verdict. The list is
    short, and printing it lets a reader (or a judge) confirm "minimal, nothing
    sensitive" from the output itself instead of trusting the word "OK" — an
    empty allowlist and a locked-down one rendered identically before.
    """
    paths = list(paths)
    problems = audit_public_allowlist(paths)
    if problems:
        return "FAIL: " + "; ".join(problems)
    if not paths:
        return ("OK: audited 0 public paths — the allowlist was empty, so this "
                "run demonstrates nothing about the live gate.")
    listed = ", ".join(sorted(paths))
    return (f"OK: audited {len(paths)} unauthenticated paths, 0 problems. Every "
            f"one is in the expected allowlist and none carries a sensitive "
            f"marker ({', '.join(SENSITIVE_MARKERS)}). Public paths: {listed}.")
