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
``.env`` over the environment it was launched with, frozen when multiplex turns on
(upstream's ``launch_secret_scope`` does the same for the bodies it binds; the
authorization and cron-delivery paths build the scope directly and never reach it). The scope builder calls
``process_env_fallback()`` for the process home only, and adds each key with
``setdefault``, so the ``.env`` and external sources still win. Secondary profiles
are untouched and never see these keys: a routed turn authorizes under the
transport's profile (``GatewayRunner._under_authorization_profile``), which is
the default one, so the allowlist does not need to be copied anywhere. Nothing is
written to disk, and ``TELEGRAM_BOT_TOKEN`` never enters a profile's ``.env``
(CLAUDE.md, "Multiplex is on").

Opt-in through ``HERMES_FORK_SCOPE_PROCESS_ENV=1``, which the image sets
(Dockerfile), so upstream's own scope tests keep upstream semantics.

``python -m hermes_cli.fork_ext.process_env_scope --check`` is the read-only
production probe to run before and after multiplex is switched on. It prints
names and verdicts, never a value.
"""

from __future__ import annotations

import contextvars
import os
import sys
from pathlib import Path
from typing import Dict

FLAG = "HERMES_FORK_SCOPE_PROCESS_ENV"

# Keys the probe must see in the launch profile's scope for Telegram to work.
REQUIRED = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS", "TELEGRAM_GROUP_ALLOWED_CHATS")


# Context-local off switch for the probe's upstream-scope build: never touches os.environ,
# so a scope built on another thread or task keeps the fallback.
_SUPPRESSED: contextvars.ContextVar[bool] = contextvars.ContextVar("_fork_process_env_suppressed",
                                                                  default=False)


def enabled() -> bool:
    if _SUPPRESSED.get():
        return False
    return os.environ.get(FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def process_env_fallback() -> Dict[str, str]:
    """Non-global launch env keys for the launch profile's scope; ``{}`` when the flag is off.

    Read through upstream's ``tui_gateway.launch_profile_policy._launch_env``: once multiplex
    is active that is the env frozen at activation (the gateway captures it where it flips
    multiplex on, before any secondary profile has run), never the live ``os.environ``,
    which a secondary's context could have touched since (#107422). Before activation it is
    the live env, which is provably the launch profile's.
    """
    if not enabled():
        return {}
    from agent.secret_scope import _is_global_env
    from tui_gateway.launch_profile_policy import _launch_env
    return {k: v for k, v in _launch_env().items() if k != FLAG and not _is_global_env(k)}


# ── the production probe ───────────────────────────────────────────────────────


def _profile_store_jobs(home: Path):
    """``(profile, job_id, name, enabled, deliver)`` for jobs in named profiles' own stores.

    The gateway ticks these stores whether multiplex is on or off
    (``gateway/run.py`` ``_cron_tick_profile_homes``); the fork's own profile jobs live
    in the default store with a ``profile`` field. Listed so that no job runs that
    nobody knew was there.
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


# ── stage 3 step 0e: what a move into a profile's own store would change ─────
# ops/multiplex-stage3-plan.md, section 3. All names and classes; never a value.

# Keys a satellite job must NOT resolve: losing them on a move is the point, not a
# regression (CLAUDE.md: a satellite holding the bot token is a fatal duplicate
# credential; the Shorts keys are BigLobster's publishing tokens).
_DENY_EXACT = frozenset({"TELEGRAM_BOT_TOKEN", "SHORTS_STUDIO_GITHUB_TOKEN"})
_DENY_PREFIXES = ("YOUTUBE_", "META_", "HERMES_RENTAL_LANGFUSE_")


def _denied(name: str) -> bool:
    return (name in _DENY_EXACT or name.startswith(_DENY_PREFIXES)
            or (name.startswith("TELEGRAM_") and "ALLOWED" in name))


def _named_profiles(home: Path):
    """``(name, home)`` for every named profile the gateway serves (default excluded)."""
    try:
        from hermes_cli.profiles import profiles_to_serve
        return [(n, Path(h)) for n, h in profiles_to_serve(multiplex=True) if n != "default"]
    except Exception:
        root = home / "profiles"
        return sorted((p.name, p) for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


def _satellite_losses(profile_home: Path) -> tuple:
    """``(lost, policy)``: key names a fork ``profile`` job resolves and a job in that
    profile's own store would not. The fork scope is ``{**os.environ, **profile .env}``
    (``cron/fork_ext/profile_scope.py``); the satellite scope is the ``.env`` alone.
    Global keys read ``os.environ`` either way, and the deny set is meant to be lost.
    ``policy`` is the subset a rental loses by design (``TENANT_EXCLUDE``)."""
    from agent.secret_scope import _is_global_env
    from hermes_cli.fork_ext.boot_reconcile import TENANT_EXCLUDE, is_rented_tenant

    satellite = _upstream_scope(profile_home)
    lost = {k for k in os.environ
            if k != FLAG and not _is_global_env(k) and not _denied(k) and k not in satellite}
    policy = lost & set(TENANT_EXCLUDE) if is_rented_tenant(profile_home / ".env") else set()
    return lost - policy, policy


def _auditor_child_view(auditor_home: Path) -> list:
    """``(name, in_service_env, resolves)`` for each env name ``auditor/llm.py`` reads,
    as a child of an auditor job in its own store would resolve it: the child env
    (``_make_run_env`` under the auditor's home, scope and ``profile_run``), then
    ``$HERMES_HOME/.env`` (``_env_value`` -> ``get_env_value``)."""
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    from hermes_cli.fork_ext.profile_env import profile_run
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.environments.local import _make_run_env

    names = ("HERMES_AUDITOR_JUDGE_MAX_TOKENS", "HERMES_AUDITOR_JUDGE_REASONING_EFFORT",
             "HERMES_AUDITOR_JUDGE_DEADLINE_SECONDS", "HERMES_AUDITOR_CONTENT_MODEL",
             "HERMES_AUDITOR_SYSTEM_MODEL", "HERMES_AUDITOR_OPENROUTER_API_KEY",
             "OPENROUTER_API_KEY")
    dotenv = _upstream_scope(auditor_home)
    home_token = set_hermes_home_override(str(auditor_home))
    scope_token = set_secret_scope(dotenv, profile_home=str(auditor_home))
    try:
        with profile_run():
            child = _make_run_env({})
    finally:
        reset_secret_scope(scope_token)
        reset_hermes_home_override(home_token)
    return [(n, bool(os.environ.get(n)), bool(child.get(n) or dotenv.get(n))) for n in names]


def _deliver_class(target: str, profile_thread: str) -> str:
    """One deliver/failure_deliver value as a class (plan section 1): ``local``,
    ``own topic``, ``General`` or ``other``. Never prints a chat id or thread."""
    classes = []
    for part in [p.strip() for p in str(target or "").split(",") if p.strip()] or ["local"]:
        platform, _, rest = part.partition(":")
        if part in ("local", "origin"):
            classes.append(part)
        elif platform != "telegram":
            classes.append(f"other:{platform}")
        elif not rest:
            classes.append("own topic" if profile_thread else "General (no routing thread)")
        else:
            thread = rest.split(":")[1] if ":" in rest else ""
            if not thread:
                classes.append("General")
            elif thread == profile_thread:
                classes.append("own topic")
            else:
                classes.append("other topic")
    return "+".join(classes)


def _default_store_profile_jobs(home: Path) -> list:
    """``(profile, id, name, enabled, deliver class, failure class, workdir)`` for
    every fork ``profile`` job in the default store: the jobs a move would carry."""
    import json

    path = home / "cron" / "jobs.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    rows = []
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict) or not str(job.get("profile") or "").strip():
            continue
        prof = str(job["profile"]).strip()
        scope = _upstream_scope(home / "profiles" / prof)
        thread = (scope.get("TELEGRAM_CRON_THREAD_ID")
                  or scope.get("TELEGRAM_HOME_CHANNEL_THREAD_ID") or "")
        rows.append((prof, str(job.get("id", "?")), str(job.get("name", "")),
                     job.get("enabled", True),
                     _deliver_class(job.get("deliver"), thread),
                     _deliver_class(job.get("failure_deliver"), thread) if job.get("failure_deliver") else "-",
                     "yes" if str(job.get("workdir") or "").strip() else "no"))
    return rows


def _print_move_report(home: Path) -> None:
    rows = _default_store_profile_jobs(home)
    print(f"default-store profile jobs (what a move would carry): {len(rows)}")
    for prof, job_id, name, is_enabled, deliver, failure, workdir in rows:
        state = "enabled" if is_enabled else "disabled"
        print(f"  {prof} {job_id} {state} deliver={deliver} failure={failure} workdir={workdir} {name}")

    losses = {name: _satellite_losses(phome) for name, phome in _named_profiles(home)}
    common = set.intersection(*(lost for lost, _ in losses.values())) if losses else set()
    print("keys a fork profile job resolves and a job in its profile's own store would not "
          f"(deny set excluded). Lost by every profile: {len(common)}")
    for name in sorted(common):
        print(f"  {name}")
    for prof, (lost, policy) in losses.items():
        extra = sorted(lost - common)
        if extra:
            print(f"  [{prof}] also lost: {len(extra)} {' '.join(extra)}")
        if policy:
            print(f"  [{prof}] rental, lost by policy (TENANT_EXCLUDE): {' '.join(sorted(policy))}")

    auditor = home / "profiles" / "auditor"
    if auditor.is_dir():
        print("auditor child env, as a job in its own store would resolve it:")
        for name, in_service, resolves in _auditor_child_view(auditor):
            if resolves:
                verdict = "resolves"
            elif in_service:
                verdict = "MISSING (set in the process env, not reaching the child)"
            else:
                verdict = "unset (default applies)"
            print(f"  {name}: {verdict}")


def _upstream_scope(home: Path) -> Dict[str, str]:
    """``home``'s scope as upstream builds it (flag off): ``.env`` plus external sources."""
    from agent.secret_scope import build_profile_secret_scope
    token = _SUPPRESSED.set(True)
    try:
        return build_profile_secret_scope(home)
    finally:
        _SUPPRESSED.reset(token)


def check() -> int:
    """Simulate the multiplexed launch profile's scope and report. Names only, never values.

    Run it as its own process (``--check``), never from a live gateway: it switches the
    process-global multiplex flag and installs a scope for its duration.

    NOT READY means a key in ``REQUIRED`` does not resolve through the scope a multiplexed
    Telegram turn would read: the image lacks the flag, or the key is absent from the
    environment this probe was started with (start it under ``with-contenv``, as the
    gateway is).
    """
    from agent import secret_scope
    from gateway.platforms._shared import platform_gate_env
    from hermes_constants import get_process_hermes_home

    if secret_scope.is_multiplex_active():
        raise RuntimeError("process_env_scope.check() must run as its own process, "
                           "never inside a multiplexed gateway")
    home = get_process_hermes_home()
    print(f"launch home: {home}")
    print(f"{FLAG}: {'on' if enabled() else 'OFF'}")

    # What the fallback has to cover: keys the process has and upstream's scope lacks.
    upstream = _upstream_scope(home)
    fallback_only = sorted(k for k in os.environ
                           if k != FLAG and not secret_scope._is_global_env(k) and k not in upstream)

    token = None
    try:
        secret_scope.set_multiplex_active(True)
        token = secret_scope.set_secret_scope(secret_scope.build_profile_secret_scope(home),
                                              profile_home=str(home))
        missing = [name for name in REQUIRED if not platform_gate_env(name)]
    finally:
        if token is not None:
            secret_scope.reset_secret_scope(token)
        secret_scope.set_multiplex_active(False)

    for name in REQUIRED:
        print(f"  {name}: {'missing' if name in missing else 'present'}")
    covered = "covered by the fallback" if enabled() else "INVISIBLE under multiplex"
    print(f"process env keys not in {home / '.env'} ({covered}): {len(fallback_only)}")
    for name in fallback_only:
        print(f"  {name}")

    rows = _profile_store_jobs(home)
    print(f"jobs in named profiles' own cron stores (the gateway ticks these): {len(rows)}")
    for prof, job_id, name, is_enabled, deliver in rows:
        state = "enabled" if is_enabled else "disabled"
        print(f"  {prof} {job_id} {state} deliver={deliver or '-'} {name}")

    try:
        _print_move_report(home)
    except Exception as exc:  # informational: never costs the multiplex verdict
        print(f"move report failed: {type(exc).__name__}")

    ok = enabled() and not missing
    print("VERDICT: " + ("OK" if ok else "NOT READY"))
    return 0 if ok else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:
        # Run the IMPORTED module's check, never this ``__main__`` copy: the scope builder
        # consults ``hermes_cli.fork_ext.process_env_scope._SUPPRESSED``, so suppressing a
        # second copy's ContextVar left the fallback on in the "upstream" build and the
        # fallback-only list came back empty in production.
        from hermes_cli.fork_ext import process_env_scope as _module
        sys.exit(_module.check())
    print("usage: python -m hermes_cli.fork_ext.process_env_scope --check", file=sys.stderr)
    sys.exit(2)
