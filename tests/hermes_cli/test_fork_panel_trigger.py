"""The panel's "Run now" marks the job due and returns; the gateway's ticker runs it.

Upstream's trigger ran the job synchronously inside the dashboard request, so a
20-minute Gap Hunter run outlived Zeabur's proxy timeout and the panel never
showed its confirmation (2026-10-06). See ``hermes_cli/fork_ext/web.py``.
"""

import pytest
from fastapi import HTTPException

import hermes_cli.web_server_cron as _web_server_cron
from hermes_cli import web_server
from hermes_cli.fork_ext import web as fork_web


@pytest.fixture()
def isolated_profiles(tmp_path, monkeypatch):
    """Same isolated default home + one named profile as test_web_server_cron_profiles."""
    from hermes_cli import profiles

    default_home = tmp_path / ".hermes"
    profiles_root = default_home / "profiles"
    worker_home = profiles_root / "worker_alpha"
    for home in (default_home, worker_home):
        (home / "cron").mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("model: test-model\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: default_home)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: profiles_root)
    return {"default": default_home, "worker_alpha": worker_home}


def _create(name):
    return _web_server_cron._call_cron_for_profile(
        "worker_alpha", "create_job", prompt="p", schedule="every 1h", name=name)


def _get(job_id):
    return _web_server_cron._call_cron_for_profile("worker_alpha", "get_job", job_id)


def _never_fire_in_the_dashboard(monkeypatch):
    from cron.scheduler_provider import InProcessCronScheduler

    def _boom(self, *a, **kw):
        raise AssertionError("the dashboard must not run the job; the gateway ticker does")

    monkeypatch.setattr(InProcessCronScheduler, "fire_due", _boom)


@pytest.mark.asyncio
async def test_run_now_marks_only_the_selected_job_due_and_returns(isolated_profiles, monkeypatch):
    _never_fire_in_the_dashboard(monkeypatch)
    job, sibling = _create("selected"), _create("sibling")

    result = await fork_web.trigger_cron_job(job["id"], profile="worker_alpha")

    assert result["id"] == job["id"]
    stored = _get(job["id"])
    assert stored["manual_run_at"] is not None
    assert stored["next_run_at"] == stored["manual_run_at"]
    assert stored["last_run_at"] is None  # queued, not run
    assert _get(sibling["id"]).get("manual_run_at") is None


@pytest.mark.asyncio
async def test_run_now_refuses_a_job_already_in_flight(isolated_profiles, monkeypatch):
    from cron import executions

    _never_fire_in_the_dashboard(monkeypatch)
    job = _create("busy")
    monkeypatch.setattr(
        executions, "EXECUTIONS_FILE", isolated_profiles["worker_alpha"] / "cron" / "executions.db")
    executions.create_execution(job["id"], source="builtin")

    with pytest.raises(HTTPException) as exc:
        await fork_web.trigger_cron_job(job["id"], profile="worker_alpha")

    assert exc.value.status_code == 409
    assert _get(job["id"]).get("manual_run_at") is None  # not queued behind the live run


@pytest.mark.asyncio
async def test_external_provider_keeps_upstreams_fire(isolated_profiles, monkeypatch):
    job = _create("external")
    fired = []

    class ExternalProvider:
        name = "external"

        def fire_due(self, job_id, *, adapters=None, loop=None, force=False, manual=False):
            fired.append(job_id)
            return True

    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler", lambda: ExternalProvider())

    await fork_web.trigger_cron_job(job["id"], profile="worker_alpha")

    assert fired == [job["id"]]


def test_app_serves_the_fork_trigger_not_upstreams():
    first = next(
        r for r in web_server.app.routes
        if getattr(r, "path", "") == "/api/cron/jobs/{job_id}/trigger"
        and "POST" in (getattr(r, "methods", None) or ()))
    assert first.endpoint is fork_web.trigger_cron_job
