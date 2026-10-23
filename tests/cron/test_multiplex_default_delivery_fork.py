"""Fork: a default-store cron job reaches Telegram under multiplex, end to end.

The 2026-09-28 incident's cron half: with multiplex on, default-profile jobs failed
``platform 'telegram' not configured/enabled``. ``TELEGRAM_BOT_TOKEN`` lives only in
the Zeabur container env, and the launch profile's secret scope was built from
``/opt/data/.env`` alone, so ``load_gateway_config()`` under that scope saw the
config.yaml ``telegram:`` block with no token and marked it disabled. A disabled
native config vetoes even a live adapter (``gateway.delivery.resolve_delivery_transport``).

``tests/agent/test_process_env_scope_fork.py`` pins the scope and the config load.
These drive the rest of the path the way the multiplex ticker does: the real
``_profile_cron_scope`` (``cron/scheduler_provider.py``) around the real
``run_one_job``, which installs the launch scope itself (``_run_one_job_body``) and
delivers through the real ``_deliver_result`` to the live adapter the ticker hands a
default-profile tick. Only the agent run, the output file and the job-store mark are
stubbed.

Since upstream v0.22 a run does not send inline: ``_save_compose_deliver`` enqueues the
notice in the profile's ``cron/deliveries.db`` and the gateway's housekeeping drains it
through the live adapters under that profile's runtime scope
(``gateway/run.py`` ``_drain_restart_safe_cron_deliveries``). The drain is driven here the
same way, and a failed send is read from the queue row, where production records it.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import pytest

import cron.scheduler as sched
import hermes_constants
from agent import secret_scope as ss
from cron.scheduler_provider import _profile_cron_scope
from gateway.config import Platform
from hermes_cli.fork_ext import process_env_scope as pes
from tui_gateway.launch_profile_policy import capture_launch_env

CHAT, THREAD = "-1004224848555", "1904"
TOKEN = "123:container-only"


@pytest.fixture(autouse=True)
def _reset_multiplex(monkeypatch):
    monkeypatch.setattr(hermes_constants, "_PINNED_PROCESS_HERMES_HOME", None, raising=False)
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


@pytest.fixture
def launch_home(tmp_path, monkeypatch):
    """Production's shape: a ``telegram:`` block in config.yaml, no token in ``.env``."""
    home = tmp_path / "data"
    home.mkdir()
    (home / "config.yaml").write_text(
        "telegram:\n  extra:\n    group_topics: []\n", encoding="utf-8")
    (home / ".env").write_text("OPENROUTER_API_KEY=from-dotenv\n", encoding="utf-8")
    biglobster = home / "profiles" / "biglobster"
    biglobster.mkdir(parents=True)
    (biglobster / ".env").write_text("OPENROUTER_API_KEY=biglobster-key\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv(pes.FLAG, "1")
    return home


class _LiveTelegram:
    """The gateway's connected adapter: records each send and the scope it ran under."""

    def __init__(self):
        self.sent = []

    async def send(self, chat_id, content, metadata=None):
        self.sent.append({"chat_id": chat_id, "content": content, "metadata": metadata or {},
                          "scope_home": ss.current_secret_scope_home()})
        return {"success": True, "message_id": "m1"}


def _fire_under_multiplex(home, job, adapter, *, during_run=None):
    """Tick ``job`` as the multiplex ticker does for the default profile, then drain the
    launch profile's delivery queue as the gateway's housekeeping does.

    Returns ``(marks, standalone)``: every ``mark_job_run`` call with its
    ``delivery_error`` (the queue row's error once drained), and every chat the
    standalone fallback was asked to send to.
    """
    from cron import delivery_queue
    from gateway.run import _profile_runtime_scope

    marks, standalone = [], []

    def fake_run_job_impl(job, **_kwargs):
        if during_run is not None:
            during_run()
        return True, "out", "the report", None

    def fake_mark(job_id, success, error=None, delivery_error=None, **_kwargs):
        marks.append({"job_id": job_id, "success": success, "delivery_error": delivery_error})
        return True

    async def fake_standalone(platform, pconfig, chat_id, text, **_kwargs):
        standalone.append(chat_id)
        return {"success": False, "error": "standalone must not be needed"}

    def fake_run_coro(coro, _loop):
        future = Future()
        future.set_result(asyncio.run(coro))
        return future

    loop = MagicMock()
    loop.is_running.return_value = True

    # GatewayRunner.__init__ does both when multiplex_profiles is on.
    ss.set_multiplex_active(True)
    capture_launch_env()
    with patch.object(sched, "_run_job_impl", fake_run_job_impl), \
         patch.object(sched, "save_job_output", lambda *_a: str(home / "out.txt")), \
         patch.object(sched, "mark_job_run", fake_mark), \
         patch("tools.send_message_tool._send_to_platform", fake_standalone), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro), \
         _profile_cron_scope(home):
        # tick_adapters_for(default_profile) hands the tick the live adapter map.
        sched.run_one_job(job, adapters={Platform.TELEGRAM: adapter}, loop=loop)
    with patch("tools.send_message_tool._send_to_platform", fake_standalone), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro), \
         _profile_runtime_scope(home):
        sched.drain_delivery_queue({Platform.TELEGRAM: adapter}, loop)
        for mark in marks:
            row = delivery_queue.get_status(job.get("execution_id") or "")
            if row is not None and mark["delivery_error"] is None:
                mark["delivery_error"] = row.get("error")
    return marks, standalone


def _job(**extra):
    return {"id": "a1b2c3d4e5f6", "name": "brief", "deliver": f"telegram:{CHAT}:{THREAD}",
            "progress_ping": False, **extra}


def test_default_profile_job_delivers_to_the_live_adapter(launch_home):
    adapter = _LiveTelegram()
    marks, standalone = _fire_under_multiplex(launch_home, _job(), adapter)

    assert marks == [{"job_id": "a1b2c3d4e5f6", "success": True, "delivery_error": None}]
    assert [s["chat_id"] for s in adapter.sent] == [CHAT]
    assert str(adapter.sent[0]["metadata"].get("thread_id")) == THREAD
    assert adapter.sent[0]["scope_home"] == str(launch_home)
    assert standalone == []


def test_without_the_flag_the_queued_drain_still_reaches_the_live_adapter(launch_home, monkeypatch):
    """Upstream's .env-only launch scope no longer breaks cron delivery.

    On 2026-09-28 this path failed ``platform 'telegram' not configured/enabled``: the
    inline send loaded the gateway config under a scope without the token, and the
    disabled ``telegram:`` block vetoed the live adapter. Since upstream v0.22 the notice
    goes through the delivery queue, and its drain hands the row to the live adapter
    whatever the scope's token says, so the cron half of the incident cannot recur this
    way. The fork flag is still what keeps the Telegram allowlists readable
    (tests/agent/test_process_env_scope_fork.py); this pins that delivery no longer
    depends on it, so a future change that makes it depend again shows up here.
    """
    monkeypatch.delenv(pes.FLAG)
    adapter = _LiveTelegram()
    marks, standalone = _fire_under_multiplex(launch_home, _job(), adapter)

    assert marks == [{"job_id": "a1b2c3d4e5f6", "success": True, "delivery_error": None}]
    assert [s["chat_id"] for s in adapter.sent] == [CHAT]
    assert standalone == []


def test_a_legacy_profile_record_fails_instead_of_running_under_the_launch_scope(launch_home):
    """Stage 3 step 2: a default-store record still carrying ``profile`` is refused in
    ``run_job``, under the real ``run_one_job`` path, and the run is marked failed. It
    never runs under the launch identity, and its report never goes out."""
    ran = []
    adapter = _LiveTelegram()
    marks, standalone = _fire_under_multiplex(
        launch_home, _job(profile="biglobster"), adapter, during_run=lambda: ran.append(1))

    assert ran == []
    assert len(marks) == 1 and marks[0]["success"] is False
    assert not any("the report" in s["content"] for s in adapter.sent)
    assert standalone == []
