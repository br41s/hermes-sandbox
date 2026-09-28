"""The launch profile's secret scope falls back to the process environment (fork-owned).

Why: under ``gateway.multiplex_profiles`` every credential and allow/deny read goes
through the active profile's secret scope, and upstream builds that scope from
``<home>/.env`` plus external secret sources only (``agent.secret_scope.
build_profile_secret_scope``). A scoped miss never falls through to ``os.environ``.
That is right for a secondary profile, whose ``os.environ`` fallback would be the
launch profile's keys. It is wrong for the launch profile itself here, because
Zeabur injects its keys as the container environment and several of them never
reach ``/opt/data/.env``: ``TELEGRAM_ALLOWED_USERS``, ``TELEGRAM_GROUP_ALLOWED_CHATS``,
``TELEGRAM_BOT_TOKEN`` and the Shorts publishing keys among them.

That is what silenced Telegram on 2026-09-28. The adapter read an empty allowlist
and logged ``Blocked unauthorized user`` for every sender. Default-profile cron
jobs loaded a gateway config with no token and failed ``platform 'telegram' not
configured/enabled``.

The fix gives the launch profile back exactly what it had before multiplex: its
``.env`` over the process environment. The scope builder calls
``process_env_fallback()`` for the process home only, and adds each key with
``setdefault``, so the ``.env`` and external sources still win. Secondary profiles
are untouched and never see these keys: a routed turn authorizes under the
transport's profile (``GatewayRunner._under_authorization_profile``), which is
the default one, so the allowlist does not need to be copied anywhere. Nothing is
written to disk, and ``TELEGRAM_BOT_TOKEN`` never enters a profile's ``.env``
(CLAUDE.md, "Multiplex is rolled back").

Opt-in through ``HERMES_FORK_SCOPE_PROCESS_ENV=1``, which the image sets
(Dockerfile), so upstream's own scope tests keep upstream semantics.

``python -m hermes_cli.fork_ext.process_env_scope --check`` is the read-only
production probe to run before and after multiplex is switched on. It prints
names and verdicts, never a value.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict

FLAG = "HERMES_FORK_SCOPE_PROCESS_ENV"

# Keys the probe must see in the launch profile's scope for Telegram to work.
REQUIRED = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS", "TELEGRAM_GROUP_ALLOWED_CHATS")


def enabled() -> bool:
    return os.environ.get(FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def process_env_fallback() -> Dict[str, str]:
    """Non-global process env keys for the launch profile's scope; ``{}`` when the flag is off."""
    if not enabled():
        return {}
    from agent.secret_scope import _is_global_env
    return {k: v for k, v in os.environ.items() if k != FLAG and not _is_global_env(k)}


# ── the production probe ───────────────────────────────────────────────────────


def _profile_store_jobs(home: Path):
    """``(profile, job_id, name, enabled, deliver)`` for jobs in named profiles' own stores.

    Only multiplex ticks these (``cron/scheduler_provider.py``); the fork's own profile
    jobs live in the default store with a ``profile`` field. Listed so that switching
    multiplex on never silently starts a job nobody knew was there.
    """
    import json
    rows = []
    root = home / "profiles"
    if not root.is_dir():
        return rows
    for prof in sorted(p for p in root.iterdir() if p.is_dir()):
        path = prof / "cron" / "jobs.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows.append((prof.name, "?", "(unreadable jobs.json)", None, ""))
            continue
        jobs = data.get("jobs", data) if isinstance(data, dict) else data
        for job in jobs if isinstance(jobs, list) else []:
            if isinstance(job, dict):
                rows.append((prof.name, str(job.get("id", "?")), str(job.get("name", "")),
                             job.get("enabled", True), str(job.get("deliver", ""))))
    return rows


def check() -> int:
    """Simulate the multiplexed launch profile's scope and report. Names only, never values."""
    from agent import secret_scope
    from gateway.platforms._shared import platform_gate_env
    from hermes_constants import get_process_hermes_home

    home = get_process_hermes_home()
    print(f"launch home: {home}")
    print(f"{FLAG}: {'on' if enabled() else 'OFF'}")

    previous = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(True)
    token = secret_scope.set_secret_scope(secret_scope.build_profile_secret_scope(home),
                                          profile_home=str(home))
    try:
        missing = [name for name in REQUIRED if not platform_gate_env(name)]
        for name in REQUIRED:
            print(f"  {name}: {'missing' if name in missing else 'present'}")
        unseen = sorted(k for k in os.environ
                        if k != FLAG and not secret_scope._is_global_env(k)
                        and secret_scope.get_secret(k) is None)
        print(f"process env keys the launch scope cannot see: {len(unseen)}")
        for name in unseen:
            print(f"  {name}")
    finally:
        secret_scope.reset_secret_scope(token)
        secret_scope.set_multiplex_active(previous)

    rows = _profile_store_jobs(home)
    print(f"jobs in named profiles' own cron stores (multiplex ticks these): {len(rows)}")
    for prof, job_id, name, is_enabled, deliver in rows:
        state = "enabled" if is_enabled else "disabled"
        print(f"  {prof} {job_id} {state} deliver={deliver or '-'} {name}")

    ok = not missing and not unseen
    print("VERDICT: " + ("OK" if ok else "NOT READY"))
    return 0 if ok else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:
        sys.exit(check())
    print("usage: python -m hermes_cli.fork_ext.process_env_scope --check", file=sys.stderr)
    sys.exit(2)
