"""Merge pull requests that are labelled, green, and clear of protected paths.

A sibling of the incident watcher, not a remediation class: a healthy PR is not
an incident, so it does not belong in that registry or in the incident brief.
What it *does* share is the governance — kill switch, per-signature debounce,
per-class rate limit and the append-only ledger all come from
``remediation.guards`` / ``remediation.ledger``, so every merge lands in the same
audit trail as every remediation act.

Run it as a ``no_agent`` cron job. Stdout is the report and empty stdout is
silence, so a quiet tick delivers nothing.
"""

from merge_on_green.watcher import CLASS_NAME, LABEL, run

__all__ = ["CLASS_NAME", "LABEL", "run"]
