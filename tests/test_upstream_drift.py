"""Tests for scripts/upstream_drift.py — the upstream-drift watchdog.

The failure this guards is silent and total: a watchdog that reports a stale
tag as "newest" never fires, and nobody notices because its whole job is to
stay quiet.
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
        assert drift._version_key("not-a-version") == (0, 0, 0)
        assert drift._version_key("v2026.7.7") > drift._version_key("not-a-version")


class TestSilenceContract:
    """It must print NOTHING when there is nothing to do.

    The cron job runs in --no-agent mode where stdout IS the delivered message,
    so any stray output becomes a weekly Telegram notification.
    """

    def test_matching_version_is_silent(self, drift, monkeypatch, capsys):
        monkeypatch.setattr(drift, "_recorded_version", lambda: "v2026.7.30")
        monkeypatch.setattr(drift, "_api_latest_tag", lambda: ("v2026.7.30", "2026-07-30T00:00:00Z"))
        assert drift._remote_report(30) == 0
        assert capsys.readouterr().out == ""

    def test_new_tag_under_threshold_is_silent(self, drift, monkeypatch, capsys):
        """A tag cut today is not yet a reason to nag."""
        from datetime import datetime, timedelta, timezone

        fresh = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        monkeypatch.setattr(drift, "_recorded_version", lambda: "v2026.7.20")
        monkeypatch.setattr(drift, "_api_latest_tag", lambda: ("v2026.7.30", fresh))
        assert drift._remote_report(30) == 0
        assert capsys.readouterr().out == ""

    def test_stale_tag_over_threshold_reports(self, drift, monkeypatch, capsys):
        from datetime import datetime, timedelta, timezone

        old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        monkeypatch.setattr(drift, "_recorded_version", lambda: "v2026.7.20")
        monkeypatch.setattr(drift, "_api_latest_tag", lambda: ("v2026.7.30", old))
        assert drift._remote_report(30) == 1
        out = capsys.readouterr().out
        assert "v2026.7.20" in out and "v2026.7.30" in out and "45 days ago" in out
