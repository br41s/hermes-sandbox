"""Fork: a cron agent run has a wall-clock ceiling (cron/fork_ext/max_runtime.py).

2026-09-29: an auditor run hung after its fourth model call and held the sequential
profile/workdir lane for 95 minutes. A waiting stream refreshes the agent's activity
clock every 30s, so the 600s inactivity watchdog never fired. These pin that a run
which keeps looking active is still stopped at HERMES_CRON_MAX_RUNTIME, that it
leaves the diagnostics the inactivity path leaves, and that its failure notice does
not blame the model provider.
"""

from __future__ import annotations

import threading

import pytest

import cron.scheduler as scheduler
from cron.fork_ext import max_runtime as mr


class _AlwaysActiveHungAgent:
    """Blocks in run_conversation while reporting fresh activity, like a waiting stream."""

    def __init__(self):
        self.release = threading.Event()
        self.hard_interrupts = []

    def run_conversation(self, prompt, task_id=None):
        self.release.wait(30)
        return {"final_response": "late"}

    def get_activity_summary(self):
        return {"seconds_since_activity": 0.0,
                "last_activity_desc": "waiting for stream response (30s, first_chunk)",
                "api_call_count": 4, "max_iterations": 90, "current_tool": None}

    def hard_interrupt(self, message=None):
        self.hard_interrupts.append(message)
        self.release.set()


class _QuickAgent(_AlwaysActiveHungAgent):
    def run_conversation(self, prompt, task_id=None):
        return {"final_response": "done"}


def _run(agent):
    return scheduler._run_agent_with_watchdog(
        agent, "prompt", {"id": "c19bb95c0a62", "schedule": {"kind": "cron"}},
        "c19bb95c0a62", "auditor-review", "task-1", None)


@pytest.mark.parametrize("raw,expected", [
    ("", 1800.0), ("0", None), ("45", 45.0), ("2700", 2700.0),
    ("abc", 1800.0), ("-5", 1800.0),
])
def test_limit_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv(mr.ENV, raw)
    assert mr.max_runtime_seconds() == expected


def test_a_run_that_keeps_looking_active_is_stopped_at_the_ceiling(monkeypatch):
    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "600")
    monkeypatch.setenv(mr.ENV, "0.01")
    dumps, abandoned = [], []
    monkeypatch.setattr(scheduler, "_dump_stuck_agent_stack",
                        lambda job_id, idle, reason="inactivity timeout": dumps.append((job_id, reason)))
    monkeypatch.setattr(scheduler, "_note_abandoned_agent_thread", lambda: abandoned.append(1))
    agent = _AlwaysActiveHungAgent()

    with pytest.raises(TimeoutError, match=r"over its max runtime \(HERMES_CRON_MAX_RUNTIME=0s\)"):
        _run(agent)

    assert dumps == [("c19bb95c0a62", "max runtime exceeded")]
    assert abandoned == [1]
    assert agent.hard_interrupts, "the hung agent must be told to stop"


def test_a_run_inside_the_ceiling_returns_its_result(monkeypatch):
    monkeypatch.setenv(mr.ENV, "1800")
    assert _run(_QuickAgent()) == {"final_response": "done"}


def test_the_ceiling_applies_even_with_the_inactivity_watchdog_off(monkeypatch):
    """HERMES_CRON_TIMEOUT=0 used to mean a blocking wait with no limit at all."""
    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")
    monkeypatch.setenv(mr.ENV, "0.01")
    monkeypatch.setattr(scheduler, "_dump_stuck_agent_stack", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "_note_abandoned_agent_thread", lambda: None)
    with pytest.raises(TimeoutError, match="max runtime"):
        _run(_AlwaysActiveHungAgent())


def test_the_notice_does_not_blame_the_model_provider():
    job = {"name": "auditor-review", "id": "c19bb95c0a62"}
    error = ("TimeoutError: Cron job 'auditor-review' ran for 1800s, over its max runtime "
             "(HERMES_CRON_MAX_RUNTIME=1800s) — last activity: waiting for stream response (30s, first_chunk)")
    msg = scheduler._summarize_cron_failure_for_delivery(job, error)
    assert "30-minute limit" in msg and "HERMES_CRON_MAX_RUNTIME" in msg
    assert "did not respond in time" not in msg
    assert "backup provider" not in msg.lower()
    assert "hermes cron runs c19bb95c0a62" in msg


def test_the_inactivity_notice_is_unchanged():
    job = {"name": "Daily Repo Sweep", "id": "82d65bdd5ba9"}
    error = "TimeoutError: Cron job 'Daily Repo Sweep' idle for 1239s (limit 600s) — last activity: terminal"
    assert mr.delivery_notice("x", "y", error.lower()) is None
    assert "stalled" in scheduler._summarize_cron_failure_for_delivery(job, error).lower()
