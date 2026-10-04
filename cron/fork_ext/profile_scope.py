"""Profile identity for cron runs (fork-owned).

Every profile-owned job lives in its profile's own cron store (stage 3,
``ops/multiplex-stage3-plan.md``); the multiplex ticker and a routed webhook run
it under that profile's home override and ``.env`` secret scope. This module adds
what they do not:

- ``_satellite_store_context`` — the identity tripwire and ``profile_run()`` for a
  job run from a profile's own store;
- ``_assert_own_subprocess_identity`` / ``_iter_sibling_profiles`` — refuse a run
  whose subprocess ``HOME`` (its git/gh identity) belongs to another profile
  (``ProfileIdentityError``);
- ``_refuse_legacy_profile_record`` — the one guard left from the old fork
  ``profile`` field (``LegacyProfileJobError``): a record that still carries it is
  refused, never run under the default identity.
"""

import logging
from contextlib import contextmanager
from pathlib import Path

# Same logger as before the move, so agent.log lines keep their
# ``cron.scheduler`` name and tests patching ``cron.scheduler.logger`` still
# see these calls.
logger = logging.getLogger("cron.scheduler")


class ProfileIdentityError(RuntimeError):
    """Raised when a profile job's runtime identity would not be its own.

    Fail-closed because in the terminal lane ``$HOME`` IS the git/gh identity —
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


class LegacyProfileJobError(RuntimeError):
    """Raised for a job record that still carries the old fork ``profile`` field.

    Stage 3 step 2 removed the code that ran such a record under its profile
    (``_job_profile_context``). Running it now would run it under the
    scheduler's DEFAULT identity — the 2026-09-12 PR-as-``hermes-auditor`` class,
    and the reason the old path failed closed. So it is refused: the run fails
    with an error naming the fix, ``hermes cron move <id> --to-profile <p>``,
    which puts the job in its profile's own store and drops the field.
    """


def legacy_profile(job: dict) -> str:
    """The retired ``profile`` value a record still carries, or ``""`` when it has none.

    ``default`` counts as none: the old path ran it under the root home, exactly
    like a plain job of the default store, so refusing it would break a job that
    runs the same as before. (``cron edit`` no longer has ``--profile`` to clear it.)
    Reads the raw record: ``jobs.py`` no longer normalises the field.
    """
    profile = str(job.get("profile") or "").strip()
    return "" if profile.lower() == "default" else profile


def _refuse_legacy_profile_record(job: dict) -> None:
    """Raise :class:`LegacyProfileJobError` if *job* still carries a named ``profile``."""
    profile = legacy_profile(job)
    if profile:
        logger.error(
            "Job '%s': record still carries profile %r — refusing to run it under "
            "the default identity; move it with `hermes cron move %s --to-profile %s`",
            job.get("id"), profile, job.get("id"), profile,
        )
        raise LegacyProfileJobError(
            f"job {job.get('id')!r} still carries the retired profile field {profile!r}; "
            f"run `hermes cron move {job.get('id')} --to-profile {profile} --apply`"
        )


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
def _satellite_store_context(job_id: str):
    """Give a job run from a profile's OWN cron store the identity guarantees
    (stage 3 step 0b, ``ops/multiplex-stage3-plan.md``).

    The multiplex ticker and a routed webhook already run such a job under the
    profile's home override and its ``.env`` secret scope. This adds the identity
    tripwire (``_assert_own_subprocess_identity``, the 2026-09-12
    PR-as-``hermes-auditor`` class) and ``profile_run()``, without which
    ``child_env_overlay`` hands the run's children nothing from the profile's
    ``.env``. The scope is the profile's ``.env`` only, never ``os.environ`` (plan
    fact 11). A job run from the launch store passes straight through.
    """
    from cron.fork_ext.dispatch import in_profile_store

    if not in_profile_store():
        yield None
        return

    from hermes_cli.fork_ext.profile_env import profile_run
    from hermes_constants import get_hermes_home

    profile_home = get_hermes_home().resolve()
    _assert_own_subprocess_identity(job_id, profile_home.name, profile_home)
    logger.info(
        "Job '%s': running from profile '%s' own cron store (%s)",
        job_id, profile_home.name, profile_home,
    )
    with profile_run():
        yield profile_home.name
