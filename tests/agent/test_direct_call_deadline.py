"""An inline (cron) API call must have an overall deadline, not just per-read timeouts.

THE BUG THIS CLOSES

`direct_api_call` is the path every cron agent takes (`should_use_direct_api_call`
sends non-interactive, nested-pool contexts here so the interrupt worker cannot
deadlock, #62151). Its docstring claimed the client's httpx timeout "bounds a
genuinely hung provider". It does not: an httpx timeout is PER OPERATION and
resets on every byte.

Measured in production 2026-09-22 — OpenRouter returned headers and then dripped
the body at ~12 bytes/sec (`rchar` +140 every 12s, steady, for 20 minutes) on an
ESTABLISHED socket. Every individual read finished far inside the 600s read
timeout, so nothing ever fired. Five consecutive Infographic Engineer runs died
on the cron inactivity watchdog at 1200s instead, each blaming "waiting for
non-streaming API response" — the wrong layer, and the reason the real cause
went unfound for a day.

`stale_timeout_seconds` does not cover it either: that is time-to-FIRST byte.

So the tests below pin the behaviour that actually matters: a call that never
returns gets aborted at the deadline and raises something the retry/fallback
loop can act on, and a call that returns normally is never touched.
"""
import threading
import time

import pytest

from agent import chat_completion_helpers as helpers


class FakeClient:
    def __init__(self):
        self.aborted = threading.Event()
        self.abort_reason = None
        self.closed = False


class FakeAgent:
    """Only the surface `direct_api_call` actually touches."""

    provider = "openrouter"
    model = "deepseek/deepseek-v4.1-flash"
    log_prefix = ""

    def __init__(self):
        self._interrupt_requested = False
        self._active_request_abort = None
        self.activity = []
        self.client = FakeClient()

    def _touch_activity(self, what):
        self.activity.append(what)

    def _create_request_openai_client(self, *, reason, api_kwargs):
        return self.client

    def _abort_request_openai_client(self, client, *, reason):
        client.abort_reason = reason
        client.aborted.set()

    def _close_request_openai_client(self, client, *, reason):
        client.closed = True


@pytest.fixture
def agent(monkeypatch):
    a = FakeAgent()
    # Stale-streak bookkeeping is orthogonal to the deadline.
    monkeypatch.setattr(helpers, "_check_stale_giveup", lambda _a: None)
    monkeypatch.setattr(helpers, "_reset_stale_streak", lambda _a: None)
    # A short deadline keeps the suite fast; the mechanism is the same at 600s.
    monkeypatch.setattr(helpers, "get_provider_request_timeout", lambda *_a, **_k: 1.0)
    return a


def test_a_trickling_provider_is_aborted_at_the_deadline(agent, monkeypatch):
    """The production failure: a call that never completes must not hang forever."""
    started = threading.Event()

    def never_returns(_agent, _kwargs, make_client):
        client = make_client("test")
        started.set()
        # Stand in for httpx blocked in Response.read() on a trickled body:
        # progress is being made, so no per-operation timeout will ever fire.
        if not client.aborted.wait(timeout=20):
            raise AssertionError("deadline never aborted the request")
        raise ConnectionError("transport closed by abort")

    monkeypatch.setattr(helpers, "_dispatch_nonstreaming_api_request", never_returns)

    begun = time.monotonic()
    with pytest.raises(TimeoutError) as excinfo:
        helpers.direct_api_call(agent, {})
    elapsed = time.monotonic() - begun

    assert started.is_set()
    assert agent.client.aborted.is_set(), "the client was never aborted"
    assert "deadline" in agent.client.abort_reason
    assert elapsed < 10, f"took {elapsed:.1f}s — the deadline did not bound the call"
    # The error must say WHY, or the next reader re-derives all of this.
    assert "deadline" in str(excinfo.value)
    assert "trickled" in str(excinfo.value)


def test_a_normal_call_is_never_aborted(agent, monkeypatch):
    """No spurious aborts, and the timer must not outlive the call."""
    def quick(_agent, _kwargs, make_client):
        make_client("test")
        return {"ok": True}

    monkeypatch.setattr(helpers, "_dispatch_nonstreaming_api_request", quick)

    assert helpers.direct_api_call(agent, {}) == {"ok": True}
    assert not agent.client.aborted.is_set()

    # Past the deadline: a leaked timer would fire here and abort a client that
    # is already finished, poisoning the NEXT call on a reused agent.
    time.sleep(1.4)
    assert not agent.client.aborted.is_set(), "a cancelled timer still fired"
    assert agent.client.closed, "the request client was not closed"


def test_a_real_transport_error_keeps_its_own_identity(agent, monkeypatch):
    """Only a deadline abort becomes TimeoutError; other failures pass through."""
    def boom(_agent, _kwargs, make_client):
        make_client("test")
        raise ConnectionError("DNS exploded")

    monkeypatch.setattr(helpers, "_dispatch_nonstreaming_api_request", boom)

    with pytest.raises(ConnectionError, match="DNS exploded"):
        helpers.direct_api_call(agent, {})


def test_no_deadline_configured_leaves_behaviour_unchanged(agent, monkeypatch):
    """A provider with no request_timeout_seconds must not gain a 0s deadline."""
    monkeypatch.setattr(helpers, "get_provider_request_timeout", lambda *_a, **_k: None)

    def quick(_agent, _kwargs, make_client):
        make_client("test")
        return {"ok": True}

    monkeypatch.setattr(helpers, "_dispatch_nonstreaming_api_request", quick)
    assert helpers.direct_api_call(agent, {}) == {"ok": True}
    assert not agent.client.aborted.is_set()


def test_an_interrupt_still_wins_over_the_deadline(agent, monkeypatch):
    """Interactive interrupt semantics must not be swallowed by the new path."""
    def interrupted(_agent, _kwargs, make_client):
        make_client("test")
        agent._interrupt_requested = True
        raise ConnectionError("closed by interrupt")

    monkeypatch.setattr(helpers, "_dispatch_nonstreaming_api_request", interrupted)

    with pytest.raises(InterruptedError):
        helpers.direct_api_call(agent, {})
