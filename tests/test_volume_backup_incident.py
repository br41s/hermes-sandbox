"""Regression lock for the host volume-backup signal.

The host's nightly dump (ops/backup-volumes.sh from /etc/cron.d) logs OK/FAILED on
the host, where nothing reads it. These tests pin the outside check: the newest
dated folder in Drive must be recent and hold every Postgres dump; rclone refusing
is BLIND, not silence.

Hermetic: rclone listings and the clock are injected; rclone never runs.
"""
from datetime import datetime, timedelta, timezone

import pytest

import incidents.sweep as sw
from incidents.sweep import VOLUME_BACKUP_DUMPS, VOLUME_BACKUP_STALE_HOURS, volume_backup_incidents

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)


def _folder(hours_ago):
    return {"Name": (NOW - timedelta(hours=hours_ago)).strftime("%Y%m%d-%H%M%S"), "IsDir": True}


def _complete(size=32308):
    return [{"Name": n, "Size": size, "IsDir": False} for n in VOLUME_BACKUP_DUMPS] + [
        {"Name": "biglobster-eu-redis-dump.rdb", "Size": 197304, "IsDir": False}]


def test_a_recent_complete_backup_is_silent():
    folders = [_folder(40), _folder(16), {"Name": "notes", "IsDir": True},
               {"Name": "20991231-000000.txt", "IsDir": False}]
    assert volume_backup_incidents(folders=folders, newest_files=_complete(), now=NOW) == []


def test_a_stall_alerts_once_naming_the_newest_folder():
    folders = [_folder(VOLUME_BACKUP_STALE_HOURS + 30), _folder(VOLUME_BACKUP_STALE_HOURS + 6)]
    out = volume_backup_incidents(folders=folders, newest_files=_complete(), now=NOW)
    assert len(out) == 1 and out[0].kind == "volume_backup"
    assert folders[1]["Name"] in out[0].id and folders[1]["Name"] in out[0].detail
    later = volume_backup_incidents(folders=folders, newest_files=_complete(),
                                    now=NOW + timedelta(hours=7))
    assert later[0].id == out[0].id


@pytest.mark.parametrize("files", [
    _complete()[1:],                                          # one dump missing
    [dict(f, Size=20) if f["Name"] == VOLUME_BACKUP_DUMPS[2] else f for f in _complete()],
])
def test_a_missing_or_empty_dump_is_incomplete(files):
    out = volume_backup_incidents(folders=[_folder(10)], newest_files=files, now=NOW)
    assert len(out) == 1 and "incomplete" in out[0].title


def test_no_dated_folder_at_all_alerts():
    out = volume_backup_incidents(folders=[{"Name": "tmp", "IsDir": True}], now=NOW)
    assert out[0].id == "volume-backup-stale:none"


def test_rclone_refusing_is_blind_and_a_timeout_is_quiet(monkeypatch):
    def refused(path):
        raise sw.DependencyAlertBlind(7, "token expired")

    monkeypatch.setattr(sw, "_rclone_lsjson", refused)
    out = volume_backup_incidents(in_deployment=True, now=NOW)
    assert len(out) == 1 and "BLIND" in out[0].title and "exit 7" in out[0].title

    monkeypatch.setattr(sw, "_rclone_lsjson", lambda path: None)
    assert volume_backup_incidents(in_deployment=True, now=NOW) == []


def test_outside_the_deployment_rclone_never_runs(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("rclone ran outside the deployment")

    monkeypatch.setattr(sw, "_rclone_lsjson", _boom)
    assert volume_backup_incidents(in_deployment=False, now=NOW) == []


def test_a_folder_still_uploading_is_not_called_incomplete():
    partial = _complete()[:1]
    assert volume_backup_incidents(folders=[_folder(0.2)], newest_files=partial, now=NOW) == []
    late = volume_backup_incidents(folders=[_folder(2)], newest_files=partial, now=NOW)
    assert len(late) == 1 and "incomplete" in late[0].title


def test_odd_sizes_read_as_missing_not_as_a_crash():
    files = [dict(f, Size="32308") if f["Name"] == VOLUME_BACKUP_DUMPS[0] else f
             for f in _complete()]
    out = volume_backup_incidents(folders=[_folder(10)], newest_files=files, now=NOW)
    assert VOLUME_BACKUP_DUMPS[0] in out[0].detail


def _fake_rclone(tmp_path, monkeypatch, script, executable=True):
    import hermes_constants

    rclone = tmp_path / "scripts" / "bin" / "rclone"
    rclone.parent.mkdir(parents=True)
    rclone.write_text(script)
    rclone.chmod(0o755 if executable else 0o644)
    conf = tmp_path / ".config" / "rclone" / "rclone.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text("[hermesdrive]\n")
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)


def test_a_listing_that_is_not_a_list_is_quiet_not_a_false_alarm(tmp_path, monkeypatch):
    _fake_rclone(tmp_path, monkeypatch, "#!/bin/sh\necho '{\"Name\": \"x\"}'\n")
    assert sw._rclone_lsjson("hermesdrive:/VolumeBackups") is None


def test_an_rclone_that_cannot_run_is_blind_not_a_crash(tmp_path, monkeypatch):
    _fake_rclone(tmp_path, monkeypatch, "#!/bin/sh\necho '[]'\n", executable=False)
    with pytest.raises(sw.DependencyAlertBlind) as blind:
        sw._rclone_lsjson("hermesdrive:/VolumeBackups")
    assert blind.value.status == 126
