"""Regression lock for the Hermes volume disk-space signal.

By 2026-10-21 the HERMES_HOME volume held 93 GB of 118 — leftover Gap Hunter
clones and local backup zips — and nothing said so: ``gateway/disk_status.py``
grades the volume for ``/api/status`` only, and only near write failure. These
tests pin the watcher's earlier threshold and its once-a-day brief.

Hermetic: injected usage and clock; the real disk is never read.
"""
from datetime import datetime, timedelta, timezone

import incidents.sweep as sw
from incidents.sweep import DISK_ALERT_PERCENT, DISK_MIN_FREE_GB, disk_space_incidents

NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
GB = 1024 ** 3


def test_roomy_volume_is_silent():
    assert disk_space_incidents(usage=(100 * GB, 40 * GB), now=NOW) == []


def test_high_usage_alerts_once_a_day():
    used = DISK_ALERT_PERCENT + 1
    out = disk_space_incidents(usage=(200 * GB, int(200 * GB * (100 - used) / 100)), now=NOW)
    assert len(out) == 1
    assert out[0].kind == "disk_space"
    assert "86%" in out[0].title
    later = disk_space_incidents(usage=(200 * GB, 20 * GB), now=NOW + timedelta(hours=5))
    assert later[0].id == out[0].id
    tomorrow = disk_space_incidents(usage=(200 * GB, 20 * GB), now=NOW + timedelta(days=1))
    assert tomorrow[0].id != out[0].id


def test_low_free_space_alerts_even_on_a_low_percent():
    # 70% used of 30 GB leaves 9 GB: not enough for two nightly zips.
    out = disk_space_incidents(usage=(30 * GB, 9 * GB), now=NOW)
    assert len(out) == 1
    assert f"{DISK_MIN_FREE_GB} GB" in out[0].detail


def test_outside_the_deployment_the_disk_is_not_read(monkeypatch):
    def boom(_):
        raise AssertionError("must not read the disk outside the deployment")

    monkeypatch.setattr(sw.shutil, "disk_usage", boom)
    assert disk_space_incidents(in_deployment=False, now=NOW) == []


def test_sweep_delivers_the_disk_brief(tmp_path):
    brief = disk_space_incidents(usage=(100 * GB, 5 * GB), now=NOW)
    text = sw.sweep(now=NOW, jobs=[], langfuse=[], blocked=[], checkout_drift=[],
                    deploy_drift=[], judge_liveness=[], dependency_alerts=[],
                    unclean_exits=[], laptop_backup=[], site_down=[], volume_backup=[],
                    openrouter_budget=[], disk_space=brief,
                    state_path=tmp_path / "state.json", dry_run=True,
                    ledger_path=tmp_path / "ledger.jsonl", modes_path=tmp_path / "modes.json")
    assert "Hermes volume 95% full" in text
