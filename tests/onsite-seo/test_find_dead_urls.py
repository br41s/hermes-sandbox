"""Tests for the dead-URL detection script.

The one thing worth getting wrong here is the two-run confirmation: a URL
must look dead on THIS run and on a PRIOR run before it's eligible for the
write path, and a transient 5xx/timeout must never advance or reset that
counter. Everything else is plumbing.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "onsite-seo" / "find_dead_urls.py"
_spec = importlib.util.spec_from_file_location("find_dead_urls", _MODULE_PATH)
mod = importlib.util.module_from_spec(_spec)
sys.modules["find_dead_urls"] = mod
_spec.loader.exec_module(mod)


def gsc_response(*urls_with_clicks):
    return {
        "rows": [
            {"keys": [url], "clicks": clicks, "impressions": max(clicks, 1) * 10}
            for url, clicks in urls_with_clicks
        ]
    }


def site_state(*live_urls):
    return {"internal_link_graph": {u: {} for u in live_urls}}


def write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def run(tmp_path, monkeypatch, statuses, gsc_urls, live_urls, history=None):
    gsc_path = tmp_path / "gsc.json"
    state_path = tmp_path / "site-state.json"
    history_path = tmp_path / "history.json"
    out_path = tmp_path / "out.json"

    write(gsc_path, gsc_response(*gsc_urls))
    write(state_path, site_state(*live_urls))
    if history is not None:
        write(history_path, history)

    monkeypatch.setattr(mod, "_fetch_status", lambda url: {"status": statuses[url]})

    mod.main([
        "--gsc-pages", str(gsc_path),
        "--site-state", str(state_path),
        "--history", str(history_path),
        "--out", str(out_path),
    ])
    return json.loads(out_path.read_text(encoding="utf-8")), json.loads(history_path.read_text(encoding="utf-8"))


def test_url_still_on_the_current_site_is_never_a_candidate(tmp_path, monkeypatch):
    out, _ = run(
        tmp_path, monkeypatch,
        statuses={},
        gsc_urls=[("https://x/live", 5)],
        live_urls=["https://x/live"],
    )
    assert out["checked"] == 0
    assert out["confirmed_dead"] == []


def test_first_time_dead_is_pending_not_confirmed(tmp_path, monkeypatch):
    out, history = run(
        tmp_path, monkeypatch,
        statuses={"https://x/gone": 404},
        gsc_urls=[("https://x/gone", 5)],
        live_urls=[],
    )
    assert out["confirmed_dead"] == []
    assert len(out["pending_confirmation"]) == 1
    assert history["dead_urls"]["https://x/gone"]["last_status"] == 404


def test_dead_on_two_separate_runs_is_confirmed(tmp_path, monkeypatch):
    prior_history = {"dead_urls": {"https://x/gone": {
        "last_status": 404, "last_checked": "2026-01-01T00:00:00+00:00",
        "first_seen_dead": "2026-01-01T00:00:00+00:00",
    }}}
    out, _ = run(
        tmp_path, monkeypatch,
        statuses={"https://x/gone": 404},
        gsc_urls=[("https://x/gone", 5)],
        live_urls=[],
        history=prior_history,
    )
    assert len(out["confirmed_dead"]) == 1
    assert out["confirmed_dead"][0]["url"] == "https://x/gone"
    assert out["pending_confirmation"] == []


def test_a_5xx_is_inconclusive_and_does_not_touch_the_confirmation_counter(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    prior_history = {"dead_urls": {"https://x/flaky": {
        "last_status": 404, "last_checked": recent, "first_seen_dead": recent,
    }}}
    out, history = run(
        tmp_path, monkeypatch,
        statuses={"https://x/flaky": 503},
        gsc_urls=[("https://x/flaky", 5)],
        live_urls=[],
        history=prior_history,
    )
    assert out["confirmed_dead"] == []
    assert len(out["inconclusive"]) == 1
    # History is untouched — a 5xx must not silently confirm or reset progress.
    assert history["dead_urls"]["https://x/flaky"]["last_status"] == 404


def test_a_url_that_recovers_to_200_is_dropped_from_history(tmp_path, monkeypatch):
    prior_history = {"dead_urls": {"https://x/back": {
        "last_status": 404, "last_checked": "2026-01-01T00:00:00+00:00",
        "first_seen_dead": "2026-01-01T00:00:00+00:00",
    }}}
    out, history = run(
        tmp_path, monkeypatch,
        statuses={"https://x/back": 200},
        gsc_urls=[("https://x/back", 5)],
        live_urls=[],
        history=prior_history,
    )
    assert len(out["now_alive"]) == 1
    assert "https://x/back" not in history["dead_urls"]


def test_low_impression_pages_are_filtered_out(tmp_path, monkeypatch):
    # gsc_response()'s helper always gives impressions >= 10, so a true
    # zero-impression row is built directly here to exercise the filter.
    gsc_path = tmp_path / "gsc2.json"
    write(gsc_path, {"rows": [{"keys": ["https://x/z"], "clicks": 0, "impressions": 0}]})
    state_path = tmp_path / "site-state2.json"
    write(state_path, site_state())
    history_path = tmp_path / "history2.json"
    out_path = tmp_path / "out2.json"
    monkeypatch.setattr(mod, "_fetch_status", lambda url: {"status": 404})
    mod.main([
        "--gsc-pages", str(gsc_path),
        "--site-state", str(state_path),
        "--history", str(history_path),
        "--out", str(out_path),
    ])
    result = json.loads(out_path.read_text(encoding="utf-8"))
    assert result["checked"] == 0
