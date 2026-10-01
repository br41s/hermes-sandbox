"""``hermes cron move``: move a job between the default store and a profile's own
store without changing what it is (fork-owned, stage 3 step 0f).

``ops/multiplex-stage3-plan.md`` section 2.2 is the spec. ``remove`` + ``create`` is
wrong three ways: ``create_job`` mints a new id (breaking ``cron runs``, Langfuse
session ids, ``context_from`` and every doc that names the job), it recomputes
``next_run_at`` from now (shifting an interval job's phase), and ``remove_job``
deletes the job's output and notepad. This never calls ``remove_job``.

The invariant every step holds: *at most one runnable record per id across all
stores*. The same id in two stores is two independent jobs (the in-flight guard,
the fire fence and the fork's run lock are all per home), so:

1. **Refuse** unless the job is not in flight, its ``next_run_at`` is at least
   ``MIN_LEAD_SECONDS`` ahead, the id is absent from the target, every
   ``context_from`` edge moves in the same call, and it is not a webhook trigger
   (unless ``--webhook-route-disabled``: a pause does not stop a
   ``trigger_cron_job_id`` run).
2. **Pause the source** under its store's jobs lock, recording the pre-move state
   in ``fork_move``. From here the source cannot be ticked.
3. **Write the target** under the target store's jobs lock: a deep copy with the
   same id, ``next_run_at``, ``last_*``, ``repeat`` and the pre-move pause state.
   It drops ``profile`` (moving to a profile) or regains it (moving back).
4. **Copy per-home state**, never move it, since the source copy is the rollback:
   execution rows (``completed_occurrence`` proves a slot is done from the
   current home's ``executions.db``; without them catch-up can re-fire it), the
   output directory, and notepad rows when the target has none for the job.
5. **Delete the source record** with ``save_jobs(removed_ids=...)``.

A crash between 2 and 5 leaves the source paused with its ``fork_move`` marker;
re-running the same command finishes the move.

A dry run unless ``apply`` is set. Prints names and ids only: never a prompt.
"""

from __future__ import annotations

import contextlib
import copy
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence

MIN_LEAD_SECONDS = 600
MOVE_KEY = "fork_move"
_PAUSE_FIELDS = ("enabled", "state", "paused_at", "paused_reason")
_DROP_ON_TARGET = ("profile", "fire_claim", "run_claim", "pending_slot", MOVE_KEY)
_WEBHOOK_KEYS = ("trigger_cron_job_id", "cron_job")


class MoveRefused(RuntimeError):
    """The move would break the one-runnable-record invariant or lose state."""


@dataclass
class _Side:
    name: str   # "default" or a profile name
    home: Path


@dataclass
class _Plan:
    source: _Side
    target: _Side
    job_ids: List[str]
    jobs: Dict[str, dict] = field(default_factory=dict)  # id -> source record
    resuming: set = field(default_factory=set)            # ids paused by an earlier run
    done: set = field(default_factory=set)                # ids already only in the target
    refusals: List[str] = field(default_factory=list)


# ── store access ─────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _in_home(home: Path) -> Iterator[None]:
    """Scope cron storage AND the per-home databases (executions, notepad) to ``home``."""
    from cron.jobs import use_cron_store
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(home))
    try:
        with use_cron_store(home):
            yield
    finally:
        reset_hermes_home_override(token)


def _load(home: Path) -> List[dict]:
    from cron.jobs import load_jobs

    if not (home / "cron" / "jobs.json").is_file():
        return []  # never create a store just to read it
    with _in_home(home):
        return [j for j in load_jobs() if isinstance(j, dict)]


def _rewrite(home: Path, fn: Callable[[List[dict]], Optional[Sequence[str]]]) -> None:
    """``fn(jobs)`` mutates the list in place under ``home``'s jobs lock and returns
    the ids it deleted, if any; the result is saved in the same critical section."""
    from cron.jobs import _jobs_lock, _save_jobs_unlocked, load_jobs

    with _in_home(home), _jobs_lock():
        jobs = load_jobs()
        removed = fn(jobs)
        _save_jobs_unlocked(jobs, removed_ids=set(removed) if removed else None)


_DB_FILES = {"executions": "executions.db", "cron_notepad": "notepad.db"}


def _rows(module, table: str, job_id: str, home: Path) -> List[dict]:
    if not (home / "cron" / _DB_FILES[table]).is_file():
        return []  # reading must not create the database
    with _in_home(home), module._transaction() as conn:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE job_id=?", (job_id,))]


def _insert(module, table: str, rows: List[dict], home: Path) -> int:
    if not rows:
        return 0
    cols = list(rows[0])
    sql = (f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) "
           f"VALUES ({', '.join('?' for _ in cols)})")
    with _in_home(home), module._transaction() as conn:
        before = conn.total_changes
        conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
        return conn.total_changes - before


# ── checks ───────────────────────────────────────────────────────────────────


def _webhook_refs(homes: Sequence[Path]) -> List[tuple]:
    """``(home, key, value)`` for every webhook route field that fires a cron job, read
    from each home's ``config.yaml`` and ``webhook_subscriptions.json``. Raises if a
    file exists and cannot be parsed: an unreadable route list must refuse the move."""
    import yaml

    found = []

    def walk(node, home):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _WEBHOOK_KEYS and value:
                    found.append((home, key, str(value)))
                walk(value, home)
        elif isinstance(node, list):
            for item in node:
                walk(item, home)

    for home in dict.fromkeys(homes):
        for name, parse in (("config.yaml", yaml.safe_load), ("webhook_subscriptions.json", json.loads)):
            path = home / name
            if path.is_file():
                walk(parse(path.read_text(encoding="utf-8")), home)
    return found


def _in_flight(job: dict, home: Path, now: datetime) -> Optional[str]:
    from cron import executions
    from cron.constants import FIRE_CLAIM_TTL_SECONDS
    from cron.jobs import _claim_is_live, _oneshot_run_claim_ttl_seconds

    if _claim_is_live(job.get("fire_claim"), now, FIRE_CLAIM_TTL_SECONDS):
        return "it holds a live fire_claim"
    if _claim_is_live(job.get("run_claim"), now, _oneshot_run_claim_ttl_seconds()):
        return "it holds a live run_claim"
    live = [r for r in _rows(executions, "executions", job["id"], home)
            if r.get("status") in ("claimed", "running")]
    if live:
        return f"{len(live)} claimed/running execution row(s) in {home / 'cron' / 'executions.db'}"
    return None


def _refs(job: dict) -> List[str]:
    raw = job.get("context_from") or []
    return [raw] if isinstance(raw, str) else [str(r) for r in raw]


def _check(plan: _Plan, now: datetime, webhook_route_disabled: bool) -> None:
    from cron.jobs import _parse_aware

    source_jobs = _load(plan.source.home)
    target_ids = {j.get("id") for j in _load(plan.target.home)}
    by_id = {j.get("id"): j for j in source_jobs}
    moving = set(plan.job_ids)

    for jid in plan.job_ids:
        job = by_id.get(jid)
        if job is None:
            if jid in target_ids:
                plan.done.add(jid)
            else:
                plan.refusals.append(f"{jid}: not in the {plan.source.name} store")
            continue
        plan.jobs[jid] = job
        marker = job.get(MOVE_KEY)
        if isinstance(marker, dict):
            if marker.get("to") != plan.target.name:
                plan.refusals.append(f"{jid}: a move to {marker.get('to')!r} is half done; "
                                     f"finish that one first")
                continue
            plan.resuming.add(jid)  # finishing a move: target may exist, margin already spent
        elif jid in target_ids:
            plan.refusals.append(f"{jid}: the {plan.target.name} store already has this id")
        expected = (job.get("profile") or "").strip() or None
        if plan.target.name != "default" and expected != plan.target.name and jid not in plan.resuming:
            plan.refusals.append(f"{jid}: its profile is {expected or 'none'}, not {plan.target.name}")
        reason = _in_flight(job, plan.source.home, now)
        if reason:
            plan.refusals.append(f"{jid}: in flight ({reason})")
        if jid not in plan.resuming:
            nxt = _parse_aware(job.get("next_run_at")) if job.get("next_run_at") else None
            if nxt is not None and nxt - now < timedelta(seconds=MIN_LEAD_SECONDS):
                plan.refusals.append(
                    f"{jid}: next run at {job.get('next_run_at')} is under "
                    f"{MIN_LEAD_SECONDS // 60} minutes away; wait for it to run")
        for ref in _refs(job):
            if ref not in moving and ref not in target_ids:
                plan.refusals.append(f"{jid}: reads context_from {ref}, which is not moving with it")

    for other in source_jobs:
        oid = other.get("id")
        if oid in moving:
            continue
        for ref in _refs(other):
            if ref in moving:
                plan.refusals.append(f"{ref}: job {oid} reads its context_from; move them together")

    names = {jid: {jid, str(plan.jobs[jid].get("name") or "").lower()} - {""} for jid in plan.jobs}
    try:
        refs = _webhook_refs([_default_home(), plan.source.home, plan.target.home])
    except Exception as exc:
        plan.refusals.append(f"cannot read the webhook routes ({type(exc).__name__}); refusing blind")
        refs = []
    for home, key, value in refs:
        for jid, keys in names.items():
            if value in keys or value.lower() in keys:
                if webhook_route_disabled:
                    continue
                plan.refusals.append(
                    f"{jid}: webhook route {key} in {home} fires it, and a pause does not stop "
                    f"that; disable the route, then pass --webhook-route-disabled")


# ── the move ─────────────────────────────────────────────────────────────────


def _target_record(job: dict, plan: _Plan) -> dict:
    record = copy.deepcopy(job)
    marker = record.get(MOVE_KEY)
    if isinstance(marker, dict):
        for name, value in (marker.get("pre") or {}).items():
            record[name] = value
    for name in _DROP_ON_TARGET:
        record.pop(name, None)
    if plan.target.name == "default":
        record["profile"] = plan.source.name  # back to the fork shape it came from
    return record


def _move_one(jid: str, plan: _Plan, now: datetime, out: Callable[[str], None]) -> None:
    from cron import executions, notepad
    from cron.jobs import get_cron_output_dir

    # 2. Pause the source, keeping what it looked like before.
    def pause(jobs):
        for job in jobs:
            if job.get("id") == jid and not isinstance(job.get(MOVE_KEY), dict):
                job[MOVE_KEY] = {"to": plan.target.name,
                                 "pre": {k: job.get(k) for k in _PAUSE_FIELDS}}
                job.update(enabled=False, state="paused", paused_at=now.isoformat(),
                           paused_reason=f"moving to {plan.target.name}")
    _rewrite(plan.source.home, pause)
    paused = next((j for j in _load(plan.source.home) if j.get("id") == jid), None)
    if paused is None:
        raise MoveRefused(f"{jid}: left the {plan.source.name} store while it was being "
                          f"paused; re-run the command to see where it is")

    # 3. Write the target record, unless an earlier run already did.
    record = _target_record(paused, plan)

    def write(jobs):
        if not any(j.get("id") == jid for j in jobs):
            jobs.append(record)
    _rewrite(plan.target.home, write)

    # 4. Copy per-home state. INSERT OR IGNORE and copytree(dirs_exist_ok) make a re-run safe.
    copied = _insert(executions, "executions",
                     _rows(executions, "executions", jid, plan.source.home), plan.target.home)
    notes = 0
    if not _rows(notepad, "cron_notepad", jid, plan.target.home):
        notes = _insert(notepad, "cron_notepad",
                        _rows(notepad, "cron_notepad", jid, plan.source.home), plan.target.home)
    with _in_home(plan.source.home):
        src_out = get_cron_output_dir() / jid
    with _in_home(plan.target.home):
        dst_out = get_cron_output_dir() / jid
    if src_out.is_dir():
        shutil.copytree(src_out, dst_out, dirs_exist_ok=True)

    # 5. Delete the source record. Never remove_job: it deletes output and notepad.
    def delete(jobs):
        jobs[:] = [j for j in jobs if j.get("id") != jid]
        return [jid]
    _rewrite(plan.source.home, delete)
    out(f"  moved {jid}: {copied} execution row(s), {notes} notepad row(s), "
        f"output {'copied' if src_out.is_dir() else 'none'}")


def _default_home() -> Path:
    from hermes_cli.profiles import get_profile_dir

    return Path(get_profile_dir("default")).resolve()


def _side(name: str) -> _Side:
    from hermes_cli.profiles import get_profile_dir, normalize_profile_name

    canon = normalize_profile_name(name)
    if canon == "default":
        return _Side("default", _default_home())
    home = Path(get_profile_dir(canon)).resolve()
    if not home.is_dir():
        raise MoveRefused(f"profile {canon!r} does not exist at {home}")
    return _Side(canon, home)


def move(job_ids: Sequence[str], *, to_profile: Optional[str] = None,
         from_profile: Optional[str] = None, to_default: bool = False, apply: bool = False,
         webhook_route_disabled: bool = False, now: Optional[datetime] = None,
         out: Callable[[str], None] = print) -> int:
    """Plan (and with ``apply``, perform) a move. Returns 0 on success, 1 if refused."""
    from cron.jobs import _hermes_now

    now = now or _hermes_now()
    try:
        if to_default == bool(to_profile):
            raise MoveRefused("pass exactly one of --to-profile <p> or --to-default")
        if to_default:
            if not from_profile:
                raise MoveRefused("--to-default needs --from-profile <p>: the store it leaves")
            source, target = _side(from_profile), _side("default")
        else:
            if from_profile:
                raise MoveRefused("--from-profile only goes with --to-default")
            source, target = _side("default"), _side(to_profile)
        if source.name == target.name:
            raise MoveRefused("source and target are the same store")
    except MoveRefused as exc:
        out(f"refused: {exc}")
        return 1

    plan = _Plan(source, target, list(dict.fromkeys(job_ids)))
    _check(plan, now, webhook_route_disabled)
    out(f"move {', '.join(plan.job_ids)}: {source.name} ({source.home}) -> "
        f"{target.name} ({target.home})")
    for jid in sorted(plan.done):
        out(f"  {jid}: already in the {target.name} store, nothing to do")
    for jid in sorted(plan.resuming):
        out(f"  {jid}: an earlier move stopped part-way; this run finishes it")
    if plan.refusals:
        for reason in plan.refusals:
            out(f"  refused: {reason}")
        return 1
    if not apply:
        for jid in plan.jobs:
            out(f"  would move {jid} ({plan.jobs[jid].get('name') or '-'})")
        out("dry run: nothing changed. Re-run with --apply to move.")
        return 0
    for jid in plan.jobs:
        try:
            _move_one(jid, plan, now, out)
        except MoveRefused as exc:
            out(f"  refused: {exc}")
            return 1
    flag = "" if target.name == "default" else f"-p {target.name} "
    out(f"done. Per-job commands now need the target store: hermes {flag}cron list")
    return 0
