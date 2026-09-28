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


@pytest.mark.asyncio
async def test_fresh_turn_schedules_the_ack_once(monkeypatch):
    import gateway.run as gateway_run

    runner = _make_runner()
    runner._handle_message_with_agent = AsyncMock(return_value="agent response")
    runner._maybe_send_turn_start_ack = AsyncMock()
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    event = _make_group_event("hello", thread_id="555")
    assert await runner._handle_message(event) == "agent response"
    await _drain()

    runner._maybe_send_turn_start_ack.assert_awaited_once()
    sent_event, sent_source = runner._maybe_send_turn_start_ack.await_args.args
    assert sent_event is event
    assert sent_source.chat_id == event.source.chat_id


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
