"""CLI entrypoint: ``python -m merge_on_green [--dry-run] [--verbose] [repo ...]``.

Stdout is the cron job's delivered message, so it stays empty on a quiet tick.
Exit status is 0 whenever the watcher itself ran — a PR that was not merged is
a normal outcome, not a failure of the watcher.
"""

from __future__ import annotations

import argparse
import sys

from merge_on_green.watcher import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="merge_on_green",
        description="Merge auditor-approved, auto-merge-labelled PRs under the Hermes autonomy gates.",
    )
    parser.add_argument("repos", nargs="*",
                        help="owner/repo (default: the auditor's review set)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be merged; never merges, never writes the ledger")
    parser.add_argument("--verbose", action="store_true",
                        help="also report PRs that are waiting, and repeat already-reported outcomes")
    args = parser.parse_args(argv)

    for line in run(args.repos or None, dry_run=args.dry_run, verbose=args.verbose):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
