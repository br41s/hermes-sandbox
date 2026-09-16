"""Regression lock for the judge's non-pipe entry point (auditor/llm.py).

The defect this closes: auditor.prompt PASO 2d told the agent to pipe the rubric
and diff into `python -m auditor.llm`. Cron runs with approvals.cron_mode: deny,
and the Tirith scanner blocks every pipe into an interpreter with no user present
to approve it — so the judge call was refused on every run. Content merges and
system approvals were being decided by the orchestrator model alone, silently,
and the per-tier model split was inert.

So the load-bearing properties are: the command PASO 2d actually contains must
pass the security scanner, the --repo path must never touch stdin, and an
unfetchable or empty PR must be a loud error rather than a blank review — a judge
handed nothing approves everything.
"""
import json
import re
from pathlib import Path

import pytest

import auditor.llm as llm

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT = REPO_ROOT / "auditor" / "auditor.prompt"


def _fake_gh(mapping):
    """Replace llm._gh with a lookup keyed on the gh subcommand ('view'/'diff')."""
    def _gh(args, timeout=120):
        return mapping.get(args[1])
    return _gh


META = json.dumps({"title": "Fix the thing", "body": "why", "additions": 3,
                   "deletions": 1, "changedFiles": 2})


def test_paso_2d_command_passes_the_cron_security_scanner():
    """The bug in one assertion: the command in the prompt must be allowed."""
    from tools.tirith_security import check_command_security
    text = PROMPT.read_text(encoding="utf-8")
    # Match the WHOLE line any auditor.llm invocation sits on, so a reintroduced
    # `printf ... | python -m auditor.llm` is caught as a pipe rather than slipping
    # past a regex anchored on `python`.
    cmds = [ln.strip() for ln in text.splitlines() if "python -m auditor.llm" in ln]
    assert cmds, "PASO 2d no longer invokes auditor.llm — update this test"
    for cmd in cmds:
        concrete = (cmd.replace("<tier>", "system")
                       .replace("<repo>", "br41s/hermes-sandbox")
                       .replace("<number>", "247"))
        assert "|" not in concrete, (
            f"PASO 2d pipes into the interpreter again — cron blocks this: {concrete}")
        verdict = check_command_security(concrete).get("action")
        assert verdict == "allow", f"cron would refuse PASO 2d: {verdict} for {concrete}"


def test_repo_path_never_reads_stdin(monkeypatch, capsys):
    monkeypatch.setattr(llm, "_gh", _fake_gh({"view": META, "diff": "diff --git a/x b/x\n+1\n"}))
    monkeypatch.setattr(llm, "review", lambda tier, content, **kw: "APPROVE")

    class Exploding:
        def read(self):
            raise AssertionError("--repo path must not read stdin (cron has none)")
    monkeypatch.setattr(llm.sys, "stdin", Exploding())

    assert llm.main(["--tier", "system", "--repo", "o/n", "--number", "5"]) == 0
    assert "APPROVE" in capsys.readouterr().out


def test_repo_and_number_must_come_together():
    assert llm.main(["--tier", "system", "--repo", "o/n"]) == 2
    assert llm.main(["--tier", "system", "--number", "5"]) == 2


def test_unfetchable_pr_is_an_error_not_a_blank_review(monkeypatch):
    monkeypatch.setattr(llm, "_gh", _fake_gh({"view": None, "diff": None}))
    monkeypatch.setattr(llm, "review",
                        lambda *a, **k: pytest.fail("must not judge an unfetchable PR"))
    assert llm.main(["--tier", "system", "--repo", "o/n", "--number", "5"]) == 3


def test_empty_diff_refuses_to_judge_nothing(monkeypatch):
    monkeypatch.setattr(llm, "_gh", _fake_gh({"view": META, "diff": "   \n"}))
    text, err = llm.fetch_pr_content("o/n", 5)
    assert text is None
    assert "empty diff" in err


def test_unreadable_metadata_is_an_error(monkeypatch):
    monkeypatch.setattr(llm, "_gh", _fake_gh({"view": "not json", "diff": "d"}))
    text, err = llm.fetch_pr_content("o/n", 5)
    assert text is None and "unreadable" in err


def test_composed_review_carries_rubric_title_and_diff(monkeypatch):
    monkeypatch.setattr(llm, "_gh", _fake_gh({"view": META, "diff": "diff --git a/x b/x"}))
    text, err = llm.fetch_pr_content("o/n", 5)
    assert err is None
    assert "BLOCK or APPROVE" in text        # rubric travels with the request
    assert "o/n#5 — Fix the thing" in text   # the judge knows what it is reading
    assert "diff --git a/x b/x" in text


def test_oversized_diff_is_truncated_and_says_so(monkeypatch):
    big = "x" * (llm._MAX_DIFF_CHARS + 5000)
    monkeypatch.setattr(llm, "_gh", _fake_gh({"view": META, "diff": big}))
    text, err = llm.fetch_pr_content("o/n", 5)
    assert err is None
    assert "TRUNCATED" in text, "silent truncation would have the judge grade a fragment"
    assert len(text) < len(big) + 5000
