"""CLI subcommand: `hermes memory-curator <subcommand>`.

Thin shell around agent/memory_curator.py. Lets the user trigger a read-only
digest, check status, and read the latest digest without dropping into a Python
one-liner (which is painful over a flaky `service exec` channel).

Read-only in slice 1: no subcommand writes to memory. Import-time side effects
are avoided — main.py wires the argparse subparser on demand.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _fmt_ts(ts: Optional[str]) -> str:
    if not ts:
        return "never"
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return str(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _cmd_status(args) -> int:
    from agent import memory_curator as mc
    st = mc.load_state()
    print("Memory curator (read-only lesson digest)")
    print(f"  enabled:        {mc.is_enabled()}")
    print(f"  paused:         {mc.is_paused()}")
    print(f"  interval:       {mc.get_interval_hours()}h")
    print(f"  lookback:       {mc.get_lookback_days()}d")
    print(f"  runs:           {st.get('run_count', 0)}")
    print(f"  last run:       {_fmt_ts(st.get('last_run_at'))}")
    print(f"  last summary:   {st.get('last_run_summary') or '—'}")
    print(f"  last digest:    {st.get('last_digest_path') or '—'}")
    return 0


def _cmd_run(args) -> int:
    from agent import memory_curator as mc
    # --force here means "run even when the curator is disabled", so you can
    # preview a digest without flipping memory_curator.enabled on. It does NOT
    # govern the interval: a manual `run` always runs now, bypassing the weekly
    # schedule — same contract as `hermes curator run`.
    allow_disabled = bool(getattr(args, "force", False))
    if not mc.is_enabled() and not allow_disabled:
        print("Memory curator is disabled (memory_curator.enabled=false). "
              "Use --force to run a one-off digest anyway.")
        return 1
    print("Running memory digest (read-only)…")
    res = mc.run_memory_digest(on_digest=lambda m: print(f"  {m}"), force=True)
    if not res:
        print("Nothing to do (no eligible sessions).")
        return 0
    path = res.get("digest_path")
    if path:
        print(f"\nDigest written to: {path}")
        print("View it with:      hermes memory-curator show")
    return 0


def _cmd_show(args) -> int:
    from agent.memory_curator import _digest_dir
    latest = _digest_dir() / "latest.md"
    if not latest.exists():
        print("No digest yet. Run one with: hermes memory-curator run")
        return 1
    print(latest.read_text(encoding="utf-8"))
    return 0


def register_cli(parent: argparse.ArgumentParser) -> None:
    """Attach memory-curator subcommands to an existing parser.

    Called from main.py after ``subparsers.add_parser("memory-curator", ...)``.
    """
    parent.set_defaults(func=lambda a: (parent.print_help(), 0)[1])
    subs = parent.add_subparsers(dest="memory_curator_command")

    subs.add_parser("status", help="Show memory-curator status") \
        .set_defaults(func=_cmd_status)

    p_run = subs.add_parser("run", help="Run a read-only digest now")
    p_run.add_argument(
        "--force", dest="force", action="store_true",
        help="Run even when the curator is disabled (memory_curator.enabled=false)",
    )
    p_run.set_defaults(func=_cmd_run)

    subs.add_parser("show", help="Print the latest digest") \
        .set_defaults(func=_cmd_show)
