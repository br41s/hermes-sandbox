"""Fork: a timezone edited in config.yaml by another process takes effect without a restart.

2026-10-04: ``hermes -p bl-shoroban config set timezone Asia/Bangkok`` ran while the
multiplexed gateway already held the profile's zone as UTC (unset) in ``hermes_time``'s
cache. The gateway kept UTC and re-anchored the rental's 12:45 Bangkok cron run to 13:45.
"""
import os

import pytest

import hermes_time


@pytest.fixture(autouse=True)
def _fresh_tz_cache(monkeypatch):
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    hermes_time.reset_cache()
    yield
    hermes_time.reset_cache()


def _write(home, text, bump_ns):
    path = home / "config.yaml"
    path.write_text(text, encoding="utf-8")
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + bump_ns))  # a distinct mtime, always


def test_an_edit_from_another_process_is_picked_up_without_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write(tmp_path, "{}\n", 0)
    assert hermes_time.get_timezone_name() == ""  # unset: server-local

    _write(tmp_path, "timezone: Asia/Bangkok\n", 10**9)
    assert hermes_time.get_timezone_name() == "Asia/Bangkok"
    assert hermes_time.now().utcoffset().total_seconds() == 7 * 3600

    _write(tmp_path, "timezone: Europe/Madrid\n", 2 * 10**9)
    assert hermes_time.get_timezone_name() == "Europe/Madrid"


def test_superseded_entries_for_the_same_file_are_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for i, zone in enumerate(("UTC", "Asia/Bangkok", "Europe/Madrid")):
        _write(tmp_path, f"timezone: {zone}\n", i * 10**9)
        hermes_time.get_timezone()
    keys = [k for k in hermes_time._tz_cache if k[0] == "config"]
    assert len(keys) == 1


def test_a_missing_config_file_is_still_cached_as_server_local(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert hermes_time.get_timezone_name() == ""
    _write(tmp_path, "timezone: Asia/Bangkok\n", 0)  # created later: picked up too
    assert hermes_time.get_timezone_name() == "Asia/Bangkok"
