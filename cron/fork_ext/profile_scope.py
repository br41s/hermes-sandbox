"""Per-job Hermes profile scope for cron runs (fork-owned).

Moved verbatim out of ``cron/scheduler.py`` so the fork's code does not sit
inside upstream's file:

- ``_job_profile_context`` — runs one job under its configured profile, fail
  closed (``ProfileResolutionError``) when the profile cannot be resolved;
- ``_assert_own_subprocess_identity`` / ``_iter_sibling_profiles`` — refuse a
  run whose subprocess ``HOME`` (its git/gh identity) belongs to another
  profile (``ProfileIdentityError``);
- ``_read_profile_env_value`` — read one key off a profile's ``.env`` for
  delivery routing, without touching ``os.environ``.

``cron.scheduler`` re-imports the names its call sites and callers use
(``run_job``, the home-target lookups, ``tools/cronjob_tools.py``), so they
resolve the same objects as before and patches of those ``cron.scheduler``
attributes still apply. ``_job_profile_context`` calls
``_assert_own_subprocess_identity`` in THIS module, so patch that one here.
"""

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

# Same logger as before the move, so agent.log lines keep their
# ``cron.scheduler`` name and tests patching ``cron.scheduler.logger`` still
# see these calls.
logger = logging.getLogger("cron.scheduler")


class ProfileIdentityError(RuntimeError):
    """Raised when a profile job's runtime identity would not be its own.

    Sibling of :class:`ProfileResolutionError`, and fail-closed for the same
    reason: in the terminal lane ``$HOME`` IS the git/gh identity —
    ``GITHUB_TOKEN``/``GH_TOKEN`` are stripped from every spawned subprocess,
    so ``~/.gitconfig``, ``~/.git-credentials`` and ``~/.config/gh/hosts.yml``
    are the only credentials a command can reach. A job whose subprocess
    ``HOME`` resolves outside its own profile will commit, push and open PRs
    as somebody else. That is strictly worse than not running: on 2026-09-12
    a FinView content job ran with the auditor's ``HOME`` and opened
    FinView PR #245 as ``hermes-auditor``, which the auditor then skipped as
    its own work, so the PR silently left the review queue altogether.

    The auditor drops such a PR on BOTH sides — ``auditor.prompt`` step 1a
    skips any PR whose author is ``hermes-auditor`` ("never review your own
    work") and ``docker/profiles/auditor/SOUL.md`` forbids merging one — so
    the observable symptom is not an unreviewed auto-merge but the review gate
    quietly disappearing, with no error and no alert. The PR then merges only
    if a human merges it, which is exactly what happened to the earlier
    mis-attributed content PRs (FinView #95/#97/#100/#144/#173/#190/#200/
    #202/#204/#205, biglobster #467/#476/#479/#490/#492/#493 — all merged by
    ``br41s`` by hand, none carrying an auditor review).
    """


class ProfileResolutionError(RuntimeError):
    """Raised when a job's configured profile can't be resolved.

    A job that opts into a profile is relying on that profile's isolated
    identity (its own GITHUB_TOKEN/HERMES_HOME) — e.g. the auditor's dedicated
    ``hermes-auditor`` bot account, kept separate from the CEO's own account on
    purpose. Silently continuing under the scheduler's default profile would
    run the job as the WRONG identity instead of not running it at all, so
    this is raised rather than swallowed — the caller should skip the run
    (job stays due, retried on the job's normal schedule) instead of executing
    under an unintended identity.
    """


def _iter_sibling_profiles(profile_home: Path):
    """Yield the profile directories alongside *profile_home*, or nothing if
    that listing is unavailable. Never raises — a failed scandir must not be
    able to wedge a job."""
    try:
        return list(profile_home.parent.iterdir())
    except OSError:
        return []


def _assert_own_subprocess_identity(
    job_id: str, profile: str, profile_home: Path
) -> None:
    """Verify this run will not act under a DIFFERENT profile's identity.

    Cheap (a couple of path comparisons, no subprocess) and runs once per
    profile job, immediately after the Hermes-home override is installed — so
    it checks the identity the job is about to act under instead of trusting
    the previous job to have cleaned up after itself.

    ``get_subprocess_home()`` is the exact function the terminal and file lanes
    call to pick ``HOME`` for every command they spawn, so asking it here asks
    the real question. In that lane ``HOME`` IS the git/gh identity:
    ``GITHUB_TOKEN``/``GH_TOKEN`` are stripped from every spawned subprocess,
    leaving ``~/.gitconfig``, ``~/.git-credentials`` and
    ``~/.config/gh/hosts.yml`` as the only credentials a command can reach.

    The assertion is deliberately narrow — *not another profile's home* rather
    than *this profile's home* — because the ``auto`` home policy legitimately
    keeps the real OS-user home on host (non-container) installs, and demanding
    a profile home there would fail every job on every host deployment. A
    sibling profile's home is never a legitimate answer under any policy, which
    makes this the one check that is both universally safe and sufficient to
    catch the 2026-09-12 class.

    A profile with no ``home/`` directory falls back to the OS user's home, so
    its jobs would commit as the *default* identity rather than their own.
    Whether that is a bug depends on the install, and the answer is readable
    straight off the disk: if any SIBLING profile has a ``home/``, this
    deployment pins identity per profile (``docker/cont-init.d/03-biglobster-config``
    creates one for every profile carrying a ``SOUL.md``) and a profile without
    one is mis-provisioned — fail closed. If no sibling has one, this is a host
    install running the ``auto`` home policy, where the real OS-user home is the
    correct and only answer — carry on.

    Calibrating off the siblings rather than hardcoding "container => required"
    is what lets the same check be strict in production and silent on a laptop.
    It also closes the ``earthsaver`` shape: a bare directory under
    ``profiles/`` is accepted as a profile by ``resolve_profile_env`` even when
    nothing ever onboarded it (no ``SOUL.md``, no ``.env``, no ``home/``), so
    without this a half-created profile silently runs as the owner account.
    """
    from hermes_constants import get_subprocess_home

    expected = profile_home / "home"
    if not expected.is_dir():
        siblings_pin_identity = any(
            sibling.is_dir()
            and sibling.name != profile_home.name
            and (sibling / "home").is_dir()
            for sibling in _iter_sibling_profiles(profile_home)
        )
        if siblings_pin_identity:
            raise ProfileIdentityError(
                f"profile {profile!r} has no subprocess home at {expected}, but "
                f"other profiles on this install do — it was never fully "
                f"provisioned and its jobs would run as the OS user"
            )
        logger.debug(
            "Job '%s': profile '%s' has no subprocess home; no sibling has one "
            "either, so this install does not pin identity per profile",
            job_id, profile,
        )
        return
    try:
        resolved = get_subprocess_home()
    except Exception as exc:  # pragma: no cover - defensive
        raise ProfileIdentityError(
            f"could not resolve the subprocess home for profile {profile!r}: {exc}"
        ) from exc
    if resolved is None:
        return
    resolved_path = Path(resolved).resolve()
    siblings_root = profile_home.parent.resolve()
    own = profile_home.resolve()
    if resolved_path.is_relative_to(siblings_root) and not resolved_path.is_relative_to(own):
        raise ProfileIdentityError(
            f"profile {profile!r} resolves its subprocess home to "
            f"{str(resolved_path)!r}, which belongs to another profile — "
            f"git/gh in this run would authenticate as somebody else"
        )
    logger.debug(
        "Job '%s': subprocess home %r is not another profile's", job_id, resolved
    )


@contextmanager
def _job_profile_context(job_id: str, profile: Optional[str]):
    """Temporarily run a job under a specific Hermes profile.

    Cron jobs are stored and scheduled by the profile running the scheduler, but
    an individual job can opt into a different runtime profile. While active,
    the scheduler's test/override hook and a context-local Hermes home override
    both point at the resolved profile directory so _get_hermes_home(),
    .env/config loading, script resolution, AIAgent construction, and downstream
    get_hermes_home() callers agree on the same home.

    The profile's ``.env`` is installed as this run's secret scope
    (``agent.secret_scope.set_secret_scope``, a ContextVar) and is never
    written into ``os.environ``: ``get_secret``/``get_env_value`` readers see
    the profile's values, terminal and script children get them through
    ``child_env_overlay``, and a job running concurrently on another thread
    keeps seeing only the process environment. That is what lets upstream
    v2026.8.31 drop ``_terminal_cwd_lock`` without a plain job reading a
    profile job's keys. The snapshot/restore of ``os.environ`` below stays as
    a backstop for any code that still writes there.

    Raises ``ProfileResolutionError`` (rather than falling back to the
    scheduler's default profile) if the configured profile can't be resolved —
    see that class's docstring for why fail-open is unsafe here.
    """
    raw_profile = str(profile or "").strip()
    if not raw_profile:
        yield None
        return

    # NOTE: deliberately does NOT assign the module-global ``_hermes_home``.
    # That global is a process-wide test monkeypatch hook, so setting it here
    # leaked this job's profile into every OTHER job running concurrently —
    # `_get_hermes_home()` prefers it over `get_hermes_home()`. Upstream's
    # dispatch rewrite made that latent bug live: sequential (profile) jobs
    # moved off the tick thread onto the `cron-seq` pool, so they now run
    # genuinely concurrently with the `cron-parallel` pool instead of blocking
    # it. A profile-less job then resolved its scripts/ dir under whichever
    # profile happened to be mid-run (observed 2026-07-31: incident-watcher
    # failing hourly with "Script not found:
    # /opt/data/profiles/{biglobster,auditor}/scripts/incident_sweep.sh").
    # `set_hermes_home_override` below is a ContextVar — per-thread, and first
    # in `get_hermes_home()`'s resolution order — so it already scopes this
    # correctly without the global.
    env_snapshot = os.environ.copy()

    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    normalized_profile = normalize_profile_name(raw_profile)
    try:
        profile_home = Path(resolve_profile_env(normalized_profile)).resolve()
    except (FileNotFoundError, ValueError) as exc:
        logger.error(
            "Job '%s': configured profile %r could not be resolved (%s) — "
            "skipping this run rather than executing under the scheduler's "
            "default identity",
            job_id, raw_profile, exc,
        )
        raise ProfileResolutionError(
            f"profile {raw_profile!r} could not be resolved: {exc}"
        ) from exc

    from agent.secret_scope import (
        build_profile_secret_scope,
        reset_secret_scope,
        set_secret_scope,
    )

    override_token = None
    scope_token = None
    try:
        override_token = set_hermes_home_override(profile_home)
        # Replaces the scheduler's scope for the length of the run: that one
        # is built from the scheduler's own home before this context is
        # entered, so without this a profile job read the DEFAULT profile's
        # .env through every get_secret() call.
        #
        # The scope is the process environment with the profile's .env over
        # it — exactly what the job used to see once load_hermes_dotenv had
        # merged the .env into os.environ. get_secret() treats a scope as
        # authoritative, so a key the profile .env deliberately leaves out
        # (a rental's EXA/HF keys) must still resolve to the process value,
        # as it did before; and os.environ now only ever holds the process's
        # own values, so carrying it over cannot hand one profile another's.
        scope_token = set_secret_scope(
            {**os.environ, **build_profile_secret_scope(profile_home)}
        )
        _assert_own_subprocess_identity(job_id, normalized_profile, profile_home)
        logger.info(
            "Job '%s': using Hermes profile '%s' (%s)",
            job_id,
            normalized_profile,
            profile_home,
        )
        yield normalized_profile
    finally:
        if scope_token is not None:
            reset_secret_scope(scope_token)
        if override_token is not None:
            reset_hermes_home_override(override_token)
        # Delta-based restore: remove added keys, restore changed keys.
        # Avoids a brief window where other threads see an empty env.
        added = set(os.environ.keys()) - set(env_snapshot.keys())
        for k in added:
            os.environ.pop(k, None)
        for k, v in env_snapshot.items():
            if os.environ.get(k) != v:
                os.environ[k] = v


def _read_profile_env_value(profile: Optional[str], key: str) -> str:
    """Read a single value from a profile's ``.env`` file, without touching
    ``os.environ``.

    Delivery resolution (``_deliver_result``) runs *after* ``run_job()``'s
    ``_job_profile_context`` has already restored the process environment
    (see that context manager's docstring) — so by the time a job's output
    is delivered, ``os.getenv()`` only ever sees the scheduler's own default
    environment, never the job's profile overrides. A profile-scoped job's
    routing.env-seeded chat/thread IDs (docker/profiles/<name>/routing.env,
    synced into profiles/<name>/.env on boot) must be read directly off
    disk instead. Returns "" on any resolution failure — callers already
    treat an empty string as "not configured" and fall back to the global
    env var.
    """
    raw_profile = str(profile or "").strip()
    if not raw_profile:
        return ""
    try:
        from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
        profile_home = Path(resolve_profile_env(normalize_profile_name(raw_profile)))
    except (FileNotFoundError, ValueError):
        return ""
    env_path = profile_home / ".env"
    if not env_path.is_file():
        return ""
    try:
        from dotenv import dotenv_values
        return (dotenv_values(str(env_path)).get(key) or "").strip()
    except Exception:
        return ""
