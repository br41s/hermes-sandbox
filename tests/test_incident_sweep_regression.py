"""Regression lock for the incident watcher sweep (incidents/sweep.py).

Hermetic: injects synthetic jobs / langfuse incidents / now / state_path, so no
cron data, network, or clock is touched. Guards the configured behaviour —
detect failures, dedup, silent-when-clean, and the 24h heartbeat.
"""
from datetime import timedelta

from incidents.sweep import (Incident, checkout_drift_incidents, cron_failure_incidents,
                             cron_stale_incidents, prompt_drift_incidents, sweep)
from incidents.sweep import _now as now_fn


def _failed_job(jid="j1", name="finview-cron", agent_err="boom", delivery_err=None, ago_hours=1):
    last_run = (now_fn() - timedelta(hours=ago_hours)).isoformat()
    return {"id": jid, "name": name, "last_error": agent_err,
            "last_delivery_error": delivery_err, "last_run_at": last_run}


def _ok_job(jid="ok1"):
    return {"id": jid, "name": "healthy", "last_error": None,
            "last_delivery_error": None, "last_run_at": now_fn().isoformat()}


def _recurring_job(jid="r1", name="seo-cron", *, minutes=10, last_run_ago_hours=None,
                   created_ago_hours=None, enabled=True, state="scheduled",
                   kind="interval", expr=None):
    """A recurring job with NO error recorded — the silent-stall shape."""
    schedule = {"kind": kind, "minutes": minutes}
    if kind == "cron":
        schedule = {"kind": "cron", "expr": expr or "*/10 * * * *"}
    last_run = None
    if last_run_ago_hours is not None:
        last_run = (now_fn() - timedelta(hours=last_run_ago_hours)).isoformat()
    created = (now_fn() - timedelta(hours=created_ago_hours if created_ago_hours
                                    is not None else 100)).isoformat()
    return {"id": jid, "name": name, "enabled": enabled, "state": state,
            "schedule": schedule, "created_at": created, "last_run_at": last_run,
            "last_error": None, "last_delivery_error": None}


class TestDetection:
    def test_failed_job_becomes_an_incident(self):
        incs = cron_failure_incidents([_failed_job()])
        assert len(incs) == 1
        assert "finview-cron" in incs[0].title
        assert incs[0].handoff == "cron job id j1"

    def test_healthy_job_is_ignored(self):
        assert cron_failure_incidents([_ok_job()]) == []

    def test_stale_failure_outside_window_is_ignored(self):
        assert cron_failure_incidents([_failed_job(ago_hours=72)]) == []

    def test_delivery_error_is_flagged(self):
        incs = cron_failure_incidents([_failed_job(agent_err=None, delivery_err="telegram 502")])
        assert len(incs) == 1 and "delivery error" in incs[0].title


class TestStaleDetection:
    """The silent-abort blind spot: no last_error, but runs stopped completing."""

    def test_stalled_interval_job_is_flagged(self):
        # 10-min job, last completed run 3h ago, no error recorded.
        incs = cron_stale_incidents([_recurring_job(last_run_ago_hours=3)])
        assert len(incs) == 1
        assert "silently stalled" in incs[0].title
        assert "no error was recorded" in incs[0].detail

    def test_job_within_cadence_plus_grace_is_healthy(self):
        # 10-min job that ran 30 min ago — overdue, but inside the 1h grace.
        assert cron_stale_incidents([_recurring_job(last_run_ago_hours=0.5)]) == []

    def test_paused_and_disabled_jobs_are_ignored(self):
        jobs = [_recurring_job(jid="p", last_run_ago_hours=48, state="paused"),
                _recurring_job(jid="d", last_run_ago_hours=48, enabled=False)]
        assert cron_stale_incidents(jobs) == []

    def test_oneshot_jobs_are_ignored(self):
        job = _recurring_job(last_run_ago_hours=48, kind="once")
        job["schedule"] = {"kind": "once", "run_at": now_fn().isoformat()}
        assert cron_stale_incidents([job]) == []

    def test_job_that_never_ran_is_flagged_from_created_at(self):
        incs = cron_stale_incidents([_recurring_job(last_run_ago_hours=None,
                                                    created_ago_hours=6)])
        assert len(incs) == 1
        assert "never" in incs[0].detail

    def test_stalled_cron_kind_schedule_is_flagged(self):
        incs = cron_stale_incidents([_recurring_job(last_run_ago_hours=5,
                                                    kind="cron", expr="*/10 * * * *")])
        assert len(incs) == 1

    def test_loudly_failing_job_is_not_double_flagged(self):
        # It ran (and recorded an error) recently — cron_failure_incidents
        # owns that signal; staleness must stay quiet.
        job = _recurring_job(last_run_ago_hours=0.1)
        job["last_error"] = "boom"
        assert cron_stale_incidents([job]) == []

    def test_stall_is_reported_once_then_deduped(self, tmp_path):
        sp = tmp_path / "s.json"
        job = _recurring_job(last_run_ago_hours=3)  # stable last_run_at
        first = sweep(jobs=[job], langfuse=[], state_path=sp)
        assert "silently stalled" in first
        assert sweep(jobs=[job], langfuse=[], state_path=sp) == ""

    def test_stall_does_not_classify_as_remediable(self):
        # A silent stall must stay a human-handled incident: blindly
        # re-triggering a job that is stuck on approval (or was killed for
        # cause) is not a bounded fix. Guards against transient-marker drift.
        from remediation.registry import classify
        (inc,) = cron_stale_incidents([_recurring_job(last_run_ago_hours=3)])
        assert classify(inc) is None


class TestPromptDrift:
    """Opt-in repo-.prompt vs live-jobs.json drift detection."""

    def _job(self, prompt, source="onsite-seo/seo.prompt", jid="pd1"):
        return {"id": jid, "name": "seo", "prompt": prompt, "prompt_source": source}

    def test_matching_prompt_is_silent(self, tmp_path):
        (tmp_path / "p.prompt").write_text("do seo\n")
        job = self._job("do seo", source="p.prompt")
        assert prompt_drift_incidents([job], repo_root=tmp_path) == []

    def test_drifted_prompt_is_flagged(self, tmp_path):
        (tmp_path / "p.prompt").write_text("do seo v2")
        job = self._job("do seo v1", source="p.prompt")
        incs = prompt_drift_incidents([job], repo_root=tmp_path)
        assert len(incs) == 1 and "drifted" in incs[0].title

    def test_missing_source_file_is_flagged(self, tmp_path):
        job = self._job("do seo", source="gone.prompt")
        incs = prompt_drift_incidents([job], repo_root=tmp_path)
        assert len(incs) == 1 and "missing" in incs[0].title

    def test_jobs_without_prompt_source_are_skipped(self, tmp_path):
        assert prompt_drift_incidents([{"id": "x", "prompt": "hi"}],
                                      repo_root=tmp_path) == []

    def test_drift_realerts_only_when_content_changes_again(self, tmp_path):
        (tmp_path / "p.prompt").write_text("v2")
        job = self._job("v1", source="p.prompt")
        (first,) = prompt_drift_incidents([job], repo_root=tmp_path)
        (again,) = prompt_drift_incidents([job], repo_root=tmp_path)
        assert first.id == again.id  # same drift -> same id -> deduped by sweep
        (tmp_path / "p.prompt").write_text("v3")
        (changed,) = prompt_drift_incidents([job], repo_root=tmp_path)
        assert changed.id != first.id  # content moved -> new alert


class TestSweepBehaviour:
    def test_clean_first_run_emits_heartbeat(self, tmp_path):
        # No incidents, no prior state -> first run proves it's alive.
        out = sweep(jobs=[_ok_job()], langfuse=[], state_path=tmp_path / "s.json")
        assert "still running" in out

    def test_clean_run_after_recent_heartbeat_is_silent(self, tmp_path):
        sp = tmp_path / "s.json"
        sweep(jobs=[_ok_job()], langfuse=[], state_path=sp)          # emits heartbeat, records ts
        out = sweep(jobs=[_ok_job()], langfuse=[], state_path=sp)    # within 24h -> silent
        assert out == ""

    def test_heartbeat_returns_after_24h_of_silence(self, tmp_path):
        sp = tmp_path / "s.json"
        sweep(jobs=[_ok_job()], langfuse=[], state_path=sp)
        later = now_fn() + timedelta(hours=25)
        out = sweep(jobs=[_ok_job()], langfuse=[], state_path=sp, now=later)
        assert "still running" in out

    def test_incident_is_reported_then_deduped(self, tmp_path):
        sp = tmp_path / "s.json"
        job = _failed_job()  # same record (stable last_run_at) across both sweeps
        first = sweep(jobs=[job], langfuse=[], state_path=sp)
        assert "Incident" in first and "finview-cron" in first
        second = sweep(jobs=[job], langfuse=[], state_path=sp)  # same failure, same run
        assert second == ""  # deduped, and not yet heartbeat-due

    def test_langfuse_incident_is_included(self, tmp_path):
        lf = [Incident(id="trace:abc", kind="langfuse", title="Langfuse error trace abc…",
                       detail="signal: boom", handoff="trace-id abc")]
        out = sweep(jobs=[_ok_job()], langfuse=lf, state_path=tmp_path / "s.json")
        assert "trace-id abc" in out

    def test_dry_run_does_not_persist_state(self, tmp_path):
        sp = tmp_path / "s.json"
        sweep(jobs=[_failed_job()], langfuse=[], state_path=sp, dry_run=True)
        assert not sp.exists()  # nothing written -> next real run still reports it
        out = sweep(jobs=[_failed_job()], langfuse=[], state_path=sp)
        assert "Incident" in out


class TestCheckoutDrift:
    """docker/cont-init.d/03-biglobster-config section 6b writes one JSON line
    per dirty-and-diverged site checkout to checkout-drift.jsonl. Regression
    lock for the 2026-09-05 incident: four checkouts sat silently broken for a
    month because nothing surfaced the boot-time warning."""

    def test_missing_file_yields_no_incidents(self, tmp_path):
        assert checkout_drift_incidents(path=tmp_path / "missing.jsonl") == []

    def test_drift_signal_becomes_an_incident(self, tmp_path):
        p = tmp_path / "checkout-drift.jsonl"
        p.write_text(
            '{"ts":"2026-09-05T00:00:00Z","checkout":"biglobster-seo","ahead":4,'
            '"reason":"dirty tracked file + local commits ahead of origin/main"}\n',
            encoding="utf-8",
        )
        incs = checkout_drift_incidents(path=p)
        assert len(incs) == 1
        assert "biglobster-seo" in incs[0].title
        assert "biglobster-seo" in incs[0].handoff

    def test_malformed_lines_are_skipped(self, tmp_path):
        p = tmp_path / "checkout-drift.jsonl"
        p.write_text("not json\n\n", encoding="utf-8")
        assert checkout_drift_incidents(path=p) == []

    def test_checkout_drift_incident_flows_through_sweep(self, tmp_path):
        cd = [Incident(id="checkout-drift:biglobster-seo:4", kind="checkout_drift",
                       title="Site checkout 'biglobster-seo' diverged and stopped pulling",
                       detail="ahead: 4", handoff="inspect biglobster-seo before resetting")]
        out = sweep(jobs=[_ok_job()], langfuse=[], checkout_drift=cd,
                   state_path=tmp_path / "s.json")
        assert "biglobster-seo" in out
