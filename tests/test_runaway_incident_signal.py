"""Runaway-agent incident signal.

The blind spot this closes, from production: the auditor cron stopped doing any
work on 2026-09-09 and nothing surfaced it until 2026-09-13. Its runs burned the
full 90-iteration budget re-running the same command, then exited at the cap —
which is a CLEAN exit, so:

  * ``cron_failure_incidents`` saw nothing (last_status stayed "ok")
  * ``cron_stale_incidents`` saw nothing (the job ran on schedule)
  * ``prompt_drift_incidents`` saw nothing (the prompt never changed)

Every existing signal keys on failure. This one keys on a success that did no
work.

What tells a cap hit from a run that chose to stop is in the session DB, not
the call count. run_agent appends a ``user`` row with
``MAX_ITERATIONS_SUMMARY_REQUEST`` as its content right before the final
assistant message when the cap cuts a run short; the scheduler books
``end_reason = cron_complete`` only when the last row is a real assistant
reply. The Shoroban Product Sheet Writer (``cron_b2f774557766_*``) showed why
the count alone is wrong: between 2026-09-29 and 2026-10-13 the signal paged it
12 times, 5 of which were full reports delivered by choice at 85–86 of 90.
"""

from datetime import datetime, timezone

from agent.context_compressor import MAX_ITERATIONS_SUMMARY_REQUEST
from incidents.sweep import (
    RUNAWAY_DEFAULT_MAX_TURNS,
    Incident,
    runaway_incidents,
    _recent_runs,
    _session_dbs,
)


NOW = datetime(2026, 9, 13, 4, 0, tzinfo=timezone.utc)


def _row(sid="cron_c19bb95c0a62_20260913_011704", calls=90,
         model="deepseek/deepseek-v4-flash-0731",
         end_reason="cron_complete", cap_hit=False):
    """A ``_recent_runs`` row. Defaults describe a run that finished with a
    real reply and no forced summary request."""
    return (sid, NOW.timestamp(), calls, model, end_reason, int(cap_hit))


def _cap_hit(**kw):
    """The session holds run_agent's forced summary request: a true cap hit."""
    return _row(cap_hit=True, **kw)


def _chose_to_stop(**kw):
    """No forced request, and the scheduler saw a real reply as the last row."""
    return _row(cap_hit=False, end_reason="cron_complete", **kw)


def _cut_short(**kw):
    """No forced request, but the run did not close with an assistant reply."""
    return _row(cap_hit=False, end_reason="cron_incomplete_no_output", **kw)


# --- a true cap hit pages ------------------------------------------------------


def test_run_at_the_cap_is_reported():
    out = runaway_incidents(now=NOW, rows=[_cap_hit(calls=90)])
    assert len(out) == 1
    assert isinstance(out[0], Incident)
    assert "90/90" in out[0].title
    assert "forced summary request" in out[0].detail


def test_shoroban_run_cut_mid_product_pages():
    """7 of the 12 Shoroban pages were real: the run hit 90 with the 8th
    product's research still open, and the forced request is in the session
    (confirmed on cron_b2f774557766_20261009_131250)."""
    out = runaway_incidents(now=NOW, rows=[
        _cap_hit(sid="cron_b2f774557766_20261009_131250", calls=90)])
    assert len(out) == 1
    assert out[0].id == "runaway:cron_b2f774557766_20261009_131250"


def test_cap_hit_pages_at_any_count():
    """The marker is the evidence, not the count: a job with a lower per-job
    cap, or a count that undershoots the cap, is still a run that was cut."""
    for calls in (16, 60, 85):
        out = runaway_incidents(now=NOW, rows=[_cap_hit(calls=calls)])
        assert len(out) == 1, calls
        assert f"{calls}/90" in out[0].title


def test_cap_hit_with_a_bad_count_still_pages():
    out = runaway_incidents(now=NOW, rows=[_cap_hit(calls=None)])
    assert len(out) == 1
    assert "?/90" in out[0].title


# --- a run that chose to stop is silent, even next to the cap -----------------


def test_healthy_run_is_silent():
    """A run that chose to stop. The real fixed auditor run used 12 calls."""
    assert runaway_incidents(now=NOW, rows=[_chose_to_stop(calls=12)]) == []


def test_busiest_healthy_run_is_still_silent():
    """60 calls was the maximum across ~200 healthy auditor runs."""
    assert runaway_incidents(now=NOW, rows=[_chose_to_stop(calls=60)]) == []


def test_shoroban_full_report_near_the_cap_is_silent():
    """5 of the 12 Shoroban pages were false: 10 products at ~8.5 iterations
    each, a full report, ``Turn ended: reason=text_response(finish_reason=stop)``
    at 85–86. No forced request, ``cron_complete``: it chose to stop."""
    for calls in (85, 86, 89, 90):
        rows = [_chose_to_stop(sid="cron_b2f774557766_20261013_171250", calls=calls)]
        assert runaway_incidents(now=NOW, rows=rows) == [], calls


# --- near the cap with no reply: the count fallback still pages ---------------


def test_near_cap_without_a_reply_pages():
    """No marker and no real reply: the session cannot vouch for a chosen stop,
    so the 95% threshold keeps its old meaning for this shape."""
    out = runaway_incidents(now=NOW, rows=[_cut_short(calls=86)])
    assert len(out) == 1
    assert "near its iteration cap" in out[0].title
    assert "cron_incomplete_no_output" in out[0].detail


def test_threshold_sits_between_healthy_and_runaway():
    """The fallback threshold is 95% of the cap, not the cap exactly.

    Production had a genuine runaway that stopped at 86/90 (23.8 minutes,
    re-running the same command) — an agent starved of budget rarely lands
    exactly on the ceiling, so requiring == max_turns would have missed it.
    84 stays silent: there is no observed healthy run above 60, but nothing
    between 61 and 85 either, so the signal does not claim more than it knows.
    """
    assert runaway_incidents(now=NOW, rows=[_cut_short(calls=84)]) == []
    assert len(runaway_incidents(now=NOW, rows=[_cut_short(calls=86)])) == 1
    assert len(runaway_incidents(now=NOW, rows=[_cut_short(calls=90)])) == 1


def test_custom_cap_scales_the_threshold():
    assert runaway_incidents(now=NOW, rows=[_cut_short(calls=20)], max_turns=20)
    assert runaway_incidents(now=NOW, rows=[_cut_short(calls=20)], max_turns=90) == []


# --- shape ----------------------------------------------------------------------


def test_id_is_stable_for_dedup():
    """sweep() dedups on id, so one bad run must report exactly once."""
    a = runaway_incidents(now=NOW, rows=[_cap_hit()])[0]
    b = runaway_incidents(now=NOW, rows=[_cap_hit()])[0]
    assert a.id == b.id == "runaway:cron_c19bb95c0a62_20260913_011704"


def test_distinct_runs_report_separately():
    out = runaway_incidents(now=NOW, rows=[
        _cap_hit(sid="cron_aaa_1"), _cap_hit(sid="cron_bbb_2")])
    assert len({i.id for i in out}) == 2


def test_detail_names_the_job_and_model():
    """The model is in the brief because a silent model/provider swap is what
    caused the incident this signal exists for."""
    inc = runaway_incidents(now=NOW, rows=[_cap_hit()])[0]
    assert "c19bb95c0a62" in inc.detail
    assert "deepseek/deepseek-v4-flash-0731" in inc.detail


def test_malformed_rows_never_raise():
    """The watcher must not die because one row is odd — a broken signal that
    takes the whole sweep down is worse than the gap it was closing."""
    rows = [_cut_short(calls=None), _cut_short(calls="90"),
            ("weird-id", NOW.timestamp(), 90, None, "cron_incomplete_no_output", 0)]
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


# --- reading the marker from the session DB --------------------------------------


def _bare_db(path):
    import sqlite3

    con = sqlite3.connect(path)
    con.execute("CREATE TABLE sessions (id TEXT, started_at REAL, ended_at REAL,"
                " api_call_count INTEGER, model TEXT, end_reason TEXT)")
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT,"
                " role TEXT, content TEXT)")
    return con


def test_recent_runs_reads_the_marker_by_its_shared_content(tmp_path):
    """The forced request is matched by ``MAX_ITERATIONS_SUMMARY_REQUEST``
    itself, as a prefix of a ``user`` row — the same constant run_agent
    appends, so the two cannot drift apart."""
    con = _bare_db(tmp_path / "state.db")
    far = 99999999999
    con.execute("INSERT INTO sessions VALUES ('cron_cut_1', ?, ?, 90, 'm', 'cron_complete')", (far, far))
    con.execute("INSERT INTO sessions VALUES ('cron_done_2', ?, ?, 85, 'm', 'cron_complete')", (far, far))
    con.execute("INSERT INTO messages (session_id, role, content) VALUES"
                " ('cron_cut_1', 'user', ?),"
                " ('cron_cut_1', 'assistant', 'Summary: ...'),"
                " ('cron_done_2', 'user', 'Write the sheets.'),"
                # Same text in the wrong role, or quoted mid-message, is not the marker.
                " ('cron_done_2', 'assistant', ?),"
                " ('cron_done_2', 'user', ?),"
                " ('cron_done_2', 'assistant', 'Report: 10 sheets done.')",
                (MAX_ITERATIONS_SUMMARY_REQUEST, MAX_ITERATIONS_SUMMARY_REQUEST,
                 "Note: " + MAX_ITERATIONS_SUMMARY_REQUEST))
    con.commit(); con.close()

    rows, unreadable = _recent_runs([tmp_path / "state.db"], since_epoch=0)
    assert unreadable == []
    assert {r[0]: r[5] for r in rows} == {"cron_cut_1": 1, "cron_done_2": 0}

    out = runaway_incidents(home=tmp_path)
    assert [i.id for i in out] == ["runaway:cron_cut_1"]


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
    con = _bare_db(tmp_path / "state.db")
    far = 99999999999
    con.execute("INSERT INTO sessions VALUES ('cron_x_1', ?, ?, 90, 'm', 'cron_complete')", (far, far))
    con.execute("INSERT INTO messages (session_id, role, content) VALUES ('cron_x_1', 'user', ?)",
                (MAX_ITERATIONS_SUMMARY_REQUEST,))
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


def test_marker_survives_the_real_session_db(tmp_path):
    """Through SessionDB itself, not a hand-built table: the forced request is
    stored as the plain string run_agent appends (``_encode_content`` keeps str
    content as-is), ``end_session`` writes ``end_reason``, and the query sees
    both. A cut run pages at any count; a run that replied by choice does not."""
    import sqlite3

    from hermes_state import SessionDB

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    try:
        db.create_session("cron_cut_1", "cron", model="m")
        db.append_message("cron_cut_1", "user", "Write the sheets.")
        db.append_message("cron_cut_1", "user", MAX_ITERATIONS_SUMMARY_REQUEST)
        db.append_message("cron_cut_1", "assistant", "Summary so far: ...")
        db.end_session("cron_cut_1", "cron_complete")

        db.create_session("cron_done_2", "cron", model="m")
        db.append_message("cron_done_2", "user", "Write the sheets.")
        db.append_message("cron_done_2", "assistant", "Report: 10 sheets done.")
        db.end_session("cron_done_2", "cron_complete")
    finally:
        db.close()
    con = sqlite3.connect(db_path)
    con.execute("UPDATE sessions SET api_call_count = 86")  # both next to the cap
    con.commit(); con.close()

    rows, unreadable = _recent_runs([db_path], since_epoch=0)
    assert unreadable == []
    assert {r[0]: (r[4], r[5]) for r in rows} == {
        "cron_cut_1": ("cron_complete", 1), "cron_done_2": ("cron_complete", 0)}
    assert [i.id for i in runaway_incidents(home=tmp_path)] == ["runaway:cron_cut_1"]
