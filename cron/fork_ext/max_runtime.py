"""Wall-clock ceiling for one cron agent run (fork-owned).

Why: every other cron limit measures *inactivity*. The 600s watchdog
(``HERMES_CRON_TIMEOUT``) reads the agent's activity clock, and a streaming
model call refreshes that clock every 30s while it waits
(``agent/chat_completion_stream_monitor.py``). So a call that never answers
looks alive and nothing ever ends the run.

On 2026-09-29 an ``auditor-review`` run went silent after its fourth model call
(10:48:47 UTC) and held the single-thread profile/workdir lane
(``cron/fork_ext/dispatch.py``) for 95 minutes, until a container restart. No
stale-stream kill, retry or inactivity timeout was logged. ``merge-on-green``
and a rental's Product Sheet Writer queued behind it, and the auditor reviewed
nothing, which read as the auditor being down.

This caps the run itself: ``HERMES_CRON_MAX_RUNTIME`` seconds, default 1800,
``0`` = unlimited. On expiry the run takes the same path as an inactivity
timeout: dump every thread's stack (so the hang is diagnosable next time),
hard-interrupt the agent, abandon its thread, and fail the run so the lane moves
on and the incident watcher reports it. It bounds each agent rather than
widening the pool (CLAUDE.md, "One long agent run starves every other agent").
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from typing import Optional

logger = logging.getLogger("cron.scheduler")

ENV = "HERMES_CRON_MAX_RUNTIME"
DEFAULT_SECONDS = 1800.0


def max_runtime_seconds() -> Optional[float]:
    """The ceiling in seconds, or ``None`` for unlimited. Bad input falls back to the default."""
    from cron.env_settings import cron_env_setting

    raw = cron_env_setting(ENV).strip()
    if not raw:
        return DEFAULT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %.0fs", ENV, raw, DEFAULT_SECONDS)
        return DEFAULT_SECONDS
    if value < 0:
        logger.warning("Invalid %s=%r; using default %.0fs", ENV, raw, DEFAULT_SECONDS)
        return DEFAULT_SECONDS
    return None if value == 0 else value


def exceeded(started_monotonic: float, limit: Optional[float]) -> bool:
    return limit is not None and time.monotonic() - started_monotonic >= limit


def raise_max_runtime(agent, job_name: str, limit: float, started_monotonic: float) -> None:
    """Log the agent's last activity, hard-interrupt it and raise ``TimeoutError``.

    The message avoids "timed out"/"timeout" so the failure summary never blames the
    model provider for what is a scheduler decision.
    """
    from agent.interrupt_compat import request_hard_interrupt

    activity = {}
    if hasattr(agent, "get_activity_summary"):
        with contextlib.suppress(Exception):
            activity = agent.get_activity_summary() or {}
    elapsed = time.monotonic() - started_monotonic
    last_desc = activity.get("last_activity_desc", "unknown")
    logger.error(
        "Job '%s' ran for %.0fs, over its max runtime (%s=%.0fs) "
        "| last_activity=%s | iteration=%s/%s | tool=%s",
        job_name, elapsed, ENV, limit, last_desc,
        activity.get("api_call_count", 0), activity.get("max_iterations", 0),
        activity.get("current_tool") or "none")
    request_hard_interrupt(agent, "Cron job stopped (max runtime)", tool_reason="cron max runtime")
    raise TimeoutError(
        f"Cron job '{job_name}' ran for {int(elapsed)}s, over its max runtime "
        f"({ENV}={int(limit)}s) — last activity: {last_desc}")


_ERROR_RE = re.compile(r"over its max runtime \(" + ENV.lower() + r"=(\d+)s\)")


def delivery_notice(job_name: str, job_id: str, error_lower: str) -> Optional[str]:
    """The chat notice for a max-runtime stop, or ``None`` if *error_lower* is not one.

    Without it the summary classifies the ``TimeoutError`` as the model service not
    responding and suggests adding a fallback provider, which sends the operator after
    the wrong system.
    """
    match = _ERROR_RE.search(error_lower)
    if not match:
        return None
    minutes = max(1, round(int(match.group(1)) / 60))
    return (
        f"⚠️ Cron '{job_name}' was stopped: it ran longer than its {minutes}-minute limit "
        f"({ENV}), so it no longer holds up the jobs queued behind it. Every thread's "
        f"stack was dumped to the log to show where it hung. It will run again at its "
        f"next scheduled time. Run log: `hermes cron runs {job_id}`.")
