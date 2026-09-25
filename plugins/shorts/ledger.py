"""The shorts ledger — one JSON file per profile, owned by the tool, not the agent.

It answers three questions every run asks, deterministically:

* Which posts already have a short? (so the producer never repeats one)
* Which palette, motif, music and footage were used recently? (so the next
  short looks different from its twin and from last week's)
* What state is each short in, and where did it get published?

The checklist kept this as LEDGER.md, edited by the agent. An agent editing
its own state file in prose is how a short gets published twice, so here the
agent only ever reads the ledger through the tool, and every write is a
tool-side state transition.

States::

    rendering ─┬─> ready ──> published
               ├─> qa_failed
               └─> render_failed
    (any) ─────────> skipped
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix: in-process only, like cron/jobs.py
    fcntl = None

LEDGER_VERSION = 1
STATES = ("rendering", "ready", "qa_failed", "render_failed", "published", "skipped")
# A post counts as done while its short is alive or out; a failure is retried.
DONE_STATES = ("rendering", "ready", "published", "skipped")
TARGETS = ("youtube", "facebook", "instagram_reel", "instagram_story")


def shorts_root() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "workspace" / "shorts"


def ledger_path() -> Path:
    return shorts_root() / "ledger.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _empty() -> Dict[str, Any]:
    return {"version": LEDGER_VERSION, "entries": {}}


@contextlib.contextmanager
def locked(write: bool = True) -> Iterator[Dict[str, Any]]:
    """Load the ledger under an exclusive lock; save it on exit if ``write``."""
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    with open(lock, "a+", encoding="utf-8") as lock_handle:
        if fcntl is not None:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else _empty()
        except (OSError, ValueError):
            # A corrupt ledger must not silently become "nothing was ever
            # made" — that would re-publish every post. Keep it aside, fail.
            raise RuntimeError(f"{path} is unreadable; inspect it before continuing")
        data.setdefault("entries", {})
        yield data
        if write:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, path)


def read() -> Dict[str, Any]:
    with locked(write=False) as data:
        return json.loads(json.dumps(data))


def entries(data: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    data = data if data is not None else read()
    return sorted(data["entries"].values(), key=lambda e: e.get("created_at", ""))


def done_urls(data: Optional[Dict[str, Any]] = None) -> set:
    return {e["article_url"] for e in entries(data) if e.get("state") in DONE_STATES}


def recent_style(data: Optional[Dict[str, Any]] = None, n: int = 2) -> Dict[str, List[str]]:
    """Palettes and motifs of the last ``n`` shorts — the next one must differ."""
    recent = [e for e in entries(data) if e.get("state") != "render_failed"][-n:]
    return {
        "palettes": [e.get("palette", "") for e in recent],
        "motifs": [e.get("motif", "") for e in recent],
    }


def used_assets(data: Optional[Dict[str, Any]] = None, n: int = 40) -> Dict[str, List[str]]:
    """Footage and music used by the last ``n`` shorts, to pass as avoid-lists."""
    recent = entries(data)[-n:]
    broll: List[str] = []
    music: List[str] = []
    for e in recent:
        broll += [str(x) for x in e.get("broll_ids") or []]
        if e.get("music_id"):
            music.append(str(e["music_id"]))
    return {"broll": broll, "music": music[-12:]}


def add(entry: Dict[str, Any]) -> Dict[str, Any]:
    with locked() as data:
        rid = entry["request_id"]
        if rid in data["entries"]:
            raise ValueError(f"request {rid} already exists in the ledger")
        entry = {**entry, "state": "rendering", "created_at": _now(), "updated_at": _now(),
                 "published": {}, "notes": []}
        data["entries"][rid] = entry
        return dict(entry)


def update(request_id: str, **fields: Any) -> Dict[str, Any]:
    with locked() as data:
        entry = data["entries"].get(request_id)
        if entry is None:
            raise KeyError(f"no short {request_id!r} in the ledger")
        state = fields.get("state")
        if state is not None and state not in STATES:
            raise ValueError(f"unknown state {state!r}")
        note = fields.pop("note", None)
        entry.update(fields)
        if note:
            entry.setdefault("notes", []).append(f"{_now()} {note}")
        entry["updated_at"] = _now()
        return dict(entry)


def record_publish(request_id: str, target: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Store a target's result; the short becomes ``published`` once all enabled targets are."""
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}")
    with locked() as data:
        entry = data["entries"][request_id]
        entry.setdefault("published", {})[target] = {**result, "at": _now()}
        entry["updated_at"] = _now()
        return dict(entry)


def get(request_id: str) -> Dict[str, Any]:
    entry = read()["entries"].get(request_id)
    if entry is None:
        raise KeyError(f"no short {request_id!r} in the ledger")
    return entry
