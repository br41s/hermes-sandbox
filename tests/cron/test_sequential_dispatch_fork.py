"""MERGE GATE: no two workdir cron jobs ever run at the same time.

This was the fork's identity invariant, and the gate for merging upstream
v2026.8.31+. A cron job that set ``profile`` or ``workdir`` used to mutate
process-global state for its whole run, so two of them overlapping leaked one
profile's identity into the other's ``gh``/``git`` subprocesses (the 2026-09-12
FinView PR #245 opened as ``hermes-auditor``). Neither mutates anything now.
Since stage 3 step 5 the lane holds ``workdir`` jobs only. A job from a profile's
own store runs on that profile's upstream pool, serial because boot pins
``cron.max_parallel_jobs: 1`` (``test_a_profile_store_runs_serially_on_its_own_pool_not_behind_the_lane``).

Upstream v2026.8.31 (91cf5448d8) deleted its sequential ``cron-seq`` pool and
dispatches every due job in parallel (``parallel_jobs = due_jobs``), because
upstream scopes workdir per run without env mutation. The fork's profile code
still mutates the env, so the fork owns its own single-thread lane in
``cron/fork_ext/dispatch.py`` and ``tick`` calls it once:

    parallel_jobs = _fork_submit_sequential(parallel_jobs, _submit_with_guard, _all_futures, _results, sync)

**Whoever merges upstream: this file must pass against the merged
``cron/scheduler.py`` before the merge is committed.** If that one line is lost
in conflict resolution, every profile job silently goes parallel, and these
tests are what notice. Dry-run 2026-09-26 against v2026.8.31 merged into this
branch: the tick re-anchor was that one line, the file passed 20/20, and it
failed (3 tests) with the line removed. Also re-anchor, outside tick: the
``_fork_shutdown_sequential()`` line in ``_shutdown_parallel_pool`` and the
``def run_job(`` -> ``def _run_job_impl(`` rename of upstream's body.

Method: the real ``tick`` / ``dispatch_job_async`` → ``run_one_job`` path, with
only ``run_job`` (and the store/delivery side effects) replaced. The fake
``run_job`` records enter/exit under a lock and holds each profile/workdir job
open for a short window, so any second job that could run concurrently WOULD
be inside that window. Plain jobs rendezvous on a barrier, which proves the
partition does not over-reach and serialize them too. Negative control: make
``is_sequential`` return False and the overlap assertions fail.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest

HOLD_SECONDS = 0.05


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _job(job_id: str, **fields) -> dict:
    return {
        "id": job_id,
        "name": job_id,
        "prompt": "test",
        "schedule": "every 5m",
        "enabled": True,
        "next_run_at": "2020-01-01T00:00:00",
        "deliver": "local",
        **fields,
    }


class _Recorder:
    """Instrumented ``run_job``: records every run's interval and thread, and
    flags any moment where two env-mutating jobs are inside ``run_job`` at
    once. Classification is by the job's own fields, NOT by
    ``is_sequential`` — so breaking the predicate cannot also blind the test."""

    def __init__(self, plain_parties: int = 0):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._active_seq: set = set()
        self.overlaps: list = []
        self.runs: dict = {}  # job_id -> (kind, thread_name, start, end)
        self.started = {}  # job_id -> Event
        self.plain_barrier = (
            threading.Barrier(plain_parties, timeout=5) if plain_parties > 1 else None
        )
        self.plain_barrier_broken = False

    @staticmethod
    def mutates_env(job: dict) -> bool:
        return bool(str(job.get("workdir") or "").strip())

    def started_event(self, job_id: str) -> threading.Event:
        with self._lock:
            return self.started.setdefault(job_id, threading.Event())

    def run_job(self, job, **_kwargs):  # upstream adds keywords over time
        job_id = job["id"]
        seq = self.mutates_env(job)
        thread = threading.current_thread().name
        with self._lock:
            start = time.monotonic()
            if seq:
                if self._active_seq:
                    self.overlaps.append((job_id, sorted(self._active_seq)))
                self._active_seq.add(job_id)
            ev = self.started.setdefault(job_id, threading.Event())
        ev.set()
        try:
            if seq:
                time.sleep(HOLD_SECONDS)  # the window a concurrent job would land in
            elif self.plain_barrier is not None:
                try:
                    self.plain_barrier.wait()
                except threading.BrokenBarrierError:
                    self.plain_barrier_broken = True
        finally:
            with self._cond:
                if seq:
                    self._active_seq.discard(job_id)
                self.runs[job_id] = ("seq" if seq else "plain", thread, start, time.monotonic())
                self._cond.notify_all()
        return True, "out", "resp", None

    def wait_for(self, job_ids, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        with self._cond:
            while not all(j in self.runs for j in job_ids):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    missing = [j for j in job_ids if j not in self.runs]
                    raise AssertionError(f"jobs never finished: {missing}")
                self._cond.wait(remaining)

    def seq_threads(self) -> set:
        return {thread for kind, thread, _s, _e in self.runs.values() if kind == "seq"}

    def pairwise_seq_overlaps(self) -> list:
        """Belt and braces on top of the live check: any two recorded
        env-mutating intervals that intersect."""
        seq = sorted(
            ((s, e, j) for j, (k, _t, s, e) in self.runs.items() if k == "seq"),
        )
        return [
            (a[2], b[2]) for a, b in zip(seq, seq[1:]) if b[0] < a[1]
        ]


@pytest.fixture
def lane(monkeypatch):
    """Isolate the scheduler's module state and stub everything around
    ``run_job`` that would touch the store or deliver."""
    import cron.scheduler as sched
    from cron.fork_ext import dispatch as fork_dispatch

    fork_dispatch.shutdown_sequential_executor()
    sched._shutdown_parallel_pool()
    monkeypatch.delenv("HERMES_CRON_MAX_PARALLEL", raising=False)

    due: list = []

    def _stub(name, value):
        # raising=False: this gate must run against the MERGED scheduler, and
        # upstream renames these (advance_next_run -> advance_next_runs at
        # v2026.8.31). Stubbing a name the module no longer has is harmless.
        monkeypatch.setattr(sched, name, value, raising=False)

    store: dict = {}  # every job ever served as due, as the real store would hold it

    def _get_due_jobs():
        store.update((j["id"], j) for j in due)
        return list(due)

    _stub("get_due_jobs", _get_due_jobs)
    _stub("advance_next_run", lambda *_a, **_kw: None)
    _stub("advance_next_runs", lambda *_a, **_kw: None)
    _stub("save_job_output", lambda *_a, **_kw: None)
    _stub("mark_job_run", lambda *_a, **_kw: None)
    _stub("_deliver_result", lambda *_a, **_kw: None)
    _stub("_send_kickoff_ping", lambda *_a, **_kw: None)
    _stub("claim_dispatch", lambda *_a, **_kw: True)

    # v2026.8.31+: tick's _process_job re-claims each job against the store
    # when the lane actually starts it — possibly after a later tick has
    # replaced ``due`` — so the claim reads ``store``, not ``due``.
    def _claim(job_id, return_job=False, **_kw):
        job = store.get(job_id)
        return dict(job) if (return_job and job is not None) else job is not None

    _stub("claim_job_for_fire", _claim)

    def _upstream_pool_is_not_the_lane(*_a, **_kw):
        raise AssertionError(
            "upstream's _get_sequential_pool() was used: the fork's lane lives in "
            "cron/fork_ext/dispatch.py (upstream deletes theirs at v2026.8.31)"
        )

    # Only while it still exists (upstream deletes it at v2026.8.31).
    if hasattr(sched, "_get_sequential_pool"):
        monkeypatch.setattr(sched, "_get_sequential_pool", _upstream_pool_is_not_the_lane)

    yield sched, fork_dispatch, due

    fork_dispatch.shutdown_sequential_executor()
    sched._shutdown_parallel_pool()


def _assert_serialized(rec: _Recorder, seq_ids) -> None:
    assert rec.overlaps == [], (
        f"profile/workdir jobs overlapped (job, already running): {rec.overlaps}"
    )
    assert rec.pairwise_seq_overlaps() == []
    threads = rec.seq_threads()
    assert len(threads) == 1, f"env-mutating jobs ran on more than one thread: {threads}"
    (thread,) = threads
    assert thread.startswith("cron-seq"), thread
    assert all(rec.runs[j][0] == "seq" for j in seq_ids)


def test_is_sequential_partition():
    from cron.fork_ext.dispatch import is_sequential

    assert is_sequential({"workdir": "/srv/proj"}) is True
    assert is_sequential({}) is False
    assert is_sequential({"workdir": None}) is False
    assert is_sequential({"workdir": "  "}) is False
    # Stage 3 step 2: the retired field no longer counts — a record carrying it
    # is refused in run_job before it could run anywhere.
    assert is_sequential({"profile": "auditor"}) is False


def test_a_job_from_a_profiles_own_store_leaves_the_lane(tmp_path):
    """Stage 3 step 5: the lane keys on ``workdir`` only. A job from a profile's own
    store goes to upstream's per-profile pool (sized 1 by boot), even though
    ``in_profile_store`` still recognises the store for the identity path."""
    from cron.fork_ext.dispatch import in_profile_store, is_sequential
    from hermes_constants import (
        get_routing_process_hermes_home, reset_hermes_home_override, set_hermes_home_override,
    )

    satellite = tmp_path / "profiles" / "grow-shop"
    satellite.mkdir(parents=True)
    token = set_hermes_home_override(str(satellite))
    try:
        assert in_profile_store() is True
        assert is_sequential({}) is False
        assert is_sequential({"workdir": "/srv/x"}) is True
    finally:
        reset_hermes_home_override(token)

    token = set_hermes_home_override(str(get_routing_process_hermes_home()))
    try:
        assert in_profile_store() is False
        assert is_sequential({}) is False
    finally:
        reset_hermes_home_override(token)


def test_in_profile_store_fails_closed(monkeypatch, caplog):
    import hermes_constants

    from cron.fork_ext.dispatch import in_profile_store

    def _boom():
        raise OSError("home unreadable")

    monkeypatch.setattr(hermes_constants, "get_hermes_home", _boom)
    with caplog.at_level("WARNING", logger="cron.scheduler"):
        assert in_profile_store() is True
    assert "could not resolve the active or launch home (OSError)" in caplog.text


def test_tick_never_overlaps_profile_or_workdir_jobs(lane, monkeypatch, tmp_path):
    sched, _fork, due = lane
    seq_ids = [_uid(f"prof{i}") for i in range(4)] + [_uid(f"wd{i}") for i in range(2)] + [_uid("both")]
    plain_ids = [_uid(f"plain{i}") for i in range(3)]
    for jid in seq_ids[:4]:
        due.append(_job(jid, workdir="/srv/auditor"))
    for jid in seq_ids[4:6]:
        due.append(_job(jid, workdir=str(tmp_path)))
    due.append(_job(seq_ids[6], workdir=str(tmp_path)))
    for jid in plain_ids:
        due.append(_job(jid))

    rec = _Recorder(plain_parties=len(plain_ids))
    monkeypatch.setattr(sched, "run_job", rec.run_job)

    n = sched.tick(verbose=False, sync=True)

    assert n == len(seq_ids) + len(plain_ids)
    rec.wait_for(seq_ids + plain_ids)
    _assert_serialized(rec, seq_ids)
    # Plain jobs are NOT serialized: all three were inside run_job at once.
    assert not rec.plain_barrier_broken, "plain jobs were serialized — partition over-reached"
    assert all(rec.runs[j][1].startswith("cron-parallel") for j in plain_ids)


def test_a_profile_store_runs_serially_on_its_own_pool_not_behind_the_lane(lane, monkeypatch, tmp_path):
    """Stage 3 step 5, the prize: a slow job on the lane (here, the default store's
    workdir jobs) no longer holds a client's jobs. A profile's jobs run on that
    profile's own pool, and with boot's ``cron.max_parallel_jobs: 1`` they still run
    one at a time, in submit order."""
    from cron.scheduler_provider import _profile_cron_scope

    sched, _fork, due = lane
    satellite = tmp_path / "profiles" / "grow-shop"
    (satellite / "cron").mkdir(parents=True)
    (satellite / "config.yaml").write_text("cron:\n  max_parallel_jobs: 1\n", encoding="utf-8")
    lane_ids = [_uid("lane0"), _uid("lane1"), _uid("lane2")]
    satellite_ids = [_uid("satplain0"), _uid("satplain1"), _uid("satplain2")]
    rec = _Recorder()
    monkeypatch.setattr(sched, "run_job", rec.run_job)

    due[:] = [_job(j, workdir=f"/srv/{j}") for j in lane_ids]
    assert sched.tick(verbose=False, sync=False) == 3
    assert rec.started_event(lane_ids[0]).wait(5)

    due[:] = [_job(j) for j in satellite_ids]
    with _profile_cron_scope(satellite):
        assert sched.tick(verbose=False, sync=False) == 3

    rec.wait_for(lane_ids + satellite_ids)
    _assert_serialized(rec, lane_ids)
    lane_thread = rec.runs[lane_ids[0]][1]
    sat = sorted((rec.runs[j][2], rec.runs[j][3], rec.runs[j][1], j) for j in satellite_ids)
    assert all(t.startswith("cron-parallel") and t != lane_thread for _s, _e, t, _j in sat), (
        "a profile-store job ran on the fork's lane")
    assert len({t for _s, _e, t, _j in sat}) == 1, "max_parallel_jobs: 1 was not one worker"
    for (_s1, end1, _t1, _j1), (start2, _e2, _t2, _j2) in zip(sat, sat[1:]):
        assert start2 >= end1, "a profile's jobs overlapped despite max_parallel_jobs: 1"
    assert [j for _s, _e, _t, j in sat] == satellite_ids, "submit order was not kept"
    assert sat[0][0] < max(rec.runs[j][3] for j in lane_ids), (
        "the profile's first job waited for the lane to drain")


def test_tick_of_the_default_store_keeps_plain_jobs_parallel(lane, monkeypatch):
    """The ticker scopes the launch profile's own store too; its plain jobs stay
    on upstream's parallel pool."""
    from cron.scheduler_provider import _profile_cron_scope
    from hermes_constants import get_routing_process_hermes_home

    sched, _fork, due = lane
    plain_ids = [_uid(f"defplain{i}") for i in range(3)]
    due[:] = [_job(j) for j in plain_ids]
    rec = _Recorder(plain_parties=len(plain_ids))
    monkeypatch.setattr(sched, "run_job", rec.run_job)

    with _profile_cron_scope(get_routing_process_hermes_home()):
        assert sched.tick(verbose=False, sync=True) == 3

    rec.wait_for(plain_ids)
    assert not rec.plain_barrier_broken, "the default store's plain jobs were serialized"
    assert all(rec.runs[j][1].startswith("cron-parallel") for j in plain_ids)


def test_lane_serializes_across_ticks(lane, monkeypatch, tmp_path):
    """A second tick firing while the first tick's profile jobs are still
    running queues behind them instead of starting a second lane."""
    sched, _fork, due = lane
    first = [_uid("t1p0"), _uid("t1p1"), _uid("t1wd")]
    second = [_uid("t2p0"), _uid("t2p1")]
    rec = _Recorder()
    monkeypatch.setattr(sched, "run_job", rec.run_job)

    due[:] = [
        _job(first[0], workdir="/srv/auditor"),
        _job(first[1], workdir="/srv/biglobster"),
        _job(first[2], workdir=str(tmp_path)),
    ]
    assert sched.tick(verbose=False, sync=False) == 3
    assert rec.started_event(first[0]).wait(5)

    due[:] = [_job(second[0], workdir="/srv/grow-shop"), _job(second[1], workdir="/srv/auditor")]
    assert sched.tick(verbose=False, sync=False) == 2

    rec.wait_for(first + second)
    _assert_serialized(rec, first + second)


def test_webhook_dispatch_shares_the_tick_lane(lane, monkeypatch, tmp_path):
    """``dispatch_job_async`` (the webhook ``trigger_cron_job_id`` path) must
    queue on the SAME single-thread lane as tick's profile jobs — the
    hermes-auditor identity leak came from exactly this pair overlapping."""
    sched, _fork, due = lane
    tick_ids = [_uid("tickp0"), _uid("tickp1"), _uid("tickwd")]
    hook_seq = [_uid("hookp"), _uid("hookwd")]
    hook_plain = _uid("hookplain")
    rec = _Recorder()
    monkeypatch.setattr(sched, "run_job", rec.run_job)

    due[:] = [
        _job(tick_ids[0], workdir="/srv/auditor"),
        _job(tick_ids[1], workdir="/srv/biglobster"),
        _job(tick_ids[2], workdir=str(tmp_path)),
    ]
    assert sched.tick(verbose=False, sync=False) == 3
    assert rec.started_event(tick_ids[0]).wait(5)

    # Fire the webhook triggers from other threads while tick's first profile
    # job is mid-run, the way the gateway event loop would.
    results: dict = {}

    def _fire(job):
        results[job["id"]] = sched.dispatch_job_async(job)

    firers = [
        threading.Thread(target=_fire, args=(_job(hook_seq[0], workdir="/srv/finview"),)),
        threading.Thread(target=_fire, args=(_job(hook_seq[1], workdir=str(tmp_path)),)),
        threading.Thread(target=_fire, args=(_job(hook_plain),)),
    ]
    for t in firers:
        t.start()
    for t in firers:
        t.join(5)

    assert all(r == {"queued": True, "reason": None} for r in results.values()), results
    rec.wait_for(tick_ids + hook_seq + [hook_plain])
    _assert_serialized(rec, tick_ids + hook_seq)
    assert rec.runs[hook_plain][1].startswith("cron-parallel")


def test_webhook_cron_job_event_shares_the_tick_lane(lane, monkeypatch, tmp_path):
    """Upstream's ``cron_job`` webhook route (``run_event_job``) must queue a
    profile/workdir job on the SAME lane as tick's, and leave a plain job on a
    worker thread of its own, as upstream runs every event."""
    import asyncio

    import cron.jobs

    sched, fork, due = lane
    tick_ids = [_uid("tickp0"), _uid("tickp1")]
    hook_seq = [_uid("evtp"), _uid("evtwd")]
    hook_plain = _uid("evtplain")
    events = {
        hook_seq[0]: _job(hook_seq[0], workdir="/srv/finview"),
        hook_seq[1]: _job(hook_seq[1], workdir=str(tmp_path)),
        hook_plain: _job(hook_plain),
    }
    monkeypatch.setattr(cron.jobs, "resolve_job_ref", events.get)
    rec = _Recorder()
    monkeypatch.setattr(sched, "run_job", rec.run_job)

    due[:] = [_job(tick_ids[0], workdir="/srv/auditor"), _job(tick_ids[1], workdir="/srv/biglobster")]
    assert sched.tick(verbose=False, sync=False) == 2
    assert rec.started_event(tick_ids[0]).wait(5)

    async def _fire_all():
        await asyncio.gather(*(fork.run_event_job(ref, rec.run_job, job) for ref, job in events.items()))

    asyncio.run(_fire_all())
    rec.wait_for(tick_ids + hook_seq + [hook_plain])
    _assert_serialized(rec, tick_ids + hook_seq)
    assert not rec.runs[hook_plain][1].startswith("cron-seq")


def test_webhook_cron_job_unresolvable_ref_fails_closed_onto_the_lane(lane, monkeypatch):
    """A ref that cannot be resolved up front (ambiguous, store unreadable)
    must not escape the lane: ``fire`` reports the error from there."""
    import asyncio

    import cron.jobs

    _sched, fork, _due = lane

    def _ambiguous(_ref):
        raise cron.jobs.AmbiguousJobReference("two jobs are named 'x'")

    monkeypatch.setattr(cron.jobs, "resolve_job_ref", _ambiguous)
    ran_on = asyncio.run(fork.run_event_job("x", lambda: threading.current_thread().name))
    assert ran_on.startswith("cron-seq"), ran_on


def test_webhook_cron_job_on_the_lane_keeps_the_routed_profile_scope(lane, monkeypatch):
    """The webhook resolves and runs the job under the ROUTED profile's scope
    (a contextvar); the lane worker must see it, as upstream's to_thread did."""
    import asyncio
    import contextvars

    import cron.jobs

    _sched, fork, _due = lane
    scope = contextvars.ContextVar("routed_profile", default="default")
    monkeypatch.setattr(cron.jobs, "resolve_job_ref", lambda ref: _job(ref, workdir="/srv/grow-shop"))

    async def _fire():
        scope.set("grow-shop")
        return await fork.run_event_job("j", lambda: (threading.current_thread().name, scope.get()))

    thread, seen = asyncio.run(_fire())
    assert thread.startswith("cron-seq"), thread
    assert seen == "grow-shop"


def test_a_webhook_fired_profile_job_queues_behind_the_profiles_tick_job(lane, monkeypatch, tmp_path):
    """Step 5: upstream's webhook ``cron_job`` route would run a plain job on a worker
    thread of its own. For a job in a profile's store that would overlap the profile's
    tick jobs, so ``run_event_job`` puts it on the profile's pool (size 1), behind them."""
    import asyncio

    import cron.jobs
    from cron.scheduler_provider import _profile_cron_scope

    sched, fork, due = lane
    satellite = tmp_path / "profiles" / "grow-shop"
    (satellite / "cron").mkdir(parents=True)
    (satellite / "config.yaml").write_text("cron:\n  max_parallel_jobs: 1\n", encoding="utf-8")
    tick_id = _uid("sattick")
    rec = _Recorder()
    monkeypatch.setattr(sched, "run_job", rec.run_job)
    monkeypatch.setattr(cron.jobs, "resolve_job_ref", lambda ref: _job(ref))
    fired = {}

    def _fire():
        fired["start"] = time.monotonic()
        fired["thread"] = threading.current_thread().name

    async def _event():
        with _profile_cron_scope(satellite):
            due[:] = [_job(tick_id)]
            assert sched.tick(verbose=False, sync=False) == 1
            assert rec.started_event(tick_id).wait(5)
            await fork.run_event_job("hook-job", _fire)

    asyncio.run(_event())
    rec.wait_for([tick_id])
    assert fired["thread"].startswith("cron-parallel"), fired["thread"]
    assert fired["start"] >= rec.runs[tick_id][3], "the webhook job overlapped the profile's tick job"


def test_shutdown_parallel_pool_drains_the_fork_lane():
    """``_shutdown_parallel_pool`` shut down the sequential pool before the
    lane moved here, and tests use it to drain between cases; a run left on
    the lane would otherwise execute under the NEXT test's monkeypatches."""
    import cron.scheduler as sched
    from cron.fork_ext import dispatch

    finished = threading.Event()
    executor = dispatch.get_sequential_executor()
    executor.submit(lambda: (time.sleep(0.2), finished.set()))

    sched._shutdown_parallel_pool()

    assert finished.is_set(), "shutdown returned while a lane job was still running"
    assert dispatch._sequential_executor is None
    assert dispatch.get_sequential_executor() is not executor
    dispatch.shutdown_sequential_executor()


def test_scheduler_call_sites_resolve_the_fork_lane():
    """The names tick, run_job and the webhook reach through cron.scheduler
    are the fork module's objects, not stale copies."""
    import cron.scheduler as sched
    from cron.fork_ext import dispatch, run_guard

    assert sched._fork_submit_sequential is dispatch.submit_sequential_jobs
    assert sched.dispatch_job_async is dispatch.dispatch_job_async
    assert sched._job_run_lock is run_guard._job_run_lock
    assert sched.guarded_run_job is run_guard.guarded_run_job
    assert dispatch.logger is sched.logger
    assert run_guard.logger is sched.logger
