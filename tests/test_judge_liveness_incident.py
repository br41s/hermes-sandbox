"""Regression lock for the auditor judge's liveness signal.

The judge's real outage produced NO error to detect: it shipped 2026-06-24
wired through a pipe the cron approval gate refused, so for twelve weeks it was
never invoked at all. Nothing threw, nothing exited non-zero, and every
content-tier PR auto-merged on the orchestrator model alone.

These tests pin the only check that catches that shape — "has it succeeded
lately" rather than "did it fail" — and in particular that a MISSING stamp
file alerts instead of staying quiet, since "never ran" is the exact condition
that went unnoticed.

Hermetic: synthetic paths and an injected clock, no real HERMES_HOME.
"""
import json
from datetime import datetime, timedelta, timezone

from incidents.sweep import JUDGE_LIVENESS_HOURS, judge_liveness_incidents

NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)


def _stamp(tmp_path, ago_hours):
    p = tmp_path / "judge-liveness.json"
    p.write_text(json.dumps(
        {"last_success_at": (NOW - timedelta(hours=ago_hours)).isoformat()}
    ), encoding="utf-8")
    return p


def test_missing_stamp_alerts(tmp_path):
    # The twelve-week case: the judge never succeeded, so the file never existed.
    # Silence here is what let it hide.
    out = judge_liveness_incidents(tmp_path / "absent.json", now=NOW)
    assert len(out) == 1
    assert out[0].id == "judge_liveness:never"
    assert "never" in out[0].detail


def test_recent_success_is_silent(tmp_path):
    assert judge_liveness_incidents(_stamp(tmp_path, 2), now=NOW) == []


def test_just_inside_threshold_is_silent(tmp_path):
    p = _stamp(tmp_path, JUDGE_LIVENESS_HOURS - 1)
    assert judge_liveness_incidents(p, now=NOW) == []


def test_past_threshold_alerts(tmp_path):
    out = judge_liveness_incidents(_stamp(tmp_path, JUDGE_LIVENESS_HOURS + 1), now=NOW)
    assert len(out) == 1
    assert out[0].kind == "judge_liveness"
    assert "AUTO-MERGE" in out[0].detail


def test_corrupt_stamp_is_treated_as_never(tmp_path):
    # Fail loud, not open: an unreadable stamp must not read as "healthy".
    p = tmp_path / "junk.json"
    p.write_text("{not json", encoding="utf-8")
    out = judge_liveness_incidents(p, now=NOW)
    assert len(out) == 1
    assert out[0].id == "judge_liveness:never"


def test_a_new_stall_after_recovery_is_a_new_incident(tmp_path):
    # Dedup is by id, so a stable id would swallow the second outage entirely.
    first = judge_liveness_incidents(_stamp(tmp_path, 100), now=NOW)[0]
    later = judge_liveness_incidents(_stamp(tmp_path, 200), now=NOW)[0]
    assert first.id != later.id


def test_record_judge_success_round_trips(tmp_path, monkeypatch):
    import auditor.llm as llm

    monkeypatch.setattr(llm, "_liveness_path", lambda: tmp_path / "judge-liveness.json")
    llm.record_judge_success(now=NOW.isoformat())
    out = judge_liveness_incidents(tmp_path / "judge-liveness.json", now=NOW)
    assert out == [], "a stamp just written must clear the alert"


def test_record_judge_success_never_raises(monkeypatch):
    # A write failure must not fail a review that actually ran.
    import auditor.llm as llm

    monkeypatch.setattr(llm, "_liveness_path", lambda: (_ for _ in ()).throw(OSError("nope")))
    llm.record_judge_success()  # must not raise


def test_judge_liveness_incident_flows_through_sweep(tmp_path):
    """The check must reach the delivered brief, not just return an Incident."""
    from incidents.sweep import Incident, sweep

    jl = [Incident(
        id="judge_liveness:never",
        kind="judge_liveness",
        title="Auditor judge has not run — PR reviews are unaided",
        detail="last successful judge verdict: never",
        handoff="check python -m auditor.llm",
    )]
    out = sweep(
        jobs=[{"id": "ok1", "name": "healthy", "last_error": None,
               "last_delivery_error": None,
               "last_run_at": datetime.now(timezone.utc).isoformat()}],
        langfuse=[], checkout_drift=[], judge_liveness=jl,
        state_path=tmp_path / "s.json",
    )
    assert "judge has not run" in out.lower()


def test_sweep_defaults_to_checking_liveness_for_real(tmp_path, monkeypatch):
    """Omitting the argument must NOT mean 'assume healthy'.

    The whole outage was an absence nobody checked for, so the default path has
    to actually look.
    """
    import incidents.sweep as sw

    called = {"n": 0}

    def _spy(*a, **k):
        called["n"] += 1
        return []

    monkeypatch.setattr(sw, "judge_liveness_incidents", _spy)
    sw.sweep(
        jobs=[{"id": "ok1", "name": "healthy", "last_error": None,
               "last_delivery_error": None,
               "last_run_at": datetime.now(timezone.utc).isoformat()}],
        langfuse=[], checkout_drift=[], state_path=tmp_path / "s.json",
    )
    assert called["n"] == 1, "sweep() must consult the liveness check by default"
