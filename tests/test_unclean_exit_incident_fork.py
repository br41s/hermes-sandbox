"""Regression lock for the unclean-gateway-exit signal.

s6-overlay's 3s default stop grace SIGKILLed the gateway mid-shutdown on 5 of
13 restarts (27-29 Sep 2026). Each next boot ran ``PRAGMA quick_check`` on the
2.2 GB state.db before connecting Telegram, silently dropping messages for up
to four minutes. PR #360 raised the grace; these tests pin the signal that
tells us whether it was enough.

Hermetic: synthetic exit-diag files, an injected clock and state path.
"""
import json
from datetime import datetime, timedelta, timezone

from incidents.sweep import sweep, unclean_exit_incidents

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)


def _record(ago_hours=1, prior_pid=4242, integrity="ok", oom=False, tag="gateway.previous_unclean_exit"):
    rec = {
        "ts": (NOW - timedelta(hours=ago_hours)).isoformat(), "tag": tag, "pid": 5151,
        "prior_pid": prior_pid, "prior_started_at": (NOW - timedelta(hours=9)).isoformat(),
        "last_heartbeat_at": (NOW - timedelta(hours=ago_hours, seconds=20)).isoformat(),
        "state_db_integrity": integrity,
    }
    if oom:
        rec["suspected_oom"] = True
    return rec


def _log(tmp_path, *records, junk=()):
    p = tmp_path / "gateway-exit-diag.log"
    p.write_text("\n".join([json.dumps(r) for r in records] + list(junk)) + "\n", encoding="utf-8")
    return p


def _sweep(incidents, state_path):
    return sweep(jobs=[], langfuse=[], blocked=[], checkout_drift=[], deploy_drift=[],
                 judge_liveness=[], dependency_alerts=[], unclean_exits=incidents,
                 state_path=state_path, now=NOW)


def test_new_record_is_one_incident(tmp_path):
    out = unclean_exit_incidents(_log(tmp_path, _record()), now=NOW)
    assert len(out) == 1
    inc = out[0]
    assert inc.kind == "gateway"
    assert inc.id == f"gateway-unclean:{_record()['ts']}:4242"
    assert "FAILED" not in inc.title
    assert "state.db integrity: ok" in inc.detail
    assert "suspected OOM: no" in inc.detail
    assert "S6_SERVICES_GRACETIME" in inc.detail  # points at the likely cause
    assert "hermes doctor" not in inc.detail


def test_repeat_is_deduped_across_sweeps(tmp_path):
    path = _log(tmp_path, _record())
    sp = tmp_path / "state.json"
    first = _sweep(unclean_exit_incidents(path, now=NOW), sp)
    assert "Gateway died uncleanly" in first
    second = _sweep(unclean_exit_incidents(path, now=NOW), sp)
    assert "Gateway died uncleanly" not in second


def test_second_death_is_a_new_incident(tmp_path):
    sp = tmp_path / "state.json"
    _sweep(unclean_exit_incidents(_log(tmp_path, _record()), now=NOW), sp)
    both = _log(tmp_path, _record(), _record(ago_hours=0.5, prior_pid=5151))
    out = _sweep(unclean_exit_incidents(both, now=NOW), sp)
    assert out.count("Gateway died uncleanly") == 1
    assert "pid 5151" in out


def test_corrupted_verdict_uses_severe_wording(tmp_path):
    rec = _record(integrity="*** in database main ***\nPage 12: btreeInitPage() returns error code 11")
    [inc] = unclean_exit_incidents(_log(tmp_path, rec), now=NOW)
    assert "FAILED its integrity check" in inc.title
    assert "DAMAGED" in inc.detail and "hermes doctor" in inc.detail
    assert inc.handoff.startswith("run `hermes doctor`")


def test_check_failed_verdict_is_also_severe(tmp_path):
    [inc] = unclean_exit_incidents(_log(tmp_path, _record(integrity="check-failed: database is locked")),
                                   now=NOW)
    assert "FAILED" in inc.title


def test_absent_db_is_not_severe(tmp_path):
    [inc] = unclean_exit_incidents(_log(tmp_path, _record(integrity="absent")), now=NOW)
    assert "FAILED" not in inc.title and "hermes doctor" not in inc.detail


def test_suspected_oom_is_named(tmp_path):
    [inc] = unclean_exit_incidents(_log(tmp_path, _record(oom=True)), now=NOW)
    assert "suspected OOM: YES" in inc.detail
    assert "OOM killer" in inc.detail


def test_no_file_is_silent(tmp_path):
    assert unclean_exit_incidents(tmp_path / "absent.log", now=NOW) == []


def test_unreadable_path_is_silent(tmp_path):
    # A directory where the file should be: read fails, the watcher stays quiet.
    assert unclean_exit_incidents(tmp_path, now=NOW) == []


def test_other_tags_junk_and_old_records_are_ignored(tmp_path):
    # The log is shared with the CLI's _exit_diag and never pruned.
    path = _log(tmp_path,
                _record(tag="gateway.asyncio_run_returned"),
                _record(ago_hours=72),
                junk=["{not json", "", "[1, 2]"])
    assert unclean_exit_incidents(path, now=NOW) == []


def test_producer_record_round_trips(tmp_path):
    # Pin the contract with gateway/lifecycle_ledger.py: a field rename there
    # must fail here, not silently blind the watcher.
    from gateway.lifecycle_ledger import _report_unclean_exit

    _report_unclean_exit({"prior_pid": 777, "prior_started_at": "2026-09-29T03:00:00+00:00",
                          "last_heartbeat_at": "2026-09-29T11:00:00+00:00", "suspected_oom": True},
                         home=tmp_path)
    [inc] = unclean_exit_incidents(tmp_path / "logs" / "gateway-exit-diag.log")
    assert "pid 777" in inc.detail
    assert "state.db integrity: absent" in inc.detail  # no state.db in tmp_path
    assert "suspected OOM: YES" in inc.detail
