"""Outcome tests for the batch prompt-sync sweep (scripts/sync_prompt_drift.py).

Two failures this pins down, both seen in production on 2026-09-14 when the
sweep pushed four drifted prompts:

1. It wrote ``prompt`` without ``prompt_synced_sha``, so the baseline stayed at
   whatever the *previous* sync recorded. The next ``cronjob_tools.sync_prompt``
   then read this script's own write as a hand-edit and refused.
2. It called ``update_job`` directly, bypassing the clobber guard — meaning the
   one tool built for unattended fan-out was the one that would silently destroy
   an emergency fix applied to a live job.
"""

import hashlib

import pytest

from scripts import sync_prompt_drift as sync

SOURCE = "infographic/infographic-engineer.prompt"
REPO_TEXT = "repo version of the prompt\n"
LIVE_TEXT = "older live version of the prompt\n"


def _sha(text):
    return hashlib.sha256(text.strip().encode()).hexdigest()


def _job(jid="j1", *, prompt=LIVE_TEXT, synced_sha=..., name="Infographic Engineer"):
    job = {"id": jid, "name": name, "prompt": prompt, "prompt_source": SOURCE}
    job["prompt_synced_sha"] = _sha(LIVE_TEXT) if synced_sha is ... else synced_sha
    return job


@pytest.fixture
def repo_file(monkeypatch):
    monkeypatch.setattr(sync, "_read_source", lambda source: REPO_TEXT)
    monkeypatch.setattr(sync, "_scan_cron_prompt", lambda text: None)


def _with_jobs(monkeypatch, *jobs):
    monkeypatch.setattr(sync, "list_jobs", lambda include_disabled=False: list(jobs))


def test_live_matching_its_baseline_syncs(monkeypatch, repo_file):
    _with_jobs(monkeypatch, _job())
    drift = sync.find_drift(None)
    assert [j["id"] for j, *_ in drift["changed"]] == ["j1"]
    assert drift["blocked"] == []


def test_live_edited_since_last_sync_is_blocked_not_overwritten(monkeypatch, repo_file):
    """The emergency path: someone hot-patched the live job. Do not clobber it."""
    _with_jobs(monkeypatch, _job(prompt="hand-patched in production\n"))
    drift = sync.find_drift(None)
    assert drift["changed"] == []
    (job, source, new_text, detail), = drift["blocked"]
    assert job["id"] == "j1"
    assert new_text is None
    assert "edited since the last sync" in detail
    assert "--force" in detail


def test_force_overrides_the_guard(monkeypatch, repo_file):
    _with_jobs(monkeypatch, _job(prompt="hand-patched in production\n"))
    drift = sync.find_drift(None, force=True)
    assert [j["id"] for j, *_ in drift["changed"]] == ["j1"]
    assert drift["blocked"] == []


def test_job_with_no_baseline_syncs_but_is_flagged(monkeypatch, repo_file):
    """Never synced, so nothing to judge the live side against — say so."""
    _with_jobs(monkeypatch, _job(synced_sha=None))
    (job, source, new_text, detail), = sync.find_drift(None)["changed"]
    assert new_text == REPO_TEXT
    assert "first sync" in detail


def test_up_to_date_job_is_untouched(monkeypatch, repo_file):
    _with_jobs(monkeypatch, _job(prompt=REPO_TEXT, synced_sha="stale-and-irrelevant"))
    drift = sync.find_drift(None)
    assert [j["id"] for j, *_ in drift["unchanged"]] == ["j1"]
    assert drift["changed"] == [] and drift["blocked"] == []


def test_blocked_job_does_not_stop_the_rest_of_the_sweep(monkeypatch, repo_file):
    _with_jobs(
        monkeypatch,
        _job("blocked1", prompt="hand-patched in production\n"),
        _job("ok1"),
    )
    drift = sync.find_drift(None)
    assert [j["id"] for j, *_ in drift["blocked"]] == ["blocked1"]
    assert [j["id"] for j, *_ in drift["changed"]] == ["ok1"]


def test_sync_records_the_baseline_it_just_wrote(monkeypatch, repo_file, capsys):
    """The 2026-09-14 bug: prompt advanced, prompt_synced_sha did not."""
    _with_jobs(monkeypatch, _job())
    writes = {}
    monkeypatch.setattr(sync, "update_job", lambda jid, updates: writes.update({jid: updates}))
    monkeypatch.setattr(sync.sys, "argv", ["sync_prompt_drift.py", "--yes"])

    assert sync.main() == 0

    assert writes["j1"]["prompt"] == REPO_TEXT
    assert writes["j1"]["prompt_synced_sha"] == _sha(REPO_TEXT), (
        "baseline must advance with the prompt, or the next sync_prompt refuses"
    )


def test_blocked_sweep_exits_nonzero(monkeypatch, repo_file):
    _with_jobs(monkeypatch, _job(prompt="hand-patched in production\n"))
    monkeypatch.setattr(sync, "update_job", lambda jid, updates: None)
    monkeypatch.setattr(sync.sys, "argv", ["sync_prompt_drift.py", "--yes"])
    assert sync.main() == 1
