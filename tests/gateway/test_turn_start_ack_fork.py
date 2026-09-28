"""Fork: the turn-start ack ("message received") is sent once when a fresh turn starts.

The method lived in the gateway since June but its only call site sat in the part of
``gateway/run.py`` upstream split out at v2026.9.x, so it went unwired and never fired.
The call now sits in ``gateway/run_inbound.py`` right after the session claim; these pin
that it is reached, and what the method itself sends or skips.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.event import MessageEvent
from gateway.config import Platform
from tests.gateway.test_telegram_topic_mode import _make_event, _make_group_event, _make_runner


async def _drain() -> None:
    # The ack is fire-and-forget; give the event loop a turn to run it.
    for _ in range(3):
        await asyncio.sleep(0)


def _live_turn_runner(monkeypatch):
    """A runner whose fresh turn goes through the real _handle_message and the real ack method."""
    import gateway.run as gateway_run

    runner, adapter = _ack_runner(monkeypatch)
    runner._handle_message_with_agent = AsyncMock(return_value="agent response")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    return runner, adapter


@pytest.mark.asyncio
async def test_fresh_turn_delivers_the_ack_once(monkeypatch):
    runner, adapter = _live_turn_runner(monkeypatch)
    event = _make_group_event("hello", thread_id="555")

    assert await runner._handle_message(event) == "agent response"
    await _drain()

    adapter._send_with_retry.assert_awaited_once()
    kwargs = adapter._send_with_retry.await_args.kwargs
    assert kwargs["chat_id"] == event.source.chat_id
    assert kwargs["content"] == "✅ recibido"


@pytest.mark.asyncio
async def test_a_slow_ack_is_retained_until_it_finishes(monkeypatch):
    """The loop keeps only a weak reference to a task: an ack still sending when the handler
    returns must stay registered on the runner, or it can be collected mid-send."""
    runner, adapter = _live_turn_runner(monkeypatch)
    release = asyncio.Event()

    async def _slow_send(**_kwargs):
        await release.wait()

    adapter._send_with_retry = AsyncMock(side_effect=_slow_send)

    await runner._handle_message(_make_group_event("hello", thread_id="555"))
    await _drain()
    pending = [t for t in runner._background_tasks if not t.done()]
    assert len(pending) == 1, runner._background_tasks

    release.set()
    await _drain()
    assert pending[0].done() and pending[0] not in runner._background_tasks
    adapter._send_with_retry.assert_awaited_once()


def _ack_runner(monkeypatch, *, enabled=True, text="✅ recibido"):
    import gateway.run as gateway_run

    runner = _make_runner()
    adapter = runner.adapters[Platform.TELEGRAM]
    adapter._send_with_retry = AsyncMock()
    display = {"display": {"turn_start_ack": enabled, "turn_start_ack_text": text}}
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: display)
    return runner, adapter


@pytest.mark.asyncio
async def test_ack_sends_the_configured_text(monkeypatch):
    runner, adapter = _ack_runner(monkeypatch)
    event = _make_event("hola")

    await runner._maybe_send_turn_start_ack(event, event.source)

    adapter._send_with_retry.assert_awaited_once()
    kwargs = adapter._send_with_retry.await_args.kwargs
    assert kwargs["chat_id"] == event.source.chat_id
    assert kwargs["content"] == "✅ recibido"
    assert kwargs["reply_to"] == "m1"  # a non-topic chat quotes the user's message


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["internal", "goal-continuation", "disabled", "empty-text"])
async def test_ack_is_skipped(monkeypatch, case):
    runner, adapter = _ack_runner(
        monkeypatch, enabled=case != "disabled", text="" if case == "empty-text" else "✅ recibido")
    event = _make_event("hola")
    if case == "internal":
        event = MessageEvent(text="hola", source=event.source, message_id="m1", internal=True)
    elif case == "goal-continuation":
        event = _make_event("[Continuing toward your standing goal]\nGoal: ship it")

    await runner._maybe_send_turn_start_ack(event, event.source)

    adapter._send_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_ack_failure_never_escapes(monkeypatch):
    runner, adapter = _ack_runner(monkeypatch)
    adapter._send_with_retry = AsyncMock(side_effect=RuntimeError("telegram down"))
    event = _make_event("hola")

    await runner._maybe_send_turn_start_ack(event, event.source)  # must not raise
