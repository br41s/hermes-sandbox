"""Runaway-agent incident signal.

The blind spot this closes, from production: the auditor cron stopped doing any
work on 2026-09-09 and nothing surfaced it until 2026-09-13. Its runs burned the
full 90-iteration budget re-running the same command, then exited at the cap —
which is a CLEAN exit, so:

  * ``cron_failure_incidents`` saw nothing (last_status stayed "ok")
  * ``cron_stale_incidents`` saw nothing (the job ran on schedule)
  * ``prompt_drift_incidents`` saw nothing (the prompt never changed)

Every existing signal keys on failure. This one keys on a success that did no
work. Across ~200 healthy auditor runs the maximum was 60 calls against a cap of
90, so a run at the ceiling is a reliable tell.
"""

from datetime import datetime, timedelta, timezone

from incidents.sweep import (
    RUNAWAY_DEFAULT_MAX_TURNS,
    Incident,
    runaway_incidents,
    _recent_runs,
    _session_dbs,
)


NOW = datetime(2026, 9, 13, 4, 0, tzinfo=timezone.utc)


def _row(sid="cron_c19bb95c0a62_20260913_011704", calls=90,
         model="deepseek/deepseek-v4-flash-0731"):
    return (sid, NOW.timestamp(), calls, model)


def test_run_at_the_cap_is_reported():
    out = runaway_incidents(now=NOW, rows=[_row(calls=90)])
    assert len(out) == 1
    assert isinstance(out[0], Incident)
    assert "90/90" in out[0].title


def test_healthy_run_is_silent():
    """A run that chose to stop. The real fixed auditor run used 12 calls."""
    assert runaway_incidents(now=NOW, rows=[_row(calls=12)]) == []


def test_busiest_healthy_run_is_still_silent():
    """60 calls was the maximum across ~200 healthy runs — must not page."""
    assert runaway_incidents(now=NOW, rows=[_row(calls=60)]) == []


def test_threshold_sits_between_healthy_and_runaway():
    """The threshold is 95% of the cap, not the cap exactly.

    Production had a genuine runaway that stopped at 86/90 (23.8 minutes,
    re-running the same command) — an agent starved of budget rarely lands
    exactly on the ceiling, so requiring == max_turns would have missed it.
    84 stays silent: there is no observed healthy run above 60, but nothing
    between 61 and 85 either, so the signal does not claim more than it knows.
    """
    assert runaway_incidents(now=NOW, rows=[_row(calls=84)]) == []
    assert len(runaway_incidents(now=NOW, rows=[_row(calls=86)])) == 1
    assert len(runaway_incidents(now=NOW, rows=[_row(calls=90)])) == 1


def test_id_is_stable_for_dedup():
    """sweep() dedups on id, so one bad run must report exactly once."""
    a = runaway_incidents(now=NOW, rows=[_row()])[0]
    b = runaway_incidents(now=NOW, rows=[_row()])[0]
    assert a.id == b.id == "runaway:cron_c19bb95c0a62_20260913_011704"


def test_distinct_runs_report_separately():
    out = runaway_incidents(now=NOW, rows=[
        _row(sid="cron_aaa_1"), _row(sid="cron_bbb_2")])
    assert len({i.id for i in out}) == 2


def test_detail_names_the_job_and_model():
    """The model is in the brief because a silent model/provider swap is what
    caused the incident this signal exists for."""
    inc = runaway_incidents(now=NOW, rows=[_row()])[0]
    assert "c19bb95c0a62" in inc.detail
    assert "deepseek/deepseek-v4-flash-0731" in inc.detail


def test_custom_cap_scales_the_threshold():
    assert runaway_incidents(now=NOW, rows=[_row(calls=20)], max_turns=20)
    assert runaway_incidents(now=NOW, rows=[_row(calls=20)], max_turns=90) == []


def test_malformed_rows_never_raise():
    """The watcher must not die because one row is odd — a broken signal that
    takes the whole sweep down is worse than the gap it was closing."""
    rows = [_row(calls=None), _row(calls="90"), ("weird-id", NOW.timestamp(), 90, None)]
    out = runaway_incidents(now=NOW, rows=rows)
    assert len(out) == 1  # only the well-formed 90-call row
    assert out[0].detail.count("unknown") >= 1


def test_session_dbs_includes_profile_homes(tmp_path):
    """Profile jobs write to <profile>/state.db, not the default home the
    watcher runs under. Scanning only the default would miss the auditor and
    every gap hunter — i.e. exactly the jobs this signal is for."""
    (tmp_path / "state.db").write_text("")
    (tmp_path / "profiles" / "auditor").mkdir(parents=True)
    (tmp_path / "profiles" / "auditor" / "state.db").write_text("")
    (tmp_path / "profiles" / "empty").mkdir()
    found = {p.parent.name for p in _session_dbs(tmp_path)}
    assert "auditor" in found
    assert RUNAWAY_DEFAULT_MAX_TURNS == 90


def test_missing_home_is_silent(tmp_path):
    assert _session_dbs(tmp_path / "nope") == []


# --- the signal must not go silently blind (auditor review, PR #237) ---------


def test_unreadable_db_is_reported_not_swallowed(tmp_path):
    """A read-only sqlite open still attaches the WAL ``-shm`` segment, so an
    ownership or permissions change on a profile's state.db makes every read
    fail. Swallowing that would leave the watcher reporting "all clean" forever
    — the exact failure this signal exists to catch, one level up."""
    (tmp_path / "state.db").write_text("this is not a database")
    out = runaway_incidents(home=tmp_path)
    assert len(out) == 1
    assert "blind" in out[0].title.lower()
    assert "state.db" in out[0].detail


def test_blind_incident_id_is_stable_within_a_day(tmp_path):
    """Dedup must stop it paging hourly, but let it re-page tomorrow if the
    breakage is still there."""
    (tmp_path / "state.db").write_text("not a database")
    a = runaway_incidents(home=tmp_path)[0]
    b = runaway_incidents(home=tmp_path)[0]
    assert a.id == b.id


def test_partial_read_failure_does_not_cry_blind(tmp_path):
    """One broken profile DB while another still reads is degraded, not blind —
    reporting it as blind would be a false alarm on every sweep."""
    import sqlite3

    good = tmp_path / "state.db"
    con = sqlite3.connect(good)
    con.execute("CREATE TABLE sessions (id TEXT, started_at REAL,"
                " ended_at REAL, api_call_count INTEGER, model TEXT)")
    con.execute("INSERT INTO sessions VALUES ('cron_x_1', 99999999999, 99999999999, 90, 'm')")
    con.commit(); con.close()
    (tmp_path / "profiles" / "broken").mkdir(parents=True)
    (tmp_path / "profiles" / "broken" / "state.db").write_text("nope")

    out = runaway_incidents(home=tmp_path)
    assert [i for i in out if "blind" in i.title.lower()] == []
    assert [i for i in out if "iteration budget" in i.title]


def test_recent_runs_returns_rows_and_failures(tmp_path):
    rows, unreadable = _recent_runs([tmp_path / "missing.db"], since_epoch=0)
    assert rows == []
    assert len(unreadable) == 1
