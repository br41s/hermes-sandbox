"""Fork: upstream's webhook ``cron_job`` route runs a profile/workdir job on the
sequential lane (``cron/fork_ext/dispatch.py::run_event_job``), not on a worker
thread of its own. Upstream's own route tests are in test_webhook_cron_trigger.py.
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
def lane():
    from cron.fork_ext import dispatch

    dispatch.shutdown_sequential_executor()
    yield
    dispatch.shutdown_sequential_executor()


@pytest.mark.asyncio
@pytest.mark.parametrize("fields, on_lane", [
    ({"profile": "grow-shop"}, True),
    ({"workdir": "/srv/site"}, True),
    ({}, False),
])
async def test_cron_job_route_runs_profile_and_workdir_jobs_on_the_lane(lane, fields, on_lane):
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
    assert thread.startswith("cron-seq") is on_lane, thread
