"""Fork's own tests for the cron execution ledger (job-record reconciliation), kept out of upstream's file so upstream merges do not conflict."""

from __future__ import annotations

import sqlite3


def _point_ledger(monkeypatch, tmp_path):
    import cron.executions as executions

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return executions


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
