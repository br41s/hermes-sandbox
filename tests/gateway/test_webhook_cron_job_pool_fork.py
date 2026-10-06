"""Fork: upstream's webhook ``cron_job`` route runs a profile-store job on that profile's
pool (``cron/fork_ext/dispatch.py::run_event_job``), behind its tick jobs, not on a
worker thread of its own. A launch-store job keeps upstream's ``to_thread``, workdir or
not: stage 3 step 5 removed the fork's lane. Upstream's own route tests are in
test_webhook_cron_trigger.py.
"""

import asyncio
import threading
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import _INSECURE_NO_AUTH, WebhookAdapter


def _adapter(job_ref: str) -> WebhookAdapter:
    routes = {"hook": {"secret": _INSECURE_NO_AUTH, "cron_job": job_ref, "prompt": "event {n}"}}
    return WebhookAdapter(PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0, "routes": routes}))


async def _post_and_drain(adapter: WebhookAdapter) -> int:
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/webhooks/hook", json={"n": 1}, headers={"X-GitHub-Delivery": "d-1"})
        status = resp.status
    await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)
    return status


@pytest.fixture
def pools():
    import cron.scheduler as sched

    sched._shutdown_parallel_pool()
    yield
    sched._shutdown_parallel_pool()


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [
    {"workdir": "/srv/site"},
    {},
    # Stage 3 step 2: the retired field no longer counts (run_job refuses the record).
    {"profile": "grow-shop"},
])
async def test_cron_job_route_in_the_launch_store_runs_on_a_worker_thread(pools, fields):
    fired = []

    def _fake_execute(job_ref, extra_prompt=None):
        fired.append((job_ref, extra_prompt, threading.current_thread().name))
        return {"claimed": True, "success": True, "error": None}

    job = {"id": "abc123", "name": "review-sweeper", **fields}
    with patch("cron.jobs.resolve_job_ref", return_value=job), \
            patch("tools.cronjob_tools.execute_job_for_event", side_effect=_fake_execute):
        assert await _post_and_drain(_adapter("review-sweeper")) == 202

    assert len(fired) == 1
    job_ref, extra_prompt, thread = fired[0]
    assert job_ref == "review-sweeper"
    assert "event 1" in extra_prompt
    assert not thread.startswith(("cron-seq", "cron-parallel")), thread


@pytest.mark.asyncio
async def test_routed_cron_job_resolves_from_the_profiles_store_and_runs_on_its_pool(pools):
    """Stage 3 step 0a. A ``/p/<profile>/`` route resolves its job under the
    profile's home override alone, with no ``use_cron_store``: the store comes from
    the home fallback in ``cron.jobs._current_cron_store``. A job in that store has
    no ``profile`` field. Since step 5 it runs on that profile's own parallel pool
    (sized 1 by boot), so it queues behind the profile's tick jobs; never on a
    worker thread of its own, which would let it overlap them.

    If the module constants were re-pointed (``CRON_DIR`` and friends), that
    fallback is skipped and the lookup lands in the DEFAULT store, quietly. This
    is the test that notices."""
    import cron.jobs as jobs
    from hermes_cli.profiles import get_profile_dir

    satellite = get_profile_dir("grow-shop")
    (satellite / "cron").mkdir(parents=True, exist_ok=True)
    with jobs.use_cron_store(satellite):
        created = jobs.create_job(prompt="sweep", schedule="every 1h", name="shop-sweeper")
    assert "profile" not in created or not created.get("profile")
    assert jobs.resolve_job_ref("shop-sweeper") is None, "the job leaked into the default store"

    fired = []

    def _fake_execute(job_ref, extra_prompt=None):
        fired.append((jobs._current_cron_store().jobs_file, jobs.resolve_job_ref(job_ref),
                      threading.current_thread().name))
        return {"claimed": True, "success": True, "error": None}

    adapter = _adapter("shop-sweeper")
    with patch("tools.cronjob_tools.execute_job_for_event", side_effect=_fake_execute):
        resp = adapter._handle_cron_trigger("event 1", adapter._routes["hook"], "hook", "push", "d-1",
                                            profile="grow-shop")
        assert resp.status == 202
        await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)

    assert len(fired) == 1
    jobs_file, resolved, thread = fired[0]
    assert jobs_file == (satellite / "cron" / "jobs.json").resolve()
    assert resolved is not None and resolved["id"] == created["id"]
    assert thread.startswith("cron-parallel"), thread
