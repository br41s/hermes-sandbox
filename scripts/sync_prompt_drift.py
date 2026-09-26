#!/usr/bin/env python3
"""Fan out a shared cron `.prompt` file's current content to every job that
references it, instead of syncing jobs one-by-one via `cronjob(action="update",
prompt_source=...)` in a chat.

Every cron job stores its own frozen copy of the prompt text at creation time
(see cron/jobs.py:create_job). `prompt_source` only records a path — editing
the repo file never reaches a running job on its own; `incidents/sweep.py`
just *detects* the drift afterward. This script closes that loop for every
job sharing a template in one pass, e.g. after editing
gap-hunter/bl-site-package-gap-hunter.prompt for all rented bl-site-package
customers at once.

Usage (must run with the repo's venv Python — the bare `python3` on PATH
won't have PyYAML and other deps this imports, e.g. via cron/jobs.py):
    # Preview what would change across every drifted job
    .venv/bin/python3 scripts/sync_prompt_drift.py --dry-run

    # Sync only jobs built from one template
    .venv/bin/python3 scripts/sync_prompt_drift.py --source gap-hunter/bl-site-package-gap-hunter.prompt

    # Apply without the confirmation prompt (e.g. from another script)
    .venv/bin/python3 scripts/sync_prompt_drift.py --yes

A job whose live prompt was edited in place since its last sync is reported as
blocked rather than overwritten (same guard as `cron/fork_ext/prompt_sync.py`);
--force overrides that.
"""

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
os.environ.setdefault("HERMES_HOME", os.path.join(os.path.expanduser("~"), ".hermes"))

from cron.fork_ext.prompt_sync import prompt_sha as _prompt_sha  # noqa: E402
from cron.jobs import list_jobs, update_job  # noqa: E402
from tools.cronjob_tools import _scan_cron_prompt  # noqa: E402


def _read_source(source: str) -> str:
    path = Path(REPO_ROOT) / source
    return path.read_text(encoding="utf-8")


def find_drift(sources: list[str] | None, *, force: bool = False) -> dict:
    """Group jobs with a prompt_source by drift status.

    Returns {"unchanged": [...], "changed": [...], "blocked": [...], "missing_file": [...]}
    keyed lists of (job, source, new_text_or_None, detail).

    Carries the same clobber guard as ``cron.fork_ext.prompt_sync``: a job whose
    live prompt no longer hashes to ``prompt_synced_sha`` was edited in place
    since its last sync, and syncing would destroy that edit. Those land in
    "blocked" so the rest of the sweep still runs. ``force`` skips the check.
    """
    result = {"unchanged": [], "changed": [], "blocked": [], "missing_file": []}
    for job in list_jobs(include_disabled=True):
        source = job.get("prompt_source")
        if not source:
            continue
        if sources and source not in sources:
            continue
        try:
            file_text = _read_source(source)
        except OSError as exc:
            result["missing_file"].append((job, source, None, str(exc)))
            continue
        live_text = job.get("prompt") or ""
        if file_text.strip() == live_text.strip():
            result["unchanged"].append((job, source, None, None))
            continue
        scan_error = _scan_cron_prompt(file_text)
        if scan_error:
            result["blocked"].append((job, source, None, scan_error))
            continue
        baseline = job.get("prompt_synced_sha")
        live_sha = _prompt_sha(live_text)
        if baseline and live_sha != baseline and not force:
            result["blocked"].append((job, source, None, (
                "the live prompt was edited since the last sync, so syncing would "
                f"overwrite that edit (live {live_sha[:12]}, last synced "
                f"{baseline[:12]}). Diff both sides: if the live edit is the fix, "
                "port it INTO the repo file and sync that; if the repo is genuinely "
                "newer, re-run with --force."
            )))
            continue
        # No baseline means this job has never been synced, so there is nothing to
        # judge the live side against. It proceeds and records one, but says so —
        # that is exactly the case an automated caller must not run unattended.
        note = None if baseline else "first sync — no baseline to check"
        result["changed"].append((job, source, file_text, note))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        action="append",
        dest="sources",
        help="Restrict to this prompt_source path (repeatable). Default: all sources in use.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would change, apply nothing")
    parser.add_argument("--yes", action="store_true", help="Apply without an interactive confirmation")
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Sync even jobs whose live prompt was edited since the last sync, "
            "overwriting that edit. Diff both sides first."
        ),
    )
    args = parser.parse_args()

    drift = find_drift(args.sources, force=args.force)

    for job, source, _, _ in drift["unchanged"]:
        print(f"  = {job['id']}  {job['name']!r}  (up to date with {source})")
    for job, source, _, detail in drift["missing_file"]:
        print(f"  ! {job['id']}  {job['name']!r}  prompt_source {source} unreadable: {detail}")
    for job, source, _, detail in drift["blocked"]:
        print(f"  x {job['id']}  {job['name']!r}  BLOCKED syncing from {source}: {detail}")
    for job, source, _, detail in drift["changed"]:
        note = f" ({detail})" if detail else ""
        print(f"  ~ {job['id']}  {job['name']!r}  drifted from {source} — will sync{note}")

    if not drift["changed"]:
        print("\nNothing to sync.")
        return 1 if drift["blocked"] else 0

    if args.dry_run:
        print(f"\n--dry-run: {len(drift['changed'])} job(s) would be updated.")
        return 0

    if not args.yes:
        reply = input(f"\nApply {len(drift['changed'])} update(s)? [y/N] ").strip().lower()
        if reply != "y":
            print("Aborted.")
            return 1

    for job, source, file_text, _ in drift["changed"]:
        update_job(job["id"], {
            "prompt": file_text,
            "prompt_source": source,
            # Without this the next cronjob(action='sync_prompt') compares the live
            # prompt against a baseline this script never advanced, reads its own
            # write as a hand-edit, and refuses.
            "prompt_synced_sha": _prompt_sha(file_text),
        })
        print(f"  synced {job['id']} ({job['name']!r}) from {source}")

    if drift["blocked"]:
        print(f"\n{len(drift['blocked'])} job(s) blocked — see the reasons above, fix and re-run.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
