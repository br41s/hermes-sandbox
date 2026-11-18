"""Per-run isolated git checkouts for cron agent jobs (fork-owned).

Moved verbatim out of ``cron/scheduler.py`` so the fork's code does not sit
inside upstream's file. ``cron.scheduler`` re-imports every name below, so
its call sites (``_run_job_impl`` provisions and cleans up, ``tick`` sweeps)
and anything importing them from ``cron.scheduler`` are unchanged.

Patch the functions' collaborators (``_git``, ``_is_git_worktree``,
``subprocess``) HERE, not on ``cron.scheduler``: the functions resolve them
in this module's globals.
"""

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from hermes_cli._subprocess_compat import windows_hide_flags
from tools.environments.local import build_subprocess_env

# Same logger as before the move, so agent.log lines keep their
# ``cron.scheduler`` name and tests patching ``cron.scheduler.logger`` still
# see these calls.
logger = logging.getLogger("cron.scheduler")


# ---------------------------------------------------------------------------
# Per-run isolated checkouts
#
# Multiple cron AGENT jobs used to share one physical git working tree as their
# workdir (e.g. /opt/data/biglobster for the biglobster SEO agent + the content
# gap-hunter). Uncommitted edits from one agent survived in the shared tree, and
# the next agent's "clean tree" protocol (`git checkout -- <tracked file>`)
# reverted them — a near-miss data-loss class (2026-06-28). The agent-mailbox
# page-lock is a JSON advisory lock; it never locks the filesystem.
#
# Fix: when an agent job's workdir is a git working tree, the scheduler runs the
# agent in an EPHEMERAL local clone of that tree (one per run) and removes it
# afterwards. The agent physically cannot reach the shared tree, so two agents
# running close together can never clobber each other's uncommitted work. The
# clone is then fetched from origin and checked out at the tip of origin's
# default branch, so prior-run leftovers and a stale shared tree both vanish; if
# that fetch fails, the run continues on the shared tree's committed HEAD and a
# warning is logged (job prompts still fetch on their own). The per-URL atomic
# commit+push-to-main flow is unchanged — it just happens inside the clone.
#
# Scope: AGENT jobs only. no_agent script jobs keep their configured workdir
# (they rely on stable absolute paths and are read-mostly). Disable globally with
# HERMES_CRON_ISOLATE_WORKDIR=0. Override the ephemeral base with
# HERMES_CRON_CHECKOUT_DIR (must be writable by the agent user).
# ---------------------------------------------------------------------------

_CHECKOUT_PREFIX = "cron-checkout-"


class IsolatedCheckoutError(RuntimeError):
    """Provisioning an isolated checkout failed; the run must abort (fail-closed).

    We never silently fall back to the shared working tree — that is exactly the
    clobber hazard this machinery exists to prevent.
    """


def _isolation_enabled() -> bool:
    raw = (os.environ.get("HERMES_CRON_ISOLATE_WORKDIR") or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _checkout_base() -> str:
    return (os.environ.get("HERMES_CRON_CHECKOUT_DIR") or "").strip() or tempfile.gettempdir()


def _is_git_worktree(path: str) -> bool:
    """True when ``path`` is a git working tree (has a .git dir or file)."""
    try:
        return (Path(path) / ".git").exists()
    except OSError:
        return False


def _git(
    args: list, cwd: Optional[str] = None, timeout: int = 300, allow_protocol: Optional[str] = None
) -> subprocess.CompletedProcess:
    popen_kwargs = {"creationflags": windows_hide_flags()} if sys.platform == "win32" else {}
    # `-c safe.directory=*` so cloning/reading the source tree works even when it
    # is owned by a different user than the one running the scheduler (a recurring
    # container gotcha: the shared clone is hermes-owned, but maintenance may run
    # as root). These calls only ever touch our own internal repos and clone
    # --local runs no hooks, so disabling the dubious-ownership guard here is safe.
    return subprocess.run(
        ["git", "-c", "safe.directory=*", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        # Never let a scheduler git call block on an interactive credential
        # prompt: the tick loop is headless, so a prompt would hang the run.
        # Same env as before (no scrub, no profile-home swap: the credential
        # `store` lives in the gateway's HOME), plus two overrides.
        # GIT_ALLOW_PROTOCOL overrides every protocol.* config, so a transport
        # enabled in a copied or planted config cannot run.
        env=build_subprocess_env(
            scrub_secrets=False,
            inherit_profile_home=False,
            extra={
                "GIT_TERMINAL_PROMPT": "0",
                **({"GIT_ALLOW_PROTOCOL": allow_protocol} if allow_protocol else {}),
            },
        ),
        **popen_kwargs,
    )


def _strip_url_credentials(url: str) -> str:
    """Remove an embedded ``user:secret@`` segment from an http(s) remote URL.

    A PAT baked into the source tree's origin (``https://x-access-token:TOKEN@
    github.com/owner/repo.git``) would otherwise be copied verbatim into every
    ephemeral clone's ``.git/config`` — plaintext token on disk per run. We keep
    the remote tokenless and let the credential helper (copied alongside) supply
    the token at push time. SSH (``git@…``), local paths, and already-tokenless
    URLs are returned unchanged.
    """
    for scheme in ("https://", "http://"):
        if url.startswith(scheme):
            rest = url[len(scheme):]
            at = rest.find("@")
            slash = rest.find("/")
            # Only the "@" before the first path "/" delimits userinfo.
            if at != -1 and (slash == -1 or at < slash):
                return scheme + rest[at + 1:]
            return url
    return url


# The fetch runs in the scheduler (gateway) process, whose env holds every
# service secret. The clone's credential helper is copied from the shared tree,
# which agents can write, so a `credential.helper=!cmd` planted there would run
# here with those secrets. Reset the helper list and use only the scheduler's
# own `store` (~/.git-credentials of the gateway's HOME).
_SCHEDULER_CREDENTIALS = ("-c", "credential.helper=", "-c", "credential.helper=store")
# The origin URL is copied from that tree too, so the fetch may use only these
# transports. git refuses `ext::` by default, but a `protocol.ext.allow` in a
# config would win over `-c protocol.allow=never`; GIT_ALLOW_PROTOCOL does not.
_FETCH_ALLOW_PROTOCOL = "https:ssh"
# `scheme://user:secret@` and GitHub token shapes in a git error message; scrubbed before logging.
_URL_USERINFO = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@")
_TOKEN_SHAPES = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")


def _sync_to_origin_default(job_id: str, ephemeral: str) -> Optional[str]:
    """Fetch origin and check out its default branch tip in the clone.

    Returns the branch name on success; on ANY failure logs a WARNING and
    returns None, leaving the clone on the source tree's HEAD (the
    pre-2026-11-18 behaviour). Never raises.
    """
    # The source tree's refs/remotes/origin/* are its LOCAL branches, and
    # ``git clone --local`` checks out its HEAD — so the clone is as stale as
    # the shared tree (2026-11-18: 682 commits behind, HEAD on an unpushed
    # branch). Fetching origin here is what makes the clone current.
    def _kept(step: str, reason) -> None:
        logger.warning(
            "Job '%s': isolated checkout kept the source tree's HEAD; could not sync to origin (%s): %s",
            job_id, step, _TOKEN_SHAPES.sub("***", _URL_USERINFO.sub(r"\1", str(reason)))[:300],
        )

    step = "fetch"
    try:
        for step, args, timeout in (
            ("fetch", [*_SCHEDULER_CREDENTIALS, "fetch", "--quiet", "--prune", "origin"], 120),
            ("set-head", [*_SCHEDULER_CREDENTIALS, "remote", "set-head", "origin", "--auto"], 60),
            ("symbolic-ref", ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], 60),
        ):
            res = _git(args, cwd=ephemeral, timeout=timeout, allow_protocol=_FETCH_ALLOW_PROTOCOL)
            if res.returncode != 0:
                return _kept(step, res.stderr.strip())
        head_ref = res.stdout.strip()
        branch = head_ref[len("origin/"):] if head_ref.startswith("origin/") else ""
        if not branch or branch.startswith("-"):
            return _kept(step, f"bad origin/HEAD: {head_ref!r}")
        step = "checkout"
        res = _git(["checkout", "--quiet", "-B", branch, f"origin/{branch}", "--"], cwd=ephemeral)
        if res.returncode != 0:
            return _kept(step, res.stderr.strip())
        return branch
    except (subprocess.SubprocessError, OSError) as e:
        # Timeouts land here too (TimeoutExpired is a SubprocessError): a sync
        # that fails must not escape, because the caller converts it into
        # IsolatedCheckoutError and the job fails closed.
        return _kept(step, e)


def _provision_isolated_checkout(
    job_id: str, profile: Optional[str], workdir: str
) -> tuple[str, Optional[str]]:
    """Provision an ephemeral local clone of ``workdir`` for this run.

    Returns ``(effective_workdir, cleanup_path)``. ``cleanup_path`` is the
    ephemeral dir to remove after the run, or ``None`` when no isolation was
    applied (kill-switch off, or workdir is not a git working tree) — in which
    case ``effective_workdir == workdir``.

    Raises :class:`IsolatedCheckoutError` when isolation is required but the
    clone cannot be produced (fail-closed — never run a write-agent on the shared
    tree).
    """
    if not _isolation_enabled():
        return workdir, None
    if not workdir or not _is_git_worktree(workdir):
        return workdir, None

    base = _checkout_base()
    try:
        os.makedirs(base, exist_ok=True)
    except OSError as e:
        raise IsolatedCheckoutError(f"checkout base {base!r} not usable: {e}") from e

    slug = "".join(c for c in (profile or "x") if c.isalnum() or c in "-_") or "x"
    ephemeral = tempfile.mkdtemp(prefix=f"{_CHECKOUT_PREFIX}{slug}-{job_id}-", dir=base)
    branch: Optional[str] = None

    try:
        # Local clone: hardlinks objects from the source — fast and disk-cheap.
        # Copies only COMMITTED state, so any dirty edits in the shared tree are
        # intentionally left behind.
        res = _git(["clone", "--local", "--quiet", workdir, ephemeral])
        if res.returncode != 0 and "cross-device" in (res.stderr or "").lower():
            # Checkout base is on a different filesystem than the source (e.g.
            # base on /tmp, source on the /opt/data volume). git's default
            # hardlinking can't span devices — retry copying the objects instead.
            # Co-locate the base with the source (HERMES_CRON_CHECKOUT_DIR) to keep
            # the fast hardlink path.
            shutil.rmtree(ephemeral, ignore_errors=True)
            res = _git(["clone", "--local", "--no-hardlinks", "--quiet", workdir, ephemeral])
        if res.returncode != 0:
            raise IsolatedCheckoutError(
                f"git clone --local failed ({res.returncode}): {res.stderr.strip()}"
            )

        # Point origin at the SOURCE tree's real remote (GitHub), so the agent's
        # push lands upstream rather than in the local clone. A local clone sets
        # origin to the source path otherwise.
        src_origin = _git(["remote", "get-url", "origin"], cwd=workdir)
        if src_origin.returncode == 0 and src_origin.stdout.strip():
            # Strip any embedded PAT so the clone's .git/config stays tokenless;
            # the copied credential helper (below) resolves auth at push time.
            clean_origin = _strip_url_credentials(src_origin.stdout.strip())
            _git(["remote", "set-url", "origin", clean_origin], cwd=ephemeral)

        # A fresh clone inherits no local config — copy commit identity and any
        # credential helper so commit+push behave exactly as on the shared tree.
        dumped = _git(["config", "--local", "--get-regexp", r"^(user|credential)\."], cwd=workdir)
        if dumped.returncode == 0:
            for line in dumped.stdout.splitlines():
                line = line.strip()
                if not line or " " not in line:
                    continue
                key, value = line.split(" ", 1)
                _git(["config", "--local", key, value], cwd=ephemeral)

        # The local clone carries the source tree's HEAD and its LOCAL branches,
        # so it is only as fresh as the shared tree. Fetch origin's default
        # branch and check out its tip instead — AFTER the config copy above,
        # because the fetch needs the copied credential helper.
        if src_origin.returncode == 0 and src_origin.stdout.strip():
            branch = _sync_to_origin_default(job_id, ephemeral)
    except IsolatedCheckoutError:
        shutil.rmtree(ephemeral, ignore_errors=True)
        raise
    except (subprocess.SubprocessError, OSError) as e:
        shutil.rmtree(ephemeral, ignore_errors=True)
        raise IsolatedCheckoutError(f"provisioning isolated checkout failed: {e}") from e

    logger.info(
        "Job '%s': isolated checkout %s (from %s, on %s)",
        job_id,
        ephemeral,
        workdir,
        branch or "source HEAD",
    )
    return ephemeral, ephemeral


def _cleanup_isolated_checkout(path: Optional[str]) -> None:
    """Remove an ephemeral checkout. Guarded: only ever under the checkout base."""
    if not path:
        return
    try:
        base = os.path.realpath(_checkout_base())
        real = os.path.realpath(path)
        under_base = os.path.commonpath([base, real]) == base
        is_ephemeral = os.path.basename(real).startswith(_CHECKOUT_PREFIX)
        if not (under_base and is_ephemeral):
            # Refuse to delete anything that isn't one of our ephemeral dirs.
            logger.warning("Refusing to clean non-ephemeral path %r", path)
            return
        shutil.rmtree(real, ignore_errors=True)
    except (OSError, ValueError) as e:
        logger.debug("Isolated checkout cleanup failed for %r: %s", path, e)


def _sweep_stale_checkouts(max_age_h: float = 6.0) -> None:
    """Best-effort reap of ephemeral checkouts leaked by crashed runs."""
    import time

    base = _checkout_base()
    try:
        entries = os.listdir(base)
    except OSError:
        return
    cutoff = time.time() - max_age_h * 3600
    for name in entries:
        if not name.startswith(_CHECKOUT_PREFIX):
            continue
        full = os.path.join(base, name)
        try:
            if os.path.isdir(full) and os.path.getmtime(full) < cutoff:
                shutil.rmtree(full, ignore_errors=True)
                logger.info("Swept stale isolated checkout %s", full)
        except OSError:
            continue
