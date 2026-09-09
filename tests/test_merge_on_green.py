"""Tests for the merge-on-green watcher.

Every path that could merge something is exercised, plus every path that must
refuse. The bias throughout is fail-closed: when in doubt, do not merge.
"""

from __future__ import annotations

import base64
import json

import pytest

from merge_on_green import watcher
from remediation import ledger


# ---------------------------------------------------------------- fake gh ---

class FakeGh:
    """Minimal ``gh`` stand-in. Records merges instead of performing them."""

    def __init__(self, *, prs=None, checks=None, files=None, protected=None,
                 fail=None):
        self.prs = prs if prs is not None else []
        self.checks = checks if checks is not None else []
        self.files = files if files is not None else ["backend/app/routes.py"]
        self.protected = protected  # None => 404 => defaults
        self.fail = fail or {}
        self.merged: list[str] = []

    def __call__(self, args):
        args = list(args)
        key = " ".join(args[:2])
        if key in self.fail:
            raise watcher.GhError(self.fail[key])

        if args[:2] == ["pr", "list"]:
            return json.dumps(self.prs)
        if args[:2] == ["pr", "checks"]:
            return json.dumps(self.checks)
        if args[:2] == ["pr", "diff"]:
            return "\n".join(self.files)
        if args[:2] == ["pr", "merge"]:
            self.merged.append(args[2])
            return ""
        if args[:1] == ["api"]:
            if self.protected is None:
                raise watcher.GhError("Not Found (HTTP 404)")
            return json.dumps(base64.b64encode(self.protected.encode()).decode())
        raise AssertionError(f"unexpected gh call: {args}")


def _pr(number=7, sha="abc123def456", draft=False):
    return {"number": number, "isDraft": draft, "headRefOid": sha, "title": "t"}


def _green(n=2):
    return [{"name": f"c{i}", "state": "SUCCESS"} for i in range(n)]


@pytest.fixture
def led(tmp_path):
    return tmp_path / "ledger.jsonl"


def _run(gh, led, **kw):
    kw.setdefault("env", {})
    return watcher.process_repo("br41s/demo", runner=gh, ledger_path=led, **kw)


# ------------------------------------------------------------ path guarding --

@pytest.mark.parametrize("pattern,path,expected", [
    (".github/**", ".github/workflows/ci.yml", True),
    (".github/**", "notgithub/workflows/ci.yml", False),
    ("**/Dockerfile", "backend/Dockerfile", True),
    ("**/Dockerfile", "Dockerfile", True),
    ("Dockerfile", "backend/Dockerfile", False),
    ("cloudbuild.yaml", "cloudbuild.yaml", True),
    ("**/requirements*.txt", "backend/requirements-dev.txt", True),
    ("**/requirements*.txt", "backend/app/requirements_helper.py", False),
    ("*.md", "docs/x.md", False),          # lone * must not cross a separator
    ("**/*.md", "docs/x.md", True),
])
def test_glob_semantics(pattern, path, expected):
    assert bool(watcher.glob_to_regex(pattern).match(path)) is expected


def test_parse_protected_ignores_comments_and_blanks():
    parsed = watcher.parse_protected("# c\n\n  \n.github/**\ncloudbuild.yaml\n")
    assert [raw for raw, _ in parsed] == [".github/**", "cloudbuild.yaml"]


def test_blocked_paths_reports_every_hit_regardless_of_position():
    pats = watcher.parse_protected(".github/**\ncloudbuild.yaml")
    hits = watcher.blocked_paths(
        ["cloudbuild.yaml", "README.md", "a.py", ".github/x.yml"], pats
    )
    assert {p for p, _ in hits} == {"cloudbuild.yaml", ".github/x.yml"}


def test_missing_protected_file_falls_back_to_defaults_not_to_nothing():
    gh = FakeGh(protected=None)  # 404
    pats = watcher.protected_patterns("br41s/demo", runner=gh)
    assert watcher.blocked_paths([".github/workflows/x.yml"], pats)


def test_empty_protected_file_still_defends_ci():
    gh = FakeGh(protected="# nothing but comments\n")
    pats = watcher.protected_patterns("br41s/demo", runner=gh)
    assert watcher.blocked_paths([".github/workflows/x.yml"], pats)


# ------------------------------------------------------------ check reading --

def test_zero_checks_is_not_green():
    gh = FakeGh(checks=[])
    ok, detail = watcher.checks_green("br41s/demo", 1, runner=gh)
    assert ok is False and "no checks" in detail


def test_pending_check_is_not_green():
    gh = FakeGh(checks=[{"name": "ci", "state": "PENDING"}])
    ok, detail = watcher.checks_green("br41s/demo", 1, runner=gh)
    assert ok is False and "ci=PENDING" in detail


def test_unknown_state_blocks():
    gh = FakeGh(checks=[{"name": "ci", "state": "WEIRD"}])
    assert watcher.checks_green("br41s/demo", 1, runner=gh)[0] is False


def test_skipped_and_neutral_do_not_block():
    gh = FakeGh(checks=[{"name": "a", "state": "SUCCESS"},
                        {"name": "b", "state": "SKIPPED"},
                        {"name": "c", "state": "NEUTRAL"}])
    assert watcher.checks_green("br41s/demo", 1, runner=gh)[0] is True


def test_unreadable_checks_block():
    gh = FakeGh(fail={"pr checks": "boom"})
    assert watcher.checks_green("br41s/demo", 1, runner=gh)[0] is False


# -------------------------------------------------------------- happy path --

def test_merges_when_labelled_green_and_clean(led):
    gh = FakeGh(prs=[_pr()], checks=_green())
    out = _run(gh, led)
    assert gh.merged == ["7"]
    assert out and "merged" in out[0]
    entries = ledger.read(path=led)
    assert [e.event for e in entries] == [ledger.EVENT_APPLIED]
    assert entries[0].outcome == ledger.OUTCOME_SUCCESS


def test_silent_when_nothing_to_do(led):
    assert _run(FakeGh(prs=[]), led) == []


def test_draft_is_ignored(led):
    gh = FakeGh(prs=[_pr(draft=True)], checks=_green())
    assert _run(gh, led) == [] and gh.merged == []


def test_unlabelled_prs_never_reach_us(led):
    # `gh pr list --label` does the filtering; assert we ask for it.
    seen = {}

    def runner(args):
        if list(args[:2]) == ["pr", "list"]:
            seen["args"] = list(args)
            return "[]"
        raise AssertionError

    _run(runner, led)
    assert "--label" in seen["args"]
    assert watcher.LABEL in seen["args"]


# ------------------------------------------------------------- refusals -----

def test_protected_path_blocks_merge_and_is_recorded(led):
    gh = FakeGh(prs=[_pr()], checks=_green(),
                files=["README.md", ".github/workflows/ci.yml"],
                protected=".github/**")
    out = _run(gh, led)
    assert gh.merged == []
    assert "needs a human" in out[0] and ".github/workflows/ci.yml" in out[0]
    entries = ledger.read(path=led)
    assert entries[0].outcome == ledger.OUTCOME_FAILURE


def test_not_green_stays_silent_but_does_not_merge(led):
    gh = FakeGh(prs=[_pr()], checks=[{"name": "ci", "state": "FAILURE"}])
    assert _run(gh, led) == []
    assert gh.merged == []


def test_not_green_is_explained_when_verbose(led):
    gh = FakeGh(prs=[_pr()], checks=[{"name": "ci", "state": "FAILURE"}])
    out = _run(gh, led, verbose=True)
    assert "waiting" in out[0] and "ci=FAILURE" in out[0]


def test_empty_diff_refuses(led):
    gh = FakeGh(prs=[_pr()], checks=_green(), files=[])
    out = _run(gh, led)
    assert gh.merged == [] and "refusing" in out[0]


def test_kill_switch_blocks_and_says_so(led):
    gh = FakeGh(prs=[_pr()], checks=_green())
    out = _run(gh, led, env={"HERMES_AUTONOMY": "paused"})
    assert gh.merged == []
    assert guards_reason(out[0]) == "killswitch"


def guards_reason(line: str) -> str:
    return line.rsplit("— ", 1)[-1].strip()


def test_debounce_suppresses_a_second_tick_on_the_same_commit(led):
    gh = FakeGh(prs=[_pr()], checks=_green())
    assert _run(gh, led) and gh.merged == ["7"]
    # Same PR, same head SHA, next tick: silent, and no second merge.
    gh2 = FakeGh(prs=[_pr()], checks=_green())
    assert _run(gh2, led) == []
    assert gh2.merged == []


def test_a_new_commit_is_a_new_opportunity(led):
    gh = FakeGh(prs=[_pr(sha="aaaaaaaaaaaa")], checks=_green())
    _run(gh, led)
    gh2 = FakeGh(prs=[_pr(sha="bbbbbbbbbbbb")], checks=_green())
    _run(gh2, led)
    assert gh2.merged == ["7"], "a fresh head SHA must not be debounced"


def test_rate_limit_trips_and_is_reported(led):
    for i in range(ledger.RATE_MAX_PER_CLASS):
        gh = FakeGh(prs=[_pr(number=i, sha=f"sha{i:09d}")], checks=_green())
        _run(gh, led)
    gh = FakeGh(prs=[_pr(number=99, sha="zzzzzzzzzzzz")], checks=_green())
    out = _run(gh, led)
    assert gh.merged == []
    assert guards_reason(out[0]) == "ratelimit"


def test_merge_failure_is_recorded_and_reported(led):
    gh = FakeGh(prs=[_pr()], checks=_green(), fail={"pr merge": "not mergeable"})
    out = _run(gh, led)
    assert "merge failed" in out[0]
    assert ledger.read(path=led)[0].outcome == ledger.OUTCOME_FAILURE


def test_listing_failure_is_reported_not_swallowed(led):
    gh = FakeGh(fail={"pr list": "network down"})
    out = _run(gh, led)
    assert "cannot list PRs" in out[0]


# --------------------------------------------------------------- dry run ----

def test_dry_run_merges_nothing_and_writes_no_ledger(led):
    gh = FakeGh(prs=[_pr()], checks=_green())
    out = _run(gh, led, dry_run=True)
    assert gh.merged == []
    assert "WOULD MERGE" in out[0]
    assert not led.exists() or ledger.read(path=led) == []


def test_dry_run_still_reports_protected_blocks(led):
    gh = FakeGh(prs=[_pr()], checks=_green(),
                files=[".github/x.yml"], protected=".github/**")
    out = _run(gh, led, dry_run=True)
    assert "needs a human" in out[0]
    assert not led.exists() or ledger.read(path=led) == []


# ---------------------------------------------------------------- config ----

def test_no_repos_configured_says_so_rather_than_failing_silently():
    assert "no repos configured" in watcher.run([], env={})[0]


@pytest.mark.parametrize("raw,expected", [
    ("a/b,c/d", ["a/b", "c/d"]),
    ("a/b c/d", ["a/b", "c/d"]),
    ("  a/b , c/d ", ["a/b", "c/d"]),
    ("", []),
])
def test_repo_list_parsing(raw, expected):
    assert watcher.load_repos({"MERGE_ON_GREEN_REPOS": raw}) == expected


# ------------------------------------------------------- cron wrapper -------

def test_wrapper_survives_its_own_name_shadowing_the_package(tmp_path):
    """The deployed wrapper is named merge_on_green.py and sits in its own dir.

    Running it puts that dir on sys.path[0], so a naive ``import merge_on_green``
    binds to the script instead of the package. Regression test for that.
    """
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(repo / "scripts" / "merge_on_green.py", scripts / "merge_on_green.py")

    proc = subprocess.run(
        [sys.executable, "merge_on_green.py"],
        cwd=scripts,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HERMES_REPO_ROOT": str(repo),
             "MERGE_ON_GREEN_REPOS": ""},
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "no repos configured" in proc.stdout


def test_wrapper_fails_loudly_when_the_package_cannot_be_found(tmp_path):
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(repo / "scripts" / "merge_on_green.py", scripts / "merge_on_green.py")

    proc = subprocess.run(
        [sys.executable, "merge_on_green.py"],
        cwd=scripts, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin"}, timeout=60,
    )
    assert proc.returncode == 1
    assert "cannot import the watcher" in proc.stderr
