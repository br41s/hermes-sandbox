"""Fork: stage 3 step 3 — a profile-bound Telegram topic with no route is dropped.

Boot turns every ``group_topics`` topic bound to a served profile into a
``gateway.profile_routes`` entry (``tests/gateway/test_profile_topic_routes_fork.py``), and
upstream's multiplex turn runs it in that profile. The per-turn profile subprocess that used
to answer a bound topic without a route is gone, so such a message must be dropped: answering
it would run the DEFAULT profile, with our memory and keys, in a client's topic.
"""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from tests.gateway.conftest import _ensure_telegram_mock

_ensure_telegram_mock()
sys.modules.pop("plugins.platforms.telegram.adapter", None)

from plugins.platforms.telegram.adapter import TOPIC_PROFILE_KEY, TelegramAdapter  # noqa: E402
from telegram.constants import ChatType as _ChatType  # noqa: E402

CHAT = -1004224848555


def _adapter() -> TelegramAdapter:
    return TelegramAdapter(PlatformConfig(enabled=True, token="***", extra={"group_topics": [
        {"chat_id": CHAT, "topics": [
            {"name": "Grow Shop", "thread_id": 3, "profile": "grow-shop"},
            {"name": "Engineering", "thread_id": 5, "skill": "software-development"},
        ]},
    ]}))


def _message(thread_id: int) -> SimpleNamespace:
    chat = SimpleNamespace(id=CHAT, type=_ChatType.SUPERGROUP, title="BigLobster", is_forum=True,
                           full_name="BigLobster")
    return SimpleNamespace(
        chat=chat, from_user=SimpleNamespace(id=42, full_name="Brais"), text="hola",
        message_thread_id=thread_id, is_topic_message=True, message_id=1001,
        reply_to_message=None, date=None, forum_topic_created=None)


def _event(*, bound, routed=None) -> MessageEvent:
    source = SessionSource(platform=Platform.TELEGRAM, chat_id=str(CHAT), chat_type="group",
                           thread_id="3", profile=routed)
    return MessageEvent(text="hola", message_type=MessageType.TEXT, source=source,
                        metadata={TOPIC_PROFILE_KEY: bound} if bound else {})


async def _dispatch(event, monkeypatch):
    upstream = AsyncMock()
    monkeypatch.setattr(BasePlatformAdapter, "handle_message", upstream)
    await _adapter().handle_message(event)
    return upstream


# ── the binding travels on the event ──────────────────────────────────────────


def test_a_bound_topic_carries_its_profile_and_an_unbound_one_does_not():
    adapter = _adapter()
    bound = adapter._build_message_event(_message(3), MessageType.TEXT)
    unbound = adapter._build_message_event(_message(5), MessageType.TEXT)

    assert bound.metadata[TOPIC_PROFILE_KEY] == "grow-shop"
    assert TOPIC_PROFILE_KEY not in unbound.metadata
    assert unbound.auto_skill == "software-development"  # the skill binding is untouched
    assert not hasattr(bound, "auto_profile")  # the subprocess field is gone


# ── the drop ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_unrouted_bound_topic_is_dropped_never_answered_by_default(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="plugins.platforms.telegram.adapter"):
        upstream = await _dispatch(_event(bound="grow-shop"), monkeypatch)

    upstream.assert_not_awaited()
    warning = caplog.text
    assert "Dropping message in topic 3" in warning and "'grow-shop'" in warning


@pytest.mark.asyncio
async def test_a_routed_bound_topic_runs_in_its_profile(monkeypatch):
    event = _event(bound="grow-shop", routed="grow-shop")
    upstream = await _dispatch(event, monkeypatch)

    upstream.assert_awaited_once()
    assert upstream.await_args.args[-1] is event


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [None, "", "default", "Default"])
async def test_an_unbound_or_default_topic_passes_through(monkeypatch, bound):
    upstream = await _dispatch(_event(bound=bound), monkeypatch)
    upstream.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_dropped_update_is_still_accepted(monkeypatch):
    """The update is consumed, not left for redelivery: a drop is the final answer."""
    adapter = _adapter()
    accepted = []
    monkeypatch.setattr(adapter, "_accept_update", lambda: accepted.append(1))
    monkeypatch.setattr(BasePlatformAdapter, "handle_message", AsyncMock())

    await adapter.handle_message(_event(bound="grow-shop"))

    assert accepted == [1]
