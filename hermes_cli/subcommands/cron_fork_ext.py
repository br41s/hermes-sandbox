"""Fork-owned additions to the ``hermes cron`` parser.

``build_cron_parser`` calls :func:`extend_parser` once, right after the
``remove`` sub-parser, so every flag and sub-command lands in the same help
position it always had. The handlers live in ``cron/fork_ext/cli.py``.

This is deliberately NOT under ``cron/``: the parser is built on every
``hermes`` invocation, and importing anything in the ``cron`` package runs
``cron/__init__`` — which loads the scheduler, asyncio and sqlite3 (~70
modules) into commands that never touch cron. Keep this module import-free.
"""

from __future__ import annotations


def extend_parser(cron_subparsers, *, create, edit) -> None:
    """Add the fork's flags to ``create``/``edit`` and the ``sync-prompt`` and ``move`` sub-commands."""
    create.add_argument(
        "--profile",
        help="Hermes profile name to run the job under. Use 'default' for the root profile. Named profiles must already exist. Omit to preserve the scheduler's existing profile.",
    )

    edit.add_argument(
        "--profile",
        help="Hermes profile name to run the job under. Use 'default' for the root profile. Pass empty string to clear.",
    )
    edit.add_argument(
        "--prompt-source",
        help=(
            "Repo-relative path to this job's canonical .prompt file (e.g. "
            "'gap-hunter/biglobster-gap-hunter.prompt'). Opts the job into "
            "incidents.sweep's prompt-drift watch, which alerts when the live "
            "prompt diverges from this file. Pass empty string to clear."
        ),
    )
    edit_ping = edit.add_mutually_exclusive_group()
    edit_ping.add_argument(
        "--no-progress-ping",
        dest="progress_ping",
        action="store_const",
        const=False,
        default=None,
        help="Silence this job's '🔄 Started' kickoff ping (for monitor crons that must stay quiet on start).",
    )
    edit_ping.add_argument(
        "--progress-ping",
        dest="progress_ping",
        action="store_const",
        const=True,
        help="Force this job's kickoff ping on, regardless of the global cron.progress_pings default.",
    )

    sync_prompt = cron_subparsers.add_parser(
        "sync-prompt",
        aliases=["sync_prompt"],
        help="Push a job's repo .prompt file into its live prompt",
    )
    sync_prompt.add_argument("job_id", help="Job ID to sync")
    sync_prompt.add_argument(
        "--prompt-source",
        help=(
            "Repo-relative path to the .prompt file to sync from. Defaults to "
            "the job's existing prompt_source field if omitted."
        ),
    )

    move = cron_subparsers.add_parser(
        "move",
        help="Move jobs between the default store and a profile's own store (dry run unless --apply)",
    )
    move.add_argument("job_ids", nargs="+", metavar="job_id",
                      help="Job id(s); every context_from edge must move in the same call")
    move.add_argument("--to-profile", help="Move from the default store into this profile's own store")
    move.add_argument("--to-default", action="store_true",
                      help="Move back into the default store (needs --from-profile)")
    move.add_argument("--from-profile", help="The profile store a --to-default move leaves")
    move.add_argument("--apply", action="store_true", help="Perform the move; without it, only report")
    move.add_argument(
        "--webhook-route-disabled", action="store_true",
        help="Confirm every webhook route that fires these jobs is disabled for the move window",
    )
