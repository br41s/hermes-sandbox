"""CLI subcommand: `hermes memory-curator <subcommand>`.

Thin shell around agent/memory_curator.py. Lets the user trigger a digest,
inspect proposals, and apply/revert them without a Python one-liner (painful
over a flaky `service exec` channel).

Subcommands: run / status / show (read-only) and apply / revert (the slice-2
write path — human-gated and reversible). Import-time side effects are avoided
— main.py wires the argparse subparser on demand.
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
    print(f"  pending:        {st.get('pending_proposals', 0)} proposal(s)")
    print(f"  applied:        {st.get('applied_by_target') or '{}'}")
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


def _cmd_consolidate(args) -> int:
    from agent import memory_curator as mc
    if not bool(getattr(args, "force", False)) and not mc.is_enabled():
        print("Memory curator is disabled (memory_curator.enabled=false). "
              "Use --force to run a one-off consolidation anyway.")
        return 1
    print("Scanning memory for evictions (read-only)…")
    res = mc.run_consolidation(on_digest=lambda m: print(f"  {m}"))
    if res.get("digest_path"):
        print(f"\nDigest written to: {res['digest_path']}")
        print("Review:  hermes memory-curator show")
        print("Apply:   hermes memory-curator apply --all   (reversible via revert)")
    return 0


def _cmd_show(args) -> int:
    from agent.memory_curator import _digest_dir
    latest = _digest_dir() / "latest.md"
    if not latest.exists():
        print("No digest yet. Run one with: hermes memory-curator run")
        return 1
    print(latest.read_text(encoding="utf-8"))
    return 0


def _cmd_apply(args) -> int:
    from agent import memory_curator as mc
    apply_all = bool(getattr(args, "all", False))
    ids = list(getattr(args, "ids", []) or [])
    if not apply_all and not ids:
        print("Give proposal ids (e.g. `apply p1 p3`) or use --all. "
              "See ids with: hermes memory-curator show")
        return 1
    report = mc.apply_proposals(ids or None, apply_all=apply_all)
    applied = report.get("applied", [])
    skipped = report.get("skipped", [])
    errors = report.get("errors", [])
    if applied:
        print(f"✅ wrote to memory: {', '.join(applied)}")
    for s in skipped:
        print(f"– skipped {s}")
    for e in errors:
        print(f"❌ {e}")
    if not applied and not errors:
        print("Nothing applied.")
    if applied:
        print("Undo the last write with: hermes memory-curator revert")
    return 0 if applied or not errors else 1


def _cmd_revert(args) -> int:
    from agent import memory_curator as mc
    res = mc.revert_last()
    if res.get("reverted"):
        print(f"↩ reverted {res['reverted']} from `{res.get('target')}` memory")
        return 0
    print(f"Nothing reverted: {res.get('error', 'unknown')}")
    return 1


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

    p_cons = subs.add_parser(
        "consolidate", help="Propose evictions to keep the bounded store lean"
    )
    p_cons.add_argument(
        "--force", dest="force", action="store_true",
        help="Run even when the curator is disabled (memory_curator.enabled=false)",
    )
    p_cons.set_defaults(func=_cmd_consolidate)

    subs.add_parser("show", help="Print the latest digest") \
        .set_defaults(func=_cmd_show)

    p_apply = subs.add_parser(
        "apply", help="Write approved proposals to memory (reversible)"
    )
    p_apply.add_argument("ids", nargs="*", help="Proposal ids to apply (e.g. p1 p3)")
    p_apply.add_argument(
        "--all", dest="all", action="store_true", help="Apply every pending proposal",
    )
    p_apply.set_defaults(func=_cmd_apply)

    subs.add_parser("revert", help="Undo the most recent applied write") \
        .set_defaults(func=_cmd_revert)
