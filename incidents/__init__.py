"""Hermes incident watcher — Phase 0.

A scheduled, read-only sweep that surfaces failures to a Telegram thread using
signals Hermes already produces (failed cron jobs + errored Langfuse traces).
No new external integrations. See ``incidents/sweep.py``.
"""
