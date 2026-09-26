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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from hermes_cli._subprocess_compat import windows_hide_flags

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
# running close together can never clobber each other's uncommitted work. A fresh
# clone always starts from clean committed origin/main, so prior-run leftovers
# vanish too. The per-URL atomic commit+push-to-main flow is unchanged — it just
# happens inside the clone.
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


def _git(args: list, cwd: Optional[str] = None, timeout: int = 300) -> subprocess.CompletedProcess:
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
    except IsolatedCheckoutError:
        shutil.rmtree(ephemeral, ignore_errors=True)
        raise
    except (subprocess.SubprocessError, OSError) as e:
        shutil.rmtree(ephemeral, ignore_errors=True)
        raise IsolatedCheckoutError(f"provisioning isolated checkout failed: {e}") from e

    logger.info("Job '%s': isolated checkout %s (from %s)", job_id, ephemeral, workdir)
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
