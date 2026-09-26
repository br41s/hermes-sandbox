"""Fork-owned extensions to the ``hermes cron`` CLI.

Everything the fork adds to ``hermes cron`` handling lives here; the upstream
file keeps one-line call sites only. (The parser half — flags and the
``sync-prompt`` sub-parser — is ``hermes_cli/subcommands/cron_fork_ext.py``,
kept out of this package so building the CLI parser never imports ``cron``.)

- ``hermes_cli/cron.py`` calls :func:`print_list_rows`, :func:`job_api_kwargs`,
  :func:`print_kickoff_ping`, :func:`print_job_details` and
  :func:`reap_if_wedged`, and dispatches :data:`SUBCOMMANDS` before its own.

The shapes mirror upstream v2026.9.24's tables so re-anchoring after the merge
is one line each: ``_JOB_ARG_FIELDS += JOB_ARG_FIELDS`` and
``_CRON_SUBCOMMANDS.update(SUBCOMMANDS)``.

``hermes_cli.cron`` is imported lazily inside each function: it imports this
module at load time, and late binding keeps monkeypatches of its
``_cron_api`` / ``_job_action`` effective here.
"""

import logging
import os
import sys
from typing import Any, Dict

from hermes_cli.colors import Colors, color

# ------------------------------------------------------- create / edit args

# (cronjob kwarg, argparse attr) — same shape as upstream's _JOB_ARG_FIELDS.
# Absent attrs read as None, which the cronjob tool treats as "not given", so
# `create` (which has no --prompt-source / --progress-ping) passes them harmlessly.
JOB_ARG_FIELDS = (
    ("profile", "profile"),
    ("progress_ping", "progress_ping"),
    ("prompt_source", "prompt_source"),
)


def job_api_kwargs(args) -> Dict[str, Any]:
    """The fork's extra ``cronjob`` kwargs for ``cron create`` / ``cron edit``."""
    return {api_key: getattr(args, attr, None) for api_key, attr in JOB_ARG_FIELDS}


# ------------------------------------------------------------------ output


def print_list_rows(job: Dict[str, Any]) -> bool:
    """Print the fork's ``cron list`` rows for *job*, right after Workdir.

    Returns True when the job's last run was interrupted and its "Last run"
    lines were printed here, so the caller must skip its own.
    """
    profile = job.get("profile")
    if profile:
        print(f"    Profile:   {profile}")
    if job.get("last_status") != "interrupted":
        return False
    # last_run_at still points at the last run that actually COMPLETED,
    # so don't pair it with this status — print the two clocks apart or
    # the line reads as "the 09:25 run was interrupted", which is wrong.
    print(f"    Last run:  {job.get('last_run_at') or 'never'}  (completed)")
    print(
        f"    {color('⚠ Interrupted:', Colors.RED)} "
        f"{job.get('last_interrupted_at', '?')} — killed before it finished; "
        f"not retried"
    )
    print(f"      inspect: hermes cron runs {job.get('id', '?')}")
    return True


def print_kickoff_ping(job: Dict[str, Any]) -> None:
    """``cron edit``'s kickoff-ping line; printed before Workdir."""
    if job.get("progress_ping") is False:
        print("  Kickoff ping: off (silent on start)")


def print_job_details(job: Dict[str, Any]) -> None:
    """The fork's detail lines for ``cron create`` / ``cron edit``; printed after Workdir."""
    if job.get("profile"):
        print(f"  Profile: {job['profile']}")
    if job.get("prompt_source"):
        print(f"  Prompt source: {job['prompt_source']}")


# ------------------------------------------------------------- sub-commands


def cron_sync_prompt(args) -> int:
    from hermes_cli import cron as cron_cli

    result = cron_cli._cron_api(
        action="sync_prompt",
        job_id=args.job_id,
        prompt_source=getattr(args, "prompt_source", None),
    )
    if not result.get("success"):
        print(color(f"Failed to sync prompt: {result.get('error', 'unknown error')}", Colors.RED))
        return 1
    verb = "Synced" if result.get("changed") else "Already up to date"
    print(color(f"{verb}: {result.get('message', '')}", Colors.GREEN))
    return 0


def cron_run(args) -> int:
    """``hermes cron run``: upstream's run, then reap any wedged agent thread."""
    from hermes_cli import cron as cron_cli

    return exit_hard_if_threads_abandoned(cron_cli._job_action("run", args.job_id, "Triggered"))


# Consulted before upstream's own dispatch, so "run" here overrides upstream's.
SUBCOMMANDS = {
    "sync-prompt": cron_sync_prompt,
    "sync_prompt": cron_sync_prompt,
    "run": cron_run,
}


# ------------------------------------------------------- wedged-run reaping


def reap_if_wedged(action: str) -> None:
    """Called by ``_job_action`` right after the cronjob call, BEFORE any printing.

    A wedged run can leave stdout as a pipe nobody reads — an operator's
    `zeabur service exec` that dropped, a closed terminal — and once its
    buffer fills, print() blocks forever. That stranded the very process
    this exit exists to reap: observed 2026-09-22, main thread parked in
    process_bootstrap.write() at the "Triggered job:" line while the run had
    already been recorded failed.
    """
    if action == "run":
        exit_hard_if_threads_abandoned(0)


def exit_hard_if_threads_abandoned(rc: int) -> int:
    """Terminate instead of hanging when a wedged agent thread was abandoned.

    ``hermes cron run`` executes the agent IN THIS PROCESS (``_cron_api`` ->
    ``cronjob_tool``), so an inactivity timeout leaves a live thread here that
    nothing can stop. ``concurrent.futures`` then joins it at interpreter exit,
    so returning normally hangs forever: the run is reported, the failure is
    delivered, and the process still sits there holding ~280 MB.

    That is how four orphans accumulated on 2026-09-22 — the oldest 1h32m,
    on a container down to 213 MB free. They had to be killed by hand.

    The work is finished by the time this runs: the run record is written and
    the failure delivered. Only the wedged thread remains, and it will never
    make progress, so exiting is strictly better than waiting for it.

    ``os._exit`` skips atexit deliberately — the atexit hook is the thing that
    hangs. Buffers are flushed first, by hand, since nothing else will.
    """
    try:
        from cron.scheduler import abandoned_agent_threads

        stuck = abandoned_agent_threads()
    except Exception:
        return rc
    if not stuck:
        return rc

    # Same hazard as the caller's prints: if stdout is a pipe with no reader
    # this message would hang and defeat the whole point. Non-blocking means a
    # partial or dropped line, which is the right trade when the alternative is
    # a process that never exits.
    try:
        os.set_blocking(sys.stdout.fileno(), False)
        os.set_blocking(sys.stderr.fileno(), False)
    except Exception:
        pass
    try:
        print(color(
            f"  {stuck} agent thread(s) wedged and cannot be stopped — exiting "
            f"so this process does not linger holding memory.", Colors.YELLOW,
        ))
    except Exception:
        pass
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        logging.shutdown()
    except Exception:
        pass
    os._exit(rc)
