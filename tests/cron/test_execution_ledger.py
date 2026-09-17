"""Durable cron execution-ledger behavior."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def _point_ledger(monkeypatch, tmp_path):
    import cron.executions as executions

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return executions


def test_execution_transitions_are_durable(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)

    claimed = executions.create_execution("job-1", source="builtin")
    assert claimed["status"] == "claimed"
    assert claimed["claimed_at"]
    assert claimed["started_at"] is None
    assert claimed["finished_at"] is None

    running = executions.mark_execution_running(claimed["id"])
    assert running["status"] == "running"
    assert running["started_at"]

    completed = executions.finish_execution(claimed["id"], success=True)
    assert completed["status"] == "completed"
    assert completed["finished_at"]
    assert completed["error"] is None

    persisted = executions.list_executions(job_id="job-1")
    assert persisted == [completed]


def test_terminal_execution_cannot_be_rewritten(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("immutable", source="builtin")
    executions.mark_execution_running(record["id"])
    executions.finish_execution(record["id"], success=True)

    assert executions.finish_execution(
        record["id"], success=False, error="late writer"
    ) is None
    assert executions.latest_execution("immutable")["status"] == "completed"


def test_retention_bounds_terminal_history_but_preserves_inflight(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 3)
    inflight = executions.create_execution("live", source="builtin")
    executions.mark_execution_running(inflight["id"])
    for index in range(8):
        row = executions.create_execution(f"done-{index}", source="builtin")
        executions.finish_execution(row["id"], success=True)

    records = executions.list_executions(limit=100)
    assert len([row for row in records if row["status"] == "completed"]) == 3
    assert executions.latest_execution("live")["status"] == "running"


def test_corrupt_store_fails_closed_without_overwrite(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    executions.EXECUTIONS_FILE.parent.mkdir(parents=True)
    executions.EXECUTIONS_FILE.write_bytes(b"not a sqlite database")

    with __import__("pytest").raises(sqlite3.DatabaseError):
        executions.create_execution("new", source="builtin")
    assert executions.EXECUTIONS_FILE.read_bytes() == b"not a sqlite database"


def test_execution_history_is_paginated(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    ids = []
    for _index in range(5):
        row = executions.create_execution("paged", source="builtin")
        executions.finish_execution(row["id"], success=True)
        ids.append(row["id"])

    first = executions.list_executions(job_id="paged", limit=2)
    second = executions.list_executions(
        job_id="paged", limit=2, before_claimed_at=first[-1]["claimed_at"]
    )
    assert [row["id"] for row in first] == list(reversed(ids))[:2]
    assert set(row["id"] for row in first).isdisjoint(row["id"] for row in second)


def test_cron_runs_cli_prints_execution_history(monkeypatch, tmp_path, capsys):
    executions = _point_ledger(monkeypatch, tmp_path)
    row = executions.create_execution("cli-job", source="builtin")
    executions.finish_execution(row["id"], success=False, error="boom")
    from hermes_cli.cron import cron_runs

    cron_runs("cli-job", limit=10)

    output = capsys.readouterr().out
    assert row["id"] in output
    assert "failed" in output
    assert "boom" in output


def test_quick_backup_includes_execution_ledger():
    from hermes_cli.backup import _QUICK_STATE_FILES

    assert "cron/executions.db" in _QUICK_STATE_FILES


def test_failed_execution_keeps_error(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)

    record = executions.create_execution("job-2", source="external")
    failed = executions.finish_execution(record["id"], success=False, error="provider exploded")

    assert failed["status"] == "failed"
    assert failed["error"] == "provider exploded"


def test_recovery_does_not_mark_live_process_execution_unknown(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("still-live", source="builtin")
    executions.mark_execution_running(record["id"])

    assert executions.recover_interrupted_executions() == []
    assert executions.latest_execution("still-live")["status"] == "running"


def test_recovery_does_not_mark_other_live_owner_unknown(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("other-live", source="builtin")
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, pid=? WHERE id=?",
            ("another-import", os.getpid(), record["id"]),
        )

    assert executions.recover_interrupted_executions() == []
    assert executions.latest_execution("other-live")["status"] == "claimed"


def test_recovery_rejects_recycled_pid(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("recycled", source="builtin")
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, process_started_at=? WHERE id=?",
            ("old-import", -1, record["id"]),
        )

    recovered = executions.recover_interrupted_executions()
    assert [r["job_id"] for r in recovered] == ["recycled"]
    assert recovered[0]["status"] == "unknown"
    assert executions.latest_execution("recycled")["status"] == "unknown"


def test_restart_marks_interrupted_execution_unknown_without_requeue(tmp_path):
    """Real temp-HERMES_HOME subprocess restart: in-flight is audit-only unknown."""
    home = tmp_path / "home"
    repo = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(repo)

    create = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cron.executions import create_execution, mark_execution_running; "
            "r=create_execution('restart-job', source='builtin'); "
            "mark_execution_running(r['id']); print(r['id'])",
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    execution_id = create.stdout.strip()

    recover = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from cron.executions import recover_interrupted_executions, list_executions; "
            "print(len(recover_interrupted_executions())); "
            "print(json.dumps(list_executions(job_id='restart-job'))) ",
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    lines = recover.stdout.strip().splitlines()
    assert lines[0] == "1"
    records = json.loads(lines[1])
    assert len(records) == 1
    assert records[0]["id"] == execution_id
    assert records[0]["status"] == "unknown"
    assert records[0]["finished_at"]
    assert "restart" in records[0]["error"].lower()
    # Recovery only classifies the old attempt. It must not manufacture a new
    # claimed record (which would imply an automatic retry).
    assert [r["status"] for r in records] == ["unknown"]


def test_generic_submit_failure_finishes_attempt_and_releases_guard(monkeypatch):
    import cron.scheduler as scheduler

    class BrokenPool:
        def submit(self, _callable):
            raise ValueError("executor rejected")

    finished = []
    monkeypatch.setattr(
        scheduler, "create_execution",
        lambda *_args, **_kwargs: {"id": "exec-submit-fail"},
    )
    monkeypatch.setattr(
        scheduler, "finish_execution",
        lambda execution_id, **kwargs: finished.append((execution_id, kwargs)),
    )
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda: [{"id": "submit-fail"}])
    monkeypatch.setattr(scheduler, "advance_next_run", lambda _job_id: None)
    monkeypatch.setattr(scheduler, "_get_parallel_pool", lambda _workers: BrokenPool())

    assert scheduler.tick(verbose=False, sync=False) == 0
    assert finished == [
        ("exec-submit-fail", {
            "success": False,
            "error": "Executor dispatch failed: executor rejected",
        })
    ]
    assert "submit-fail" not in scheduler.get_running_job_ids()


def test_run_one_job_records_running_then_terminal(monkeypatch):
    import cron.scheduler as scheduler

    events = []
    monkeypatch.setattr(
        scheduler,
        "mark_execution_running",
        lambda execution_id: events.append(("running", execution_id)),
        raising=False,
    )
    monkeypatch.setattr(
        scheduler,
        "finish_execution",
        lambda execution_id, **kwargs: events.append(("finish", execution_id, kwargs)),
        raising=False,
    )
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda job, *, defer_agent_teardown=None: (True, "output", "response", None),
    )
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_args: None)
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *_args, **_kwargs: None)

    assert scheduler.run_one_job({"id": "job-3", "execution_id": "exec-3"}) is True
    assert events[0] == ("running", "exec-3")
    assert events[-1][0:2] == ("finish", "exec-3")
    assert events[-1][2]["success"] is True


def test_provider_start_recovers_interrupted_records_before_tick(monkeypatch):
    import cron.scheduler_provider as provider

    events = []
    stop = __import__("threading").Event()
    stop.set()
    monkeypatch.setattr(
        "cron.executions.recover_interrupted_executions",
        lambda: events.append("recover") or [],
        raising=False,
    )
    monkeypatch.setattr("cron.jobs.record_ticker_heartbeat", lambda **_kwargs: events.append("heartbeat"))

    provider.InProcessCronScheduler().start(stop, interval=1)

    assert events[:2] == ["recover", "heartbeat"]


def test_external_provider_start_recovers_interrupted_records(monkeypatch):
    from plugins.cron_providers.chronos import ChronosCronScheduler

    provider = ChronosCronScheduler()
    provider._client = type("Client", (), {"arm": lambda self, **kwargs: None})()
    events = []
    monkeypatch.setattr(
        "cron.executions.recover_interrupted_executions",
        lambda: events.append("recover") or [],
    )
    monkeypatch.setattr(provider, "reconcile", lambda: events.append("reconcile"))

    provider.start(__import__("threading").Event())

    assert events == ["recover", "reconcile"]


def test_job_listing_exposes_latest_execution(monkeypatch, tmp_path):
    import cron.jobs as jobs

    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    executions = _point_ledger(monkeypatch, tmp_path)

    job = jobs.create_job(prompt="audit me", schedule="every 1h", name="audit")
    record = executions.create_execution(job["id"], source="builtin")
    executions.mark_execution_running(record["id"])

    listed = jobs.list_jobs(include_disabled=True)
    assert listed[0]["latest_execution"]["id"] == record["id"]
    assert listed[0]["latest_execution"]["status"] == "running"


# --- Restart recovery reconciles the JOB record, not just the ledger --------
# Regression cover for the 2026-09-17 incident: cron job b2f774557766 started
# its 11:12 +07 slot, was killed by a routine Zeabur rollout, and afterwards
# reported last_status=ok / last_error=None from the PREVIOUS run. The ledger
# knew the attempt was `unknown`; jobs.json — what operators and the incident
# watcher read — did not.


def _job_store(monkeypatch, tmp_path):
    import cron.jobs as jobs

    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    return jobs


def _orphan_inflight(executions, job_id):
    """Claim + start an attempt, then make its owner provably dead."""
    record = executions.create_execution(job_id, source="builtin")
    executions.mark_execution_running(record["id"])
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, process_started_at=? WHERE id=?",
            ("dead-import", -1, record["id"]),
        )
    return record


def _reload(jobs, job_id):
    return next(j for j in jobs.list_jobs(include_disabled=True) if j["id"] == job_id)


def test_restart_recovery_marks_job_record_interrupted(monkeypatch, tmp_path):
    from cron.scheduler_provider import InProcessCronScheduler

    jobs = _job_store(monkeypatch, tmp_path)
    executions = _point_ledger(monkeypatch, tmp_path)

    job = jobs.create_job(prompt="write sheets", schedule="every 2h", name="sheets")
    jobs.mark_job_run(job["id"], True)  # the 09:25 run succeeded
    _orphan_inflight(executions, job["id"])  # the 11:12 run was killed

    assert InProcessCronScheduler().recover_interrupted() == 1

    fresh = _reload(jobs, job["id"])
    assert fresh["last_status"] == "interrupted"
    assert "whether side effects ran is unknown" in fresh["last_error"]
    assert fresh["last_interrupted_at"]


def test_restart_recovery_does_not_advance_last_run_at(monkeypatch, tmp_path):
    """The silent-stall detector must keep firing after recovery.

    ``cron_stale_incidents`` derives a job's stall deadline from ``last_run_at``
    read as "last COMPLETED run". Recording the interruption there (e.g. via
    ``mark_job_run``) would reset that deadline and silence the one check that
    actually caught this incident — a strictly worse outcome than the bug.
    """
    from datetime import timedelta

    from cron.scheduler_provider import InProcessCronScheduler
    from incidents.sweep import _now, cron_stale_incidents

    jobs = _job_store(monkeypatch, tmp_path)
    executions = _point_ledger(monkeypatch, tmp_path)

    job = jobs.create_job(prompt="write sheets", schedule="every 2h", name="sheets")
    jobs.mark_job_run(job["id"], True)
    completed_at = _reload(jobs, job["id"])["last_run_at"]

    _orphan_inflight(executions, job["id"])
    InProcessCronScheduler().recover_interrupted()

    fresh = _reload(jobs, job["id"])
    assert fresh["last_run_at"] == completed_at, "recovery must not fake a completed run"

    # Far enough past the missed 2h slot that the stall check is due.
    later = _now() + timedelta(hours=5)
    assert [i.id for i in cron_stale_incidents([fresh], now=later)], (
        "recovery silenced the silent-stall detector"
    )


def test_restart_recovery_leaves_schedule_and_claims_untouched(monkeypatch, tmp_path):
    """Recovery is bookkeeping only — it must not re-arm or re-queue anything."""
    from cron.scheduler_provider import InProcessCronScheduler

    jobs = _job_store(monkeypatch, tmp_path)
    executions = _point_ledger(monkeypatch, tmp_path)

    job = jobs.create_job(prompt="write sheets", schedule="every 2h", name="sheets")
    jobs.mark_job_run(job["id"], True)
    before = _reload(jobs, job["id"])

    _orphan_inflight(executions, job["id"])
    InProcessCronScheduler().recover_interrupted()
    after = _reload(jobs, job["id"])

    for field in ("next_run_at", "state", "enabled", "repeat", "run_claim", "fire_claim"):
        assert after.get(field) == before.get(field), f"recovery mutated {field}"


def test_restart_recovery_does_not_overwrite_a_newer_completed_run(monkeypatch, tmp_path):
    """A stale ledger row from an older boot must not bury a good result."""
    from cron.scheduler_provider import InProcessCronScheduler

    jobs = _job_store(monkeypatch, tmp_path)
    executions = _point_ledger(monkeypatch, tmp_path)

    job = jobs.create_job(prompt="write sheets", schedule="every 2h", name="sheets")
    _orphan_inflight(executions, job["id"])
    jobs.mark_job_run(job["id"], True)  # a later run completed successfully

    assert InProcessCronScheduler().recover_interrupted() == 1  # ledger still reconciled
    fresh = _reload(jobs, job["id"])
    assert fresh["last_status"] == "ok"
    assert fresh.get("last_error") is None


def test_interrupted_job_raises_a_dated_incident(monkeypatch, tmp_path):
    """The watcher must date the incident from the interruption, not the old run."""
    from cron.scheduler_provider import InProcessCronScheduler
    from incidents.sweep import cron_failure_incidents

    jobs = _job_store(monkeypatch, tmp_path)
    executions = _point_ledger(monkeypatch, tmp_path)

    job = jobs.create_job(prompt="write sheets", schedule="every 2h", name="sheets")
    jobs.mark_job_run(job["id"], True)
    _orphan_inflight(executions, job["id"])
    InProcessCronScheduler().recover_interrupted()

    fresh = _reload(jobs, job["id"])
    found = cron_failure_incidents([fresh])
    assert len(found) == 1
    assert "interrupted by restart" in found[0].title
    assert fresh["last_interrupted_at"] in found[0].id
    assert "NOT retried" in found[0].detail


def test_supersession_compares_instants_not_strings(monkeypatch, tmp_path):
    """Offsets differ across a server timezone change; compare instants.

    ``2026-09-17T12:30+00:00`` is LATER than ``2026-09-17T18:00+07:00`` (11:00Z)
    but sorts earlier as a string. A lexicographic compare would treat the newer
    completed run as older and bury it under a stale interruption.
    """
    jobs = _job_store(monkeypatch, tmp_path)

    job = jobs.create_job(prompt="write sheets", schedule="every 2h", name="sheets")
    stored = jobs.load_jobs()
    stored[0]["last_run_at"] = "2026-09-17T12:30:00+00:00"
    jobs.save_jobs(stored)

    assert jobs.mark_job_interrupted(
        job["id"],
        reason="killed",
        at="2026-09-17T18:05:00+07:00",
        not_before="2026-09-17T18:00:00+07:00",  # 11:00Z — before the completed run
    ) is False
    assert _reload(jobs, job["id"]).get("last_status") != "interrupted"
