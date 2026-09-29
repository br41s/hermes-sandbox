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
stubbed; the mark is where the delivery error lands, as it does in production.
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
        self.sent.append({"chat_id": chat_id, "metadata": metadata or {},
                          "scope_home": ss.current_secret_scope_home()})
        return {"success": True, "message_id": "m1"}


def _fire_under_multiplex(home, job, adapter, *, during_run=None):
    """Tick ``job`` as the multiplex ticker does for the default profile.

    Returns ``(marks, standalone)``: every ``mark_job_run`` call with its
    ``delivery_error``, and every chat the standalone fallback was asked to send to.
    """
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


def test_without_the_flag_it_is_the_incident(launch_home, monkeypatch):
    """Upstream's .env-only launch scope: the error production logged on 2026-09-28."""
    monkeypatch.delenv(pes.FLAG)
    adapter = _LiveTelegram()
    marks, standalone = _fire_under_multiplex(launch_home, _job(), adapter)

    assert len(marks) == 1
    assert marks[0]["delivery_error"] == "platform 'telegram' not configured/enabled"
    assert adapter.sent == [] and standalone == []


def test_a_fork_profile_job_delivers_under_the_launch_scope(launch_home):
    """A default-store job with ``profile: biglobster`` runs under that profile's scope
    (``_job_profile_context``, ``{**os.environ, **profile .env}``), but delivers after
    that context has closed, under the launch scope ``_run_one_job_body`` installed."""
    seen = {}

    def during_run():
        seen["home"] = ss.current_secret_scope_home()
        seen["key"] = ss.get_secret("OPENROUTER_API_KEY")

    adapter = _LiveTelegram()
    marks, standalone = _fire_under_multiplex(
        launch_home, _job(profile="biglobster"), adapter, during_run=during_run)

    # The run really was the profile's: its own key, from a scope with no stamped home.
    assert seen == {"home": None, "key": "biglobster-key"}
    assert marks == [{"job_id": "a1b2c3d4e5f6", "success": True, "delivery_error": None}]
    assert [s["chat_id"] for s in adapter.sent] == [CHAT]
    assert adapter.sent[0]["scope_home"] == str(launch_home)
    assert standalone == []
