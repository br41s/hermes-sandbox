"""Fork: a webhook-triggered cron run delivers through the live gateway, like a tick run.

The 2026-10-15 incident: ``auditor-review`` (profile auditor, own store since
2026-10-04) failed ``platform 'telegram' not configured/enabled`` on every run with
something to say. The ``trigger_cron_job_id`` webhook queued it with no adapters, so
delivery took the standalone path under the auditor's secret scope, which holds no
Telegram token (``TELEGRAM_BOT_TOKEN`` lives only in the launch env, and a satellite
holding its own is a fatal duplicate credential). The multiplex ticker's runs of the
same job delivered, because it hands a credentialless satellite ``SharedRouteAdapters``
over the primary's adapters. ``live_adapters_for`` applies that rule to the webhook.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import pytest
import yaml

from cron.fork_ext.dispatch import live_adapters_for
from cron.scheduler_preflight import SharedRouteAdapters
from gateway.config import Platform
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

CHAT = "-1004224848555"
PRIMARY_YAML = {"gateway": {"multiplex_profiles": True, "profile_routes": [
    {"name": "fork-topic:auditor:1904", "platform": "telegram", "chat_id": CHAT,
     "thread_id": "1904", "profile": "auditor"},
    {"name": "fork-topic:biglobster:2", "platform": "telegram", "chat_id": CHAT,
     "thread_id": "2", "profile": "biglobster"},
]}}


def _runner(adapters, profile_adapters=None):
    return MagicMock(adapters=adapters, _profile_adapters=profile_adapters or {},
                     _primary_profile_name="default")


def _primary_adapter():
    adapter = MagicMock()
    adapter.sent = []

    async def send(chat_id, content, metadata=None, **_kw):
        adapter.sent.append((chat_id, (metadata or {}).get("thread_id")))
        return {"success": True, "message_id": "m1"}

    adapter.send = send
    return adapter


@pytest.fixture
def auditor_scope(tmp_path, monkeypatch):
    """The auditor profile's home is active; the primary config routes topic 1904 to it."""
    root = tmp_path / "data"
    home = root / "profiles" / "auditor"
    home.mkdir(parents=True)
    (root / "config.yaml").write_text(yaml.safe_dump(PRIMARY_YAML), encoding="utf-8")
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: root)
    token = set_hermes_home_override(str(home))
    try:
        yield home
    finally:
        reset_hermes_home_override(token)


# ── live_adapters_for: the ticker's rule ──────────────────────────────────────


def test_no_runner_means_no_live_adapters():
    assert live_adapters_for(None, "auditor") is None


@pytest.mark.parametrize("profile", [None, "default"])
def test_launch_profile_uses_the_runners_adapters(profile):
    adapters = {Platform.TELEGRAM: object()}
    assert live_adapters_for(_runner(adapters), profile) is adapters


def test_secondary_with_its_own_adapters_uses_them():
    own = {Platform.TELEGRAM: object()}
    runner = _runner({Platform.TELEGRAM: object()}, {"grow-shop": own})
    assert live_adapters_for(runner, "grow-shop") is own


def test_credentialless_secondary_rides_the_primary_only_on_its_route(auditor_scope):
    primary = object()
    shared = live_adapters_for(_runner({Platform.TELEGRAM: primary}), "auditor")
    assert isinstance(shared, SharedRouteAdapters)
    assert shared.get(Platform.TELEGRAM, {"chat_id": CHAT, "thread_id": "1904"}) is primary
    # Another profile's topic, General, or no target: never the default bot.
    assert shared.get(Platform.TELEGRAM, {"chat_id": CHAT, "thread_id": "2"}) is None
    assert shared.get(Platform.TELEGRAM, {"chat_id": CHAT}) is None
    assert shared.get(Platform.TELEGRAM) is None


# ── the incident: the auditor's Telegram output reaches topic 1904 ────────────


def test_auditor_webhook_run_delivers_to_its_topic(auditor_scope):
    from cron.scheduler import _deliver_result

    primary = _primary_adapter()
    adapters = live_adapters_for(_runner({Platform.TELEGRAM: primary}), "auditor")
    job = {"id": "c19bb95c0a62", "name": "auditor-review", "deliver": f"telegram:{CHAT}:1904"}

    loop = MagicMock()
    loop.is_running.return_value = True

    def fake_run_coro(coro, _loop):
        future = Future()
        future.set_result(asyncio.run(coro))
        return future

    # The satellite's own gateway config: no Telegram block, no token.
    config = MagicMock()
    config.platforms = {}
    config.get_home_channel = lambda p: None
    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro):
        error = _deliver_result(job, "PR #460 approved", adapters=adapters, loop=loop)

    assert error is None, error
    assert primary.sent == [(CHAT, "1904")]


def test_without_live_adapters_the_satellite_cannot_deliver(auditor_scope):
    """The pre-fix path, pinned so the test above cannot pass for the wrong reason."""
    from cron.scheduler import _deliver_result

    config = MagicMock()
    config.platforms = {}
    config.get_home_channel = lambda p: None
    job = {"id": "c19bb95c0a62", "name": "auditor-review", "deliver": f"telegram:{CHAT}:1904"}
    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}):
        error = _deliver_result(job, "PR #460 approved", adapters=None, loop=None)

    assert error == "platform 'telegram' not configured/enabled"


# ── the handoff: webhook → dispatch_job_async → run_one_job ───────────────────


class _InlinePool:
    def submit(self, fn):
        fn()
        fut = concurrent.futures.Future()
        fut.set_result(True)
        return fut


def test_dispatch_forwards_adapters_and_loop_to_the_run(monkeypatch):
    import cron.scheduler as sched

    seen = {}
    monkeypatch.setattr(sched, "run_one_job", lambda job, **kw: seen.update(kw))
    monkeypatch.setattr(sched, "_get_parallel_pool", lambda mw: _InlinePool())
    adapters, loop = {Platform.TELEGRAM: object()}, object()

    assert sched.dispatch_job_async({"id": "fwd1"}, adapters=adapters, loop=loop) == {
        "queued": True, "reason": None}
    assert seen == {"adapters": adapters, "loop": loop}


@pytest.mark.asyncio
async def test_trigger_route_hands_the_runners_adapters_and_loop_to_dispatch():
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from gateway.config import PlatformConfig
    from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH

    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={
        "host": "127.0.0.1", "port": 0, "routes": {"auditor-pr-trigger": {
            "secret": _INSECURE_NO_AUTH, "events": ["pull_request"],
            "trigger_cron_job_id": "c19bb95c0a62"}}}))
    runner_adapters = {Platform.TELEGRAM: object()}
    adapter.gateway_runner = _runner(runner_adapters)
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)

    with patch("cron.jobs.get_job", MagicMock(return_value={"id": "c19bb95c0a62"})), \
         patch("cron.scheduler.dispatch_job_async",
               MagicMock(return_value={"queued": True, "reason": None})) as dispatch:
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/webhooks/auditor-pr-trigger", json={"action": "opened"},
                                  headers={"X-GitHub-Event": "pull_request",
                                           "X-GitHub-Delivery": "d-live"})
            assert resp.status == 200
            running_loop = asyncio.get_running_loop()

    kwargs = dispatch.call_args.kwargs
    assert kwargs["adapters"] is runner_adapters
    assert kwargs["loop"] is running_loop
