"""Fork: one cron job at a time per profile, profiles side by side, no fork lane.

Until stage 3 step 5 (``ops/multiplex-stage3-plan.md``) the fork ran every profile
and ``workdir`` job on ONE single-thread lane of its own (``cron-seq``), re-anchored
into upstream's ``tick``. A cron job that set ``profile`` or ``workdir`` used to mutate
process-global state for its whole run, so two of them overlapping leaked one
profile's identity into the other's ``gh``/``git`` subprocesses (the 2026-09-12
FinView PR #245 opened as ``hermes-auditor``). Neither mutates anything now: a
profile's ``.env`` is the run's secret scope and upstream scopes the workdir per run.

Phase A moved profile-store jobs onto upstream's per-profile pool, which boot sizes
to 1 on every named profile (``boot_reconcile.PROFILE_OVERRIDES``). Phase B removed
the lane, so ``workdir`` jobs run on their store's pool too. What these tests hold:

- a profile's jobs never overlap, and keep submit order (pool of 1);
- two profiles' jobs DO run at the same time — one slow profile holds no other;
- the default store's jobs, ``workdir`` ones included, run in parallel;
- both webhook entry points queue a profile's job behind its tick jobs, never on a
  worker thread of their own;
- the lane is gone: no ``cron-seq`` thread, no re-anchor in ``tick``.

Method: the real ``tick`` / ``dispatch_job_async`` → ``run_one_job`` path, with only
``run_job`` (and the store/delivery side effects) replaced. The fake ``run_job``
records every run's interval and thread; jobs that must be concurrent rendezvous on
a barrier, so a serialization would break it instead of passing by luck.
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


def _profile_home(tmp_path, name: str, max_parallel: int = 1):
    home = tmp_path / "profiles" / name
    (home / "cron").mkdir(parents=True)
    (home / "config.yaml").write_text(f"cron:\n  max_parallel_jobs: {max_parallel}\n", encoding="utf-8")
    return home


class _Recorder:
    """Instrumented ``run_job``: records every run's (thread, start, end). Jobs listed
    in ``together`` wait on one barrier, so they pass only if all of them are inside
    ``run_job`` at once; every other job holds for a short window, the window a job
    that could overlap it WOULD land in."""

    def __init__(self, together=()):
        self._cond = threading.Condition()
        self.runs: dict = {}  # job_id -> (thread_name, start, end)
        self.started: dict = {}  # job_id -> Event
        self._together = set(together)
        self._barrier = threading.Barrier(len(self._together), timeout=5) if len(self._together) > 1 else None
        self.barrier_broken = False

    def started_event(self, job_id: str) -> threading.Event:
        with self._cond:
            return self.started.setdefault(job_id, threading.Event())

    def run_job(self, job, **_kwargs):  # upstream adds keywords over time
        job_id = job["id"]
        thread = threading.current_thread().name
        start = time.monotonic()
        self.started_event(job_id).set()
        try:
            if self._barrier is not None and job_id in self._together:
                try:
                    self._barrier.wait()
                except threading.BrokenBarrierError:
                    self.barrier_broken = True
            else:
                time.sleep(HOLD_SECONDS)
        finally:
            with self._cond:
                self.runs[job_id] = (thread, start, time.monotonic())
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

    def assert_one_at_a_time_in_order(self, job_ids) -> None:
        """The jobs ran on one worker, never overlapping, in ``job_ids`` order."""
        runs = sorted((self.runs[j][1], self.runs[j][2], self.runs[j][0], j) for j in job_ids)
        threads = {t for _s, _e, t, _j in runs}
        assert len(threads) == 1, f"a pool of 1 ran on more than one worker: {threads}"
        for (_s1, end1, _t1, j1), (start2, _e2, _t2, j2) in zip(runs, runs[1:]):
            assert start2 >= end1, f"{j2} started while {j1} was still running"
        assert [j for _s, _e, _t, j in runs] == list(job_ids), "submit order was not kept"


@pytest.fixture
def pools(monkeypatch):
    """Isolate the scheduler's pools and stub everything around ``run_job`` that
    would touch the store or deliver."""
    import cron.scheduler as sched
    from cron.fork_ext import dispatch as fork_dispatch

    sched._shutdown_parallel_pool()
    monkeypatch.delenv("HERMES_CRON_MAX_PARALLEL", raising=False)

    due: list = []

    def _stub(name, value):
        # raising=False: upstream renames these over time (advance_next_run ->
        # advance_next_runs at v2026.8.31); stubbing a name that is gone is harmless.
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

    # tick's _process_job re-claims each job against the store when its pool actually
    # starts it — possibly after a later tick replaced ``due`` — so read ``store``.
    def _claim(job_id, return_job=False, **_kw):
        job = store.get(job_id)
        return dict(job) if (return_job and job is not None) else job is not None

    _stub("claim_job_for_fire", _claim)

    yield sched, fork_dispatch, due

    sched._shutdown_parallel_pool()


# ── in_profile_store ──────────────────────────────────────────────────────────


def test_in_profile_store_tells_a_profile_store_from_the_launch_one(tmp_path):
    from cron.fork_ext.dispatch import in_profile_store
    from hermes_constants import (
        get_routing_process_hermes_home, reset_hermes_home_override, set_hermes_home_override,
    )

    token = set_hermes_home_override(str(_profile_home(tmp_path, "grow-shop")))
    try:
        assert in_profile_store() is True
    finally:
        reset_hermes_home_override(token)

    token = set_hermes_home_override(str(get_routing_process_hermes_home()))
    try:
        assert in_profile_store() is False
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


# ── tick ──────────────────────────────────────────────────────────────────────


def test_the_default_stores_jobs_run_in_parallel_workdir_ones_included(pools, monkeypatch, tmp_path):
    """Phase B: a ``workdir`` job no longer leaves for a lane of its own. It runs on
    its store's pool beside plain jobs (each run gets its own isolated checkout)."""
    from cron.scheduler_provider import _profile_cron_scope
    from hermes_constants import get_routing_process_hermes_home

    sched, _fork, due = pools
    ids = [_uid("plain0"), _uid("plain1"), _uid("wd0"), _uid("wd1")]
    due[:] = [_job(ids[0]), _job(ids[1]), _job(ids[2], workdir=str(tmp_path)),
              _job(ids[3], workdir="/srv/biglobster")]
    rec = _Recorder(together=ids)
    monkeypatch.setattr(sched, "run_job", rec.run_job)

    with _profile_cron_scope(get_routing_process_hermes_home()):
        assert sched.tick(verbose=False, sync=True) == len(ids)

    rec.wait_for(ids)
    assert not rec.barrier_broken, "the default store's jobs were serialized"
    assert all(rec.runs[j][0].startswith("cron-parallel") for j in ids)


def test_a_profiles_jobs_run_one_at_a_time_in_submit_order(pools, monkeypatch, tmp_path):
    """Boot's ``cron.max_parallel_jobs: 1`` makes the profile's pool one worker, and
    a second tick's jobs queue behind the first tick's instead of starting beside them."""
    from cron.scheduler_provider import _profile_cron_scope

    sched, _fork, due = pools
    satellite = _profile_home(tmp_path, "grow-shop")
    first = [_uid("t1a"), _uid("t1b"), _uid("t1wd")]
    second = [_uid("t2a"), _uid("t2wd")]
    rec = _Recorder()
    monkeypatch.setattr(sched, "run_job", rec.run_job)

    with _profile_cron_scope(satellite):
        due[:] = [_job(first[0]), _job(first[1]), _job(first[2], workdir=str(tmp_path))]
        assert sched.tick(verbose=False, sync=False) == 3
        assert rec.started_event(first[0]).wait(5)
        due[:] = [_job(second[0]), _job(second[1], workdir="/srv/grow-shop")]
        assert sched.tick(verbose=False, sync=False) == 2

    rec.wait_for(first + second)
    rec.assert_one_at_a_time_in_order(first + second)
    assert all(rec.runs[j][0].startswith("cron-parallel") for j in first + second)


def test_profiles_run_side_by_side(pools, monkeypatch, tmp_path):
    """The prize of step 5: a slow job in one profile no longer holds another's (the
    2026-09-12 Gap Hunter sat 20+ minutes behind an auditor run). Each profile's first
    job meets the other's on a barrier, which only passes if both run at once."""
    from cron.scheduler_provider import _profile_cron_scope

    sched, _fork, due = pools
    homes = {"auditor": _profile_home(tmp_path, "auditor"), "grow-shop": _profile_home(tmp_path, "grow-shop")}
    ids = {name: _uid(name) for name in homes}
    rec = _Recorder(together=ids.values())
    monkeypatch.setattr(sched, "run_job", rec.run_job)

    for name, home in homes.items():
        with _profile_cron_scope(home):
            due[:] = [_job(ids[name])]
            assert sched.tick(verbose=False, sync=False) == 1

    rec.wait_for(list(ids.values()))
    # The barrier is the proof; thread names cannot tell the pools apart, since each
    # pool's first worker is ``cron-parallel_0``.
    assert not rec.barrier_broken, "one profile's job waited for another profile's"


# ── webhooks ──────────────────────────────────────────────────────────────────


def test_trigger_cron_job_id_queues_behind_the_profiles_tick_job(pools, monkeypatch, tmp_path):
    """``dispatch_job_async`` (the fork's ``trigger_cron_job_id`` webhook) used to run
    the job inline, beside tick's run of the same profile: that is how biglobster
    content PRs got authored as ``hermes-auditor``. It enqueues on the profile's pool."""
    from cron.scheduler_provider import _profile_cron_scope

    sched, _fork, due = pools
    satellite = _profile_home(tmp_path, "auditor")
    tick_id, hook_id, hook_wd = _uid("tick"), _uid("hook"), _uid("hookwd")
    rec = _Recorder()
    monkeypatch.setattr(sched, "run_job", rec.run_job)

    with _profile_cron_scope(satellite):
        due[:] = [_job(tick_id)]
        assert sched.tick(verbose=False, sync=False) == 1
        assert rec.started_event(tick_id).wait(5)
        assert sched.dispatch_job_async(_job(hook_id)) == {"queued": True, "reason": None}
        assert sched.dispatch_job_async(_job(hook_wd, workdir=str(tmp_path))) == {"queued": True, "reason": None}

    rec.wait_for([tick_id, hook_id, hook_wd])
    rec.assert_one_at_a_time_in_order([tick_id, hook_id, hook_wd])


def test_trigger_cron_job_id_does_not_rerun_a_running_job(pools, monkeypatch, tmp_path):
    from cron.scheduler_provider import _profile_cron_scope

    sched, _fork, due = pools
    satellite = _profile_home(tmp_path, "auditor")
    job_id = _uid("busy")
    rec = _Recorder()
    monkeypatch.setattr(sched, "run_job", rec.run_job)

    with _profile_cron_scope(satellite):
        due[:] = [_job(job_id)]
        assert sched.tick(verbose=False, sync=False) == 1
        assert rec.started_event(job_id).wait(5)
        assert sched.dispatch_job_async(_job(job_id)) == {"queued": False, "reason": "already running"}

    rec.wait_for([job_id])


def test_a_cron_job_event_queues_behind_the_profiles_tick_job(pools, monkeypatch, tmp_path):
    """Upstream's ``cron_job`` route would run the event on a worker thread of its own
    (``asyncio.to_thread``). In a profile's store that overlaps the profile's tick
    jobs, so ``run_event_job`` puts it on the profile's pool, behind them."""
    import asyncio

    from cron.scheduler_provider import _profile_cron_scope

    sched, fork, due = pools
    satellite = _profile_home(tmp_path, "grow-shop")
    tick_id = _uid("sattick")
    rec = _Recorder()
    monkeypatch.setattr(sched, "run_job", rec.run_job)
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
    assert fired["thread"] == rec.runs[tick_id][0], "the event ran outside the profile's pool"
    assert fired["start"] >= rec.runs[tick_id][2], "the event overlapped the profile's tick job"


def test_a_cron_job_event_on_the_profiles_pool_keeps_the_routed_scope(pools, tmp_path):
    """The webhook resolves and runs the job under the ROUTED profile's scope (a
    contextvar); the pool worker must see it, as upstream's ``to_thread`` would."""
    import asyncio
    import contextvars

    from cron.scheduler_provider import _profile_cron_scope

    _sched, fork, _due = pools
    satellite = _profile_home(tmp_path, "grow-shop")
    scope = contextvars.ContextVar("routed_profile", default="default")

    async def _fire():
        scope.set("grow-shop")
        with _profile_cron_scope(satellite):
            return await fork.run_event_job("j", lambda: (threading.current_thread().name, scope.get()))

    thread, seen = asyncio.run(_fire())
    assert thread.startswith("cron-parallel"), thread
    assert seen == "grow-shop"


def test_a_cron_job_event_in_the_launch_store_keeps_upstreams_worker_thread(pools):
    """The launch store's pool is unbounded, so there is nothing to queue behind:
    the event runs on ``asyncio.to_thread`` exactly as upstream does."""
    import asyncio

    _sched, fork, _due = pools
    thread = asyncio.run(fork.run_event_job("j", lambda: threading.current_thread().name))
    assert not thread.startswith(("cron-parallel", "cron-seq")), thread


# ── the lane is gone ──────────────────────────────────────────────────────────


def test_the_fork_lane_is_gone():
    """Phase B removed the lane and its re-anchor in ``tick``. A merge that brings any
    of it back would split jobs off onto a second pool again; this notices."""
    import inspect

    import cron.scheduler as sched
    import cron.scheduler_tick as scheduler_tick
    from cron.fork_ext import dispatch

    for name in ("is_sequential", "get_sequential_executor", "shutdown_sequential_executor",
                 "submit_sequential_jobs", "_sequential_executor"):
        assert not hasattr(dispatch, name), name
    for name in ("_fork_submit_sequential", "_fork_shutdown_sequential"):
        assert not hasattr(sched, name), name
    assert "_fork_submit_sequential" not in inspect.getsource(scheduler_tick)


def test_scheduler_call_sites_resolve_the_fork_modules():
    """The names tick, run_job and the webhook reach through cron.scheduler are the
    fork modules' objects, not stale copies."""
    import cron.scheduler as sched
    from cron.fork_ext import dispatch, run_guard

    assert sched.dispatch_job_async is dispatch.dispatch_job_async
    assert sched._job_run_lock is run_guard._job_run_lock
    assert sched.guarded_run_job is run_guard.guarded_run_job
    assert dispatch.logger is sched.logger
    assert run_guard.logger is sched.logger
