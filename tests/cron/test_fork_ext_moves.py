"""Guards for the fork code moved out of cron/scheduler.py and cron/jobs.py.

The moves into ``cron/fork_ext/`` were meant to change nothing at runtime.
These tests pin the two ways a move silently can:

- a name the old module's call sites resolve must be the SAME object as the
  new module's, or the scheduler would run a stale copy (and a counter would
  split in two);
- helpers that used to read ``cron.jobs`` globals must still honour patches of
  those globals, which is what the lazy ``from cron import jobs`` buys.
"""

from __future__ import annotations

from datetime import datetime


def test_scheduler_call_sites_resolve_the_moved_objects():
    import cron.scheduler as sched
    from cron.fork_ext import diagnostics, isolated_checkout

    for name in (
        "IsolatedCheckoutError",
        "_provision_isolated_checkout",
        "_cleanup_isolated_checkout",
        "_sweep_stale_checkouts",
    ):
        assert getattr(sched, name) is getattr(isolated_checkout, name), name
    for name in (
        "abandoned_agent_threads",
        "_note_abandoned_agent_thread",
        "_dump_stuck_agent_stack",
    ):
        assert getattr(sched, name) is getattr(diagnostics, name), name


def test_abandoned_counter_is_shared_across_both_names():
    """``hermes_cli/cron.py`` reads the count through ``cron.scheduler``."""
    import cron.scheduler as sched
    from cron.fork_ext import diagnostics

    before = sched.abandoned_agent_threads()
    diagnostics._note_abandoned_agent_thread()
    assert sched.abandoned_agent_threads() == before + 1


def test_moved_code_logs_under_the_scheduler_logger():
    """agent.log lines keep their ``cron.scheduler`` name after the move."""
    import cron.scheduler as sched
    from cron.fork_ext import diagnostics, isolated_checkout

    assert diagnostics.logger is sched.logger
    assert isolated_checkout.logger is sched.logger


def test_stack_dump_helper_never_raises(monkeypatch):
    import hermes_cli.stackdump as stackdump
    from cron.fork_ext import diagnostics

    def boom(*a, **k):
        raise RuntimeError("no faulthandler")

    monkeypatch.setattr(stackdump, "dump_now", boom)
    diagnostics._dump_stuck_agent_stack("job-x", 601.0)  # must not raise


def test_jobs_backup_still_runs_on_save_and_honours_patched_clock(monkeypatch, tmp_path):
    import cron.jobs as jobs

    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr(
        "cron.jobs._hermes_now", lambda: datetime(2031, 1, 2, 3, 4, 5)
    )

    jobs.create_job(prompt="p", schedule="every 2h", name="first")
    # The first save has nothing to back up; the second backs up the first.
    jobs.create_job(prompt="p", schedule="every 2h", name="second")

    cron_dir = tmp_path / "cron"
    assert (cron_dir / "jobs.bak").is_file()
    assert (cron_dir / "backups" / "jobs-20310102.json").is_file()


def test_mark_job_interrupted_moved_out_of_cron_jobs(monkeypatch, tmp_path):
    import cron.jobs as jobs
    from cron.fork_ext.interrupted import mark_job_interrupted

    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")

    job = jobs.create_job(prompt="p", schedule="every 2h", name="n")
    assert mark_job_interrupted(job["id"], reason="killed", at="2031-01-01T00:00:00+00:00")
    stored = next(j for j in jobs.load_jobs() if j["id"] == job["id"])
    assert stored["last_status"] == "interrupted"
    assert stored["last_run_at"] is None
    assert mark_job_interrupted("no-such-job", reason="x", at="y") is False
