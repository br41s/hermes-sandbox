"""A satellite's LOCAL session adapter does not make it a credentialed profile.

Since upstream v0.22.5 a served secondary's local sessions (desktop, CLI, cron's own
``session_cron``) live in a ``LocalSessionAdapter`` installed into
``runner._profile_adapters[<profile>]`` (``gateway/session_local_recovery.py``
``local_adapter_map``). The cron rule "a satellite with no adapter of its own rides the
primary's adapters for its routed targets" tested the map for emptiness, so ``{local: ...}``
read as "has its own bot": the satellite got its own map, no Telegram, and every delivery
failed ``platform 'telegram' not configured/enabled`` (biglobster and auditor, 2026-10-23 on).

These tests drive the real rule through both entry points (the multiplex ticker and the
webhook/Run-now path) and the real ``_deliver_result``.
"""
import asyncio
import threading
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import hermes_yaml as yaml

from cron.fork_ext.dispatch import live_adapters_for
from cron.scheduler import _deliver_result
from cron.scheduler_preflight import SharedRouteAdapters
from cron.scheduler_provider import InProcessCronScheduler
from gateway.config import Platform, PlatformConfig
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

CHAT, THREAD = "-1004224848555", "2"
PRIMARY_YAML = {"gateway": {"multiplex_profiles": True, "profile_routes": [
    {"name": "fork-topic:biglobster:2", "platform": "telegram", "chat_id": CHAT,
     "thread_id": THREAD, "profile": "biglobster"},
]}}


def _homes(tmp_path, monkeypatch):
    root = tmp_path / "root"
    home = root / "profiles" / "biglobster"
    home.mkdir(parents=True)
    (root / "config.yaml").write_text(yaml.safe_dump(PRIMARY_YAML), encoding="utf-8")
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: root)
    return root, home


def _primary_adapter():
    adapter = MagicMock()
    adapter.sent = []

    async def send(chat_id, content, metadata=None):
        adapter.sent.append((chat_id, (metadata or {}).get("thread_id")))
        return {"success": True, "message_id": "m1"}

    adapter.send = send
    return adapter


def _runner(primary, own):
    return SimpleNamespace(adapters={Platform.TELEGRAM: primary},
                           _profile_adapters={"biglobster": own}, _primary_profile_name="default")


def _deliver(adapters):
    """``_deliver_result`` with a live loop, under the satellite's own config: its Telegram
    block is disabled (no credential in its scope), exactly as on the pod."""
    loop = MagicMock()
    loop.is_running.return_value = True

    def fake_run_coro(coro, _loop):
        future = Future()
        future.set_result(asyncio.run(coro))
        return future

    async def _no_standalone(platform, pconfig, chat_id, text, **kwargs):
        return {"success": False, "error": "TELEGRAM_BOT_TOKEN is not set"}

    config = MagicMock()
    config.platforms = {Platform.TELEGRAM: PlatformConfig(enabled=False)}
    config.get_home_channel = lambda p: None
    job = {"id": "ce583d11dedd", "name": "Gap Hunter", "deliver": f"telegram:{CHAT}:{THREAD}"}
    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch("tools.send_message_tool._send_to_platform", _no_standalone), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro):
        return _deliver_result(job, "hello", adapters=adapters, loop=loop)


def test_webhook_path_satellite_with_only_a_local_adapter_rides_the_primary(tmp_path, monkeypatch):
    _, home = _homes(tmp_path, monkeypatch)
    primary, local = _primary_adapter(), object()
    token = set_hermes_home_override(str(home))
    try:
        adapters = live_adapters_for(_runner(primary, {Platform.LOCAL: local}), "biglobster")
        assert isinstance(adapters, SharedRouteAdapters)
        assert adapters.get(Platform.LOCAL) is local      # its own local sessions stay its own
        assert _deliver(adapters) is None
        assert primary.sent == [(CHAT, THREAD)]
    finally:
        reset_hermes_home_override(token)


def test_ticker_hands_a_local_only_satellite_the_route_grant(tmp_path, monkeypatch):
    _, home = _homes(tmp_path, monkeypatch)
    primary, local = _primary_adapter(), object()
    stop, seen = threading.Event(), []

    def fake_tick(*, adapters=None, **kwargs):
        seen.append(adapters)
        stop.set()
        return 0

    with patch("cron.scheduler.tick", fake_tick):
        InProcessCronScheduler()._start_multiplex(
            stop, profile_homes=[("biglobster", home)], adapters={Platform.TELEGRAM: primary},
            profile_adapters={"biglobster": {Platform.LOCAL: local}}, default_profile="default",
            interval=1)
    assert len(seen) == 1 and isinstance(seen[0], SharedRouteAdapters)
    token = set_hermes_home_override(str(home))
    try:
        assert _deliver(seen[0]) is None
        assert primary.sent == [(CHAT, THREAD)]
    finally:
        reset_hermes_home_override(token)


def test_a_satellite_with_its_own_messaging_adapter_keeps_its_own_map(tmp_path, monkeypatch):
    _, home = _homes(tmp_path, monkeypatch)
    own = {Platform.TELEGRAM: object(), Platform.LOCAL: object()}
    token = set_hermes_home_override(str(home))
    try:
        assert live_adapters_for(_runner(_primary_adapter(), own), "biglobster") is own
    finally:
        reset_hermes_home_override(token)


def test_unrouted_targets_still_fail_closed_with_a_local_adapter(tmp_path, monkeypatch):
    _, home = _homes(tmp_path, monkeypatch)
    primary = _primary_adapter()
    token = set_hermes_home_override(str(home))
    try:
        adapters = live_adapters_for(_runner(primary, {Platform.LOCAL: object()}), "biglobster")
        assert adapters.get(Platform.TELEGRAM, {"chat_id": CHAT, "thread_id": "1904"}) is None
        assert adapters.get(Platform.TELEGRAM) is None     # target-less: never the default bot
    finally:
        reset_hermes_home_override(token)
