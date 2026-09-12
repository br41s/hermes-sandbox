"""Regression lock for the 2026-09-12 auditor runaway (job ``c19bb95c0a62``).

Three consecutive production runs burned the full 90-iteration agent budget
(~38 min each, ~130k cached input tokens per call) and posted **zero** reviews.
Evidence from the session store: 47-52 ``auditor.pending`` invocations per run,
24-33 commands rejected as "script execution via -e/-c flag", 0 ``gh pr
comment`` calls.

Two independent defects produced that:

1. ``auditor.prompt`` PASO 2b tiered a PR with ``python -c "...classify..."``.
   ``tools/approval.py`` blocks that pattern unconditionally under cron (no
   human to approve), and ``auditor/tiers.py`` had no ``__main__``, so there
   was NO reachable way to tier. The agent could not leave step 2b and looped
   back to PASO 1 until the iteration cap.
2. ``auditor.pending`` printed ``gh``'s full per-file blob. The JSON overran
   the terminal output cap and came back truncated mid-array, which is the
   reason the agent itself gave for re-running a command the prompt says to
   call exactly once.

Each test below fails against the pre-fix code.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import auditor.pending as pending
import auditor.tiers as tiers
from tests.auditor.test_pending import _patch_repos, _pr

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT = REPO_ROOT / "auditor" / "auditor.prompt"

# tools/approval.py:853 — the exact matcher cron applies, with no approver.
BLOCKED_INLINE_CODE = r"(python[23]?|perl|ruby|node)\s+-[ec]\s+"


# --- defect 1: tiering must be reachable without python -c ------------------

def test_tiers_has_a_module_cli():
    """``python -m auditor.tiers`` must work — it is the only tiering path a
    cron agent has, because ``python -c`` is blocked before it ever runs."""
    out = subprocess.run(
        [sys.executable, "-m", "auditor.tiers", "hermes/x.py", "--repo", pending.ENGINE_REPO],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "system"


def test_tiers_cli_reads_stdin_and_honours_repo():
    out = subprocess.run(
        [sys.executable, "-m", "auditor.tiers", "--repo", "br41s/biglobster"],
        cwd=REPO_ROOT, input="site/blog/post.html\n", capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "content"


def test_prompt_never_tells_the_agent_to_run_inline_code():
    """The prompt must not instruct a command cron rejects 100% of the time.

    This is the defect itself: PASO 2b's ``python -c`` was unrunnable, so the
    agent never got a tier and never reached the review step.
    """
    import re
    hits = re.findall(BLOCKED_INLINE_CODE, PROMPT.read_text(encoding="utf-8"))
    assert not hits, f"prompt tells the cron agent to run blocked inline code: {hits}"


def test_pending_tiers_every_pr_in_process(monkeypatch, tmp_path):
    """With the tier already computed, the agent needs no shell call at all."""
    _patch_repos(monkeypatch, {
        pending.ENGINE_REPO: [_pr(1, "aaa", files=("hermes/x.py",))],
        "br41s/biglobster": [_pr(2, "bbb", files=("site/blog/p.html",))],
    })
    out = {p["repo"]: p["tier"] for p in pending.pending_prs(None, tmp_path / "s.json")}
    assert out == {pending.ENGINE_REPO: "system", "br41s/biglobster": "content"}


# --- defect 2: the queue payload must never be truncated --------------------

def _cli(monkeypatch, capsys, tmp_path, argv):
    monkeypatch.setattr(pending, "_state_path", lambda: tmp_path / "state.json")
    assert pending.main(argv) == 0
    return capsys.readouterr().out


def test_cli_payload_drops_the_per_file_blob(monkeypatch, capsys, tmp_path):
    _patch_repos(monkeypatch, {pending.ENGINE_REPO: [_pr(1, "aaa")]})
    payload = json.loads(_cli(monkeypatch, capsys, tmp_path, []))
    pr = payload["prs"][0]
    assert "files" not in pr, "raw gh per-file blob leaked into the agent payload"
    assert pr["tier"] == "system"
    assert pr["author"] == "claude-code"
    assert pr["headRefOid"] == "aaa"


def test_cli_payload_stays_under_the_terminal_output_cap(monkeypatch, capsys, tmp_path):
    """The whole point: a realistic queue must arrive parseable, not truncated.

    Sized against the smallest cap seen in production (13,908 chars observed on
    the auditor profile), not the 50,000 default — the profile config is what
    actually truncated the run.
    """
    from tools.tool_output_limits import get_max_bytes

    observed_profile_cap = 13_908
    big = [_pr(n, f"sha{n}", files=tuple(f"src/mod/file_{i}.py" for i in range(400)))
           for n in range(1, 41)]
    _patch_repos(monkeypatch, {pending.ENGINE_REPO: big})
    out = _cli(monkeypatch, capsys, tmp_path, [])
    assert len(out) < observed_profile_cap, f"payload is {len(out)} chars — would truncate"
    assert len(out) < get_max_bytes()
    json.loads(out)  # must parse — a truncated array is what broke the run


def test_cli_caps_prs_per_run_and_reports_the_remainder(monkeypatch, capsys, tmp_path):
    """An uncapped queue cannot finish inside ``max_iterations`` (90). Cap it,
    and say out loud what was deferred so the run is not silently partial."""
    _patch_repos(monkeypatch, {pending.ENGINE_REPO: [_pr(n, f"s{n}") for n in range(1, 26)]})
    payload = json.loads(_cli(monkeypatch, capsys, tmp_path, ["--limit", "4"]))
    assert payload["count"] == 4
    assert payload["total_pending"] == 25
    assert payload["deferred_to_next_run"] == 21
    assert len(payload["prs"]) == 4


def test_default_limit_fits_the_agent_iteration_budget():
    """~6-8 iterations per PR against ``run_agent`` ``max_iterations=90``."""
    import re
    src = (REPO_ROOT / "run_agent.py").read_text(encoding="utf-8")
    max_iters = int(re.search(r"max_iterations: int = (\d+)", src).group(1))
    assert pending.DEFAULT_LIMIT * 8 < max_iters, (
        f"limit {pending.DEFAULT_LIMIT} x 8 iterations exceeds the {max_iters} cap"
    )


def test_queue_is_oldest_first_so_a_pr_cannot_starve(monkeypatch, tmp_path):
    """Under a cap, arrival order decides who gets reviewed. Ascending PR number
    means an old PR is never pushed out by a stream of newer ones."""
    _patch_repos(monkeypatch, {
        "br41s/biglobster": [_pr(9, "i"), _pr(3, "c")],
        pending.ENGINE_REPO: [_pr(7, "g"), _pr(2, "b")],
    })
    out = pending.pending_prs(None, tmp_path / "s.json")
    assert [(p["repo"], p["number"]) for p in out] == [
        ("br41s/biglobster", 3), ("br41s/biglobster", 9),
        (pending.ENGINE_REPO, 2), (pending.ENGINE_REPO, 7),
    ]


def test_raw_escape_hatch_still_returns_full_objects(monkeypatch, capsys, tmp_path):
    _patch_repos(monkeypatch, {pending.ENGINE_REPO: [_pr(1, "aaa")]})
    raw = json.loads(_cli(monkeypatch, capsys, tmp_path, ["--raw"]))
    assert raw[0]["files"] == [{"path": "hermes/x.py"}]
