"""Append-only autonomy ledger for the self-remediation loop (Phase 0).

The ledger is the single source of truth for "what acted, when, and with what
outcome" — it is what makes the loop *supervised* rather than a vibe. Every
detection, proposal, execution and verification appends one JSON line, mirroring
the ``incidents/blocked-commits.jsonl`` signal-file pattern already in prod.

It also answers the two questions the Phase-0 guards need before any auto-act:
  * debounce — "did we already act on this exact signature recently?"
    (kills the retry-storm race the 60m watcher loop would otherwise create)
  * rate    — "how many times has this class acted in the window?"
    (per-class ceiling; exceed -> escalate, don't act)

Hermetic by injection: every function takes an optional ``path`` / ``now`` so
tests touch no real disk or clock. A malformed or missing file degrades to [].
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone, UTC
from pathlib import Path
from typing import List, Optional

# Events recorded over a class's lifecycle. "applied" is the only one that
# counts as an *action* for debounce/rate purposes — proposals and verifications
# do not touch the world.
EVENT_PROPOSED = "proposed"   # gated class detected; CEO approval requested
EVENT_APPLIED = "applied"     # bounded fix executed (gated-after-approval, or auto)
EVENT_VERIFIED = "verified"   # next-tick check: signature cleared (success)
EVENT_FAILED = "failed"       # next-tick check: signature persists / fix errored
EVENT_REVERTED = "reverted"   # fix rolled back via its declared reversal
EVENT_RECOMMENDED = "recommended"  # promotion suggested to CEO (deduped via this event)
EVENT_PROMOTED = "promoted"   # class mode flipped gated -> auto (CEO approved)

OUTCOME_PENDING = "pending"
OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOME_REVERTED = "reverted"

_LEDGER_CAP = 5000           # keep the file bounded; generous so promotion counts survive
DEBOUNCE_HOURS = 26          # one watcher window + margin (matches CRON_FAILURE_WINDOW_HOURS)
RATE_WINDOW_HOURS = 24
RATE_MAX_PER_CLASS = 3       # max auto-acts per class per window before we escalate instead


@dataclass
class LedgerEntry:
    """One line in the autonomy ledger."""
    ts: str               # iso8601 UTC
    cls: str              # remediation class name, e.g. "cron-transient-failure"
    signature: str        # stable per-occurrence key (the debounce/dedup unit)
    target: str           # human ref: job id / repo
    mode: str             # "gated" | "auto" at action time
    event: str            # one of EVENT_*
    outcome: str = OUTCOME_PENDING  # one of OUTCOME_*
    detail: str = ""      # free text (truncated on write)

    def as_dict(self) -> dict:
        return asdict(self)


def _now() -> datetime:
    return datetime.now(UTC)


def _ledger_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "remediation" / "ledger.jsonl"


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


def make_entry(cls: str, signature: str, target: str, mode: str, event: str,
               *, outcome: str = OUTCOME_PENDING, detail: str = "",
               now: Optional[datetime] = None) -> LedgerEntry:
    """Build a timestamped entry. Detail is truncated to keep lines small."""
    return LedgerEntry(
        ts=(now or _now()).isoformat(),
        cls=cls, signature=signature, target=target, mode=mode, event=event,
        outcome=outcome, detail=str(detail)[:500],
    )


def append(entry: LedgerEntry, *, path: Optional[Path] = None,
           cap: int = _LEDGER_CAP) -> None:
    """Append one entry as a JSON line, creating the dir as needed, then prune."""
    path = path or _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry.as_dict(), ensure_ascii=False) + "\n")
    _prune(path, cap)


def read(*, path: Optional[Path] = None) -> list[LedgerEntry]:
    """Read all entries, skipping blank/malformed lines (best-effort)."""
    path = path or _ledger_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out: list[LedgerEntry] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
            out.append(LedgerEntry(
                ts=rec["ts"], cls=rec["cls"], signature=rec["signature"],
                target=rec.get("target", ""), mode=rec.get("mode", ""),
                event=rec["event"], outcome=rec.get("outcome", OUTCOME_PENDING),
                detail=rec.get("detail", ""),
            ))
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return out


def _prune(path: Path, cap: int = _LEDGER_CAP) -> None:
    """Keep the file bounded by retaining the most recent ``cap`` lines."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return
    if len(lines) <= cap:
        return
    try:
        path.write_text("\n".join(lines[-cap:]) + "\n", encoding="utf-8")
    except Exception:
        pass


# --- queries the guards depend on -------------------------------------------

def recently_acted(signature: str, *, entries: Optional[list[LedgerEntry]] = None,
                   path: Optional[Path] = None, now: Optional[datetime] = None,
                   window_hours: int = DEBOUNCE_HOURS) -> bool:
    """True if this exact signature already has an ``applied`` action within the
    debounce window. The retry-storm guard: a fix that didn't clear the signature
    must not be re-applied on the next tick — verification/escalation handles it.
    """
    now = now or _now()
    entries = entries if entries is not None else read(path=path)
    cutoff = now - timedelta(hours=window_hours)
    for e in entries:
        if e.event != EVENT_APPLIED or e.signature != signature:
            continue
        ts = _parse_iso(e.ts)
        if ts is not None and ts >= cutoff:
            return True
    return False


def act_count(cls: str, *, entries: Optional[list[LedgerEntry]] = None,
              path: Optional[Path] = None, now: Optional[datetime] = None,
              window_hours: int = RATE_WINDOW_HOURS) -> int:
    """Count ``applied`` actions for a class within the rate window."""
    now = now or _now()
    entries = entries if entries is not None else read(path=path)
    cutoff = now - timedelta(hours=window_hours)
    n = 0
    for e in entries:
        if e.event != EVENT_APPLIED or e.cls != cls:
            continue
        ts = _parse_iso(e.ts)
        if ts is not None and ts >= cutoff:
            n += 1
    return n
