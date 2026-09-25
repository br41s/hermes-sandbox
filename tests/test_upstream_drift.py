"""Tests for scripts/upstream_drift.py — the upstream-drift watchdog.

The failure this guards is silent and total: a watchdog that never fires looks
exactly like one with nothing to say. It happened twice — a string-sorted tag
list, then a threshold measured against a tag that was always days old — so it
now reports on every run and the tests pin that down.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "upstream_drift.py"


@pytest.fixture(scope="module")
def drift():
    spec = importlib.util.spec_from_file_location("upstream_drift", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestVersionOrdering:
    """Regression: tags MUST sort numerically, not as strings.

    Caught on the script's first real run inside the container. Sorting
    ``vYYYY.M.D`` as strings puts v2026.7.7 above v2026.7.30 ('7' > '3'
    character-wise), so the watchdog reported a tag three weeks stale as the
    newest — it would have stayed silent through unbounded drift, which is the
    one outcome that makes it worse than not existing.
    """

    def test_double_digit_day_beats_single_digit(self, drift):
        assert drift._version_key("v2026.7.30") > drift._version_key("v2026.7.7")

    def test_double_digit_month_beats_single_digit(self, drift):
        assert drift._version_key("v2026.10.1") > drift._version_key("v2026.9.30")

    def test_year_dominates(self, drift):
        assert drift._version_key("v2027.1.1") > drift._version_key("v2026.12.31")

    def test_sorted_newest_first_matches_git_semantics(self, drift):
        tags = ["v2026.7.7", "v2026.7.30", "v2026.6.1", "v2026.10.2", "v2026.7.20"]
        newest = sorted(tags, key=drift._version_key, reverse=True)
        assert newest == [
            "v2026.10.2", "v2026.7.30", "v2026.7.20", "v2026.7.7", "v2026.6.1"
        ]

    def test_unparseable_tag_sorts_last_and_does_not_raise(self, drift):
        assert drift._version_key("not-a-version") == (0, 0, 0, 0)
        assert drift._version_key("v2026.7.7") > drift._version_key("not-a-version")


def _ago(days: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _tags(*names: str) -> list[tuple[str, str]]:
    return [(n, f"https://api.test/commits/{n}") for n in names]


class TestAlwaysReports:
    """Every run prints a status line — silence is never an answer.

    Regression: the watchdog used to print nothing under the threshold, and
    measured the threshold against upstream's NEWEST tag. Upstream releases
    every few days, so that tag was never 30 days old and the check said nothing
    while the fork fell 14 releases behind. A silent watchdog and a broken one
    look identical from Telegram.
    """

    def test_up_to_date_still_reports(self, drift, monkeypatch, capsys):
        monkeypatch.setattr(drift, "_recorded_version", lambda: "v2026.7.30")
        monkeypatch.setattr(drift, "_api_release_tags", lambda: _tags("v2026.7.30", "v2026.7.20"))
        assert drift._remote_report(30) == 0
        out = capsys.readouterr().out
        assert "up to date" in out and "v2026.7.30" in out

    def test_behind_under_threshold_reports_but_not_due(self, drift, monkeypatch, capsys):
        monkeypatch.setattr(drift, "_recorded_version", lambda: "v2026.7.20")
        monkeypatch.setattr(drift, "_api_release_tags", lambda: _tags("v2026.7.30", "v2026.7.20"))
        monkeypatch.setattr(drift, "_api_tag_date", lambda url: _ago(2))
        assert drift._remote_report(30) == 0
        out = capsys.readouterr().out
        assert "not due" in out and "1 release behind" in out

    def test_due_is_judged_by_the_oldest_unmerged_tag(self, drift, monkeypatch, capsys):
        """The newest tag is 1 day old; the oldest unmerged one is 45."""
        monkeypatch.setattr(drift, "_recorded_version", lambda: "v2026.7.20")
        monkeypatch.setattr(drift, "_api_release_tags",
                            lambda: _tags("v2026.9.24", "v2026.8.16.2", "v2026.7.30", "v2026.7.20"))
        dates = {"v2026.9.24": _ago(1), "v2026.8.16.2": _ago(20), "v2026.7.30": _ago(45)}
        monkeypatch.setattr(drift, "_api_tag_date", lambda url: dates[url.rsplit("/", 1)[1]])
        assert drift._remote_report(30) == 1
        out = capsys.readouterr().out
        assert "merge is due" in out and "3 releases behind" in out
        assert "v2026.7.30, came out 45 days ago" in out and "v2026.9.24" in out

    def test_unknown_age_counts_as_due(self, drift, monkeypatch, capsys):
        monkeypatch.setattr(drift, "_recorded_version", lambda: "v2026.7.20")
        monkeypatch.setattr(drift, "_api_release_tags", lambda: _tags("v2026.7.30", "v2026.7.20"))
        monkeypatch.setattr(drift, "_api_tag_date", lambda url: "")
        assert drift._remote_report(30) == 1

    def test_api_failure_is_an_error_not_silence(self, drift, monkeypatch):
        monkeypatch.setattr(drift, "_recorded_version", lambda: "v2026.7.20")
        monkeypatch.setattr(drift, "_api_release_tags", lambda: None)
        assert drift._remote_report(30) == 2

    def test_missing_upstream_version_is_an_error(self, drift, monkeypatch):
        monkeypatch.setattr(drift, "_recorded_version", lambda: None)
        assert drift._remote_report(30) == 2


class TestFourPartTags:
    """Upstream cuts same-day re-releases like v2026.8.16.2."""

    def test_fourth_component_parses_and_orders(self, drift):
        assert drift._version_key("v2026.8.16.2") > drift._version_key("v2026.8.16")
        assert drift._version_key("v2026.8.18") > drift._version_key("v2026.8.16.2")


class TestDeferToActions:
    """Hermes fallback: silent only when the Actions workflow already reported."""

    def _run_main(self, drift, monkeypatch, reported):
        monkeypatch.setattr(drift, "_actions_reported", lambda hours: reported)
        monkeypatch.setattr(drift, "_have_git_repo", lambda: False)
        monkeypatch.setattr(drift, "_remote_report", lambda t: print("STATUS") or 1)
        monkeypatch.setattr(drift.sys, "argv", ["upstream_drift.py", "--defer-to-actions"])
        return drift.main()

    def test_silent_when_actions_succeeded(self, drift, monkeypatch, capsys):
        assert self._run_main(drift, monkeypatch, (True, "succeeded 20h ago")) == 0
        assert capsys.readouterr().out == ""

    def test_reports_and_says_why_when_actions_did_not(self, drift, monkeypatch, capsys):
        assert self._run_main(drift, monkeypatch, (False, "it has never succeeded")) == 1
        out = capsys.readouterr().out
        assert out.startswith("STATUS") and "Hermes fallback" in out and "never succeeded" in out

    def _runs(self, drift, monkeypatch, payload):
        monkeypatch.setattr(drift, "_api_get", lambda url: payload)
        return drift._actions_reported(72)

    def test_recent_success_counts(self, drift, monkeypatch):
        ok, _ = self._runs(drift, monkeypatch, {"workflow_runs": [{"conclusion": "failure", "run_started_at": _ago(0)}, {"conclusion": "success", "run_started_at": _ago(1)}]})
        assert ok

    def test_last_week_success_does_not_count(self, drift, monkeypatch):
        ok, why = self._runs(drift, monkeypatch, {"workflow_runs": [{"conclusion": "success", "run_started_at": _ago(8)}]})
        assert not ok and "8 days ago" in why

    def test_no_runs_does_not_count(self, drift, monkeypatch):
        ok, why = self._runs(drift, monkeypatch, {"workflow_runs": []})
        assert not ok and "never" in why

    def test_only_failed_runs_do_not_count(self, drift, monkeypatch):
        """A run that failed to send to Telegram must not silence the fallback."""
        ok, why = self._runs(drift, monkeypatch,
                             {"workflow_runs": [{"conclusion": "failure", "run_started_at": _ago(0)}]})
        assert not ok and "no successful run" in why

    def test_unreachable_api_does_not_count(self, drift, monkeypatch):
        def boom(url):
            raise drift.urllib.error.URLError("down")

        monkeypatch.setattr(drift, "_api_get", boom)
        ok, why = drift._actions_reported(72)
        assert not ok and "could not reach" in why


class TestRecordedVersionMustBeMerged:
    """UPSTREAM_VERSION must name a tag that is an ancestor of HEAD.

    Recording a tag we have not merged makes the watchdog silent until
    upstream's next release. It is the easiest slip in the whole runbook: the
    newest tag is on screen throughout the merge, and it is precisely the wrong
    value — it is what you are merging TOWARD, not what is merged. Nearly
    written for real on 2026-07-31, which is why the check exists.
    """

    def test_unmerged_recorded_tag_is_reported_not_silent(self, drift, monkeypatch, capsys):
        monkeypatch.setattr(drift, "_recorded_version", lambda: "v2099.1.1")
        monkeypatch.setattr(drift, "_run", lambda *a, **k: "v2099.1.1\nv2026.7.20")

        class _NotAncestor:
            returncode = 1

        monkeypatch.setattr(drift.subprocess, "run", lambda *a, **k: _NotAncestor())
        assert drift._full_report(30) == 1
        out = capsys.readouterr().out
        assert "NOT merged" in out and "v2099.1.1" in out
