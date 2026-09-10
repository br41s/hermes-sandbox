"""Merge system-tier PRs the auditor approved, once the CEO has labelled them.

The last manual step of the auditor's system-tier flow: the auditor reviews and
posts an APPROVE comment carrying a head-SHA marker; the CEO adds the
``auto-merge`` label; this watcher merges. See ``watcher.py`` for the full gate.

A sibling of the incident watcher, not a remediation class: a healthy PR is not
an incident, so it does not belong in that registry or in the incident brief.
What it *does* share is the governance — kill switch, per-commit debounce,
per-class rate limit and the append-only ledger all come from
``remediation.guards`` / ``remediation.ledger``.

Runs as a ``no_agent`` cron job under the ``auditor`` profile (cont-init §5e
seeds the wrapper). Stdout is the report; empty stdout is silence.
"""

from merge_on_green.watcher import AUDITOR_LOGIN, CLASS_NAME, LABEL, run

__all__ = ["AUDITOR_LOGIN", "CLASS_NAME", "LABEL", "run"]
