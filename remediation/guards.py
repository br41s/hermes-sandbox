"""Autonomy guards for the self-remediation loop (Phase 0).

These are the *brakes* — built before any engine exists. Phase 3's auto-act path
MUST pass ``may_auto_act()`` before it touches the world; if it returns False the
caller escalates instead of acting. The guards are pure decisions over the ledger
plus one env read, so they are trivially testable and have no side effects.

Three independent ways an auto-act is refused, checked outermost-first:
  1. kill switch  — ``HERMES_AUTONOMY=paused`` freezes ALL auto-acts in one move.
  2. debounce     — this exact signature already acted within the window
                    (the retry-storm race on the 60m loop).
  3. rate limit   — this class has hit its per-window ceiling.

The per-class ``gated``/``auto`` lifecycle is a SEPARATE, primary gate (a gated
class never reaches here). The kill switch is the emergency brake on top of it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from remediation import ledger

# Values (case/space-insensitive) that mean "freeze all auto-acts". Anything else
# — including unset — leaves the kill switch open; the per-class gated lifecycle
# remains the primary gate, so default-open here is the emergency brake, not the
# only line of defence.
_PAUSED_VALUES = {"paused", "pause", "off", "frozen", "stop"}

# Refusal reasons (stable strings — callers log/branch on these).
GATE_OK = "ok"
GATE_KILLSWITCH = "killswitch"
GATE_DEBOUNCE = "debounce"
GATE_RATELIMIT = "ratelimit"


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str  # GATE_* — GATE_OK iff allowed


def autonomy_paused(env: Optional[dict] = None) -> bool:
    """True if the global kill switch is engaged via ``HERMES_AUTONOMY``."""
    env = env if env is not None else os.environ
    return (env.get("HERMES_AUTONOMY") or "").strip().lower() in _PAUSED_VALUES


def may_auto_act(cls: str, signature: str, *,
                 entries: Optional[list[ledger.LedgerEntry]] = None,
                 path: Optional[Path] = None, now: Optional[datetime] = None,
                 env: Optional[dict] = None,
                 debounce_hours: int = ledger.DEBOUNCE_HOURS,
                 rate_window_hours: int = ledger.RATE_WINDOW_HOURS,
                 rate_max: int = ledger.RATE_MAX_PER_CLASS) -> GateDecision:
    """Single gate the auto-act path must pass. Read the ledger once and reuse it
    across both queries so the decision is consistent within a tick."""
    if autonomy_paused(env):
        return GateDecision(False, GATE_KILLSWITCH)

    # One read, shared by both ledger queries (consistent snapshot, one I/O).
    if entries is None:
        entries = ledger.read(path=path)

    if ledger.recently_acted(signature, entries=entries, now=now,
                             window_hours=debounce_hours):
        return GateDecision(False, GATE_DEBOUNCE)

    if ledger.act_count(cls, entries=entries, now=now,
                        window_hours=rate_window_hours) >= rate_max:
        return GateDecision(False, GATE_RATELIMIT)

    return GateDecision(True, GATE_OK)
