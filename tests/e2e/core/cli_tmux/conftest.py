"""Fork-only: known-flaky upstream e2e tests, marked non-strict xfail (they still run).

A non-strict xfail keeps the test running and its output in the log, but a red run no longer
fails the e2e lane, so it stops blocking unrelated PRs and the auditor's CI gate. Each entry
names the measured failure rate and the upstream issue. Remove the entry when upstream fixes it.

This file is ours, not upstream's (upstream has no conftest in this directory), so it adds
nothing to the monthly upstream merge.
"""

from __future__ import annotations

import pytest

_KNOWN_FLAKY = {
    "test_resize_scrollback.py::test_resizes_keep_each_transcript_line_once_in_tmux_scrollback": (
        "upstream render race on a two-step shrink mid-stream: 6 of 60 CI runs duplicated 7-8 "
        "transcript words (2026-11-16); NousResearch/hermes-agent#156776"
    ),
}


def pytest_collection_modifyitems(config, items):
    for item in items:
        for suffix, reason in _KNOWN_FLAKY.items():
            if item.nodeid.endswith(suffix):
                item.add_marker(pytest.mark.xfail(reason=reason, strict=False))
