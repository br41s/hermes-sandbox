"""Tests for the merge-on-green watcher.

Every path that could merge something is exercised, plus every path that must
refuse. The bias throughout is fail-closed: when in doubt, do not merge.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

from merge_on_green import watcher
from remediation import ledger

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOT_SCRIPT = REPO_ROOT / "docker" / "cont-init.d" / "03-biglobster-config"


# ---------------------------------------------------------------- fake gh ---

def approval(sha, login=watcher.AUDITOR_LOGIN):
    return {"author": {"login": login},
            "body": f"I would merge this — checked X.\n<!-- hermes-auditor:approve {sha} -->"}


class FakeGh:
    """Minimal ``gh`` stand-in. Records merges instead of performing them.

    By default every listed PR is auditor-approved at its head, MERGEABLE, and
    has no checks — i.e. ready. Tests take one condition away at a time.
    """

    def __init__(self, *, prs=None, details=None, checks=None, checks_error=None,
                 files=None, protected=None, fail=None):
        self.prs = prs if prs is not None else []
        self.details = details or {}
        self.checks = checks if checks is not None else []
        self.checks_error = checks_error
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
        if args[:2] == ["pr", "view"]:
            number = int(args[2])
            pr = next(p for p in self.prs if p["number"] == number)
            d = {"headRefOid": pr["headRefOid"], "mergeable": "MERGEABLE",
                 "comments": [approval(pr["headRefOid"])]}
            d.update(self.details.get(number, {}))
            return json.dumps(d)
        if args[:2] == ["pr", "checks"]:
            if self.checks_error:
                raise watcher.GhError(self.checks_error)
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


HEAD = "abc123def4567890abc123def4567890abc12345"


def _pr(number=7, sha=HEAD, draft=False, author="br41s"):
    return {"number": number, "isDraft": draft, "headRefOid": sha,
            "author": {"login": author}}


@pytest.fixture
def led(tmp_path):
    return tmp_path / "ledger.jsonl"


def _safe(repo, number):
    return True, "ok: 0 file(s) removed"


def _run(gh, led, **kw):
    kw.setdefault("env", {})
    kw.setdefault("safety_check", _safe)
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
    hits = watcher.blocked_paths(["cloudbuild.yaml", "README.md", ".github/x.yml"], pats)
    assert {p for p, _ in hits} == {"cloudbuild.yaml", ".github/x.yml"}


def test_missing_protected_file_falls_back_to_defaults_not_to_nothing():
    pats = watcher.protected_patterns("br41s/demo", runner=FakeGh(protected=None))
    assert watcher.blocked_paths([".github/workflows/x.yml"], pats)


def test_empty_protected_file_still_defends_ci():
    pats = watcher.protected_patterns("br41s/demo", runner=FakeGh(protected="# only comments\n"))
    assert watcher.blocked_paths([".github/workflows/x.yml"], pats)


# ------------------------------------------------------ auditor approval -----

def test_approval_for_the_current_head_counts():
    assert watcher.auditor_approved([approval(HEAD)], HEAD)


def test_truncated_sha_of_at_least_seven_chars_still_binds():
    assert watcher.auditor_approved([approval(HEAD[:7])], HEAD)


def test_sha_shorter_than_seven_chars_does_not_bind():
    assert not watcher.auditor_approved([approval(HEAD[:6])], HEAD)


def test_approval_for_an_older_commit_does_not_count():
    assert not watcher.auditor_approved([approval("0" * 40)], HEAD)


def test_marker_written_by_anyone_else_is_ignored():
    assert not watcher.auditor_approved([approval(HEAD, login="random-user")], HEAD)


def test_plain_approve_prose_without_marker_does_not_count():
    comments = [{"author": {"login": watcher.AUDITOR_LOGIN}, "body": "APPROVE — I would merge this"}]
    assert not watcher.auditor_approved(comments, HEAD)


def test_the_marker_the_auditor_is_told_to_write_is_the_one_we_parse():
    """Contract between writer (auditor prompt + SOUL) and reader (this regex)."""
    for rel in ("auditor/auditor.prompt", "docker/profiles/auditor/SOUL.md"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        m = re.search(r"<!--\s*hermes-auditor:approve\s+<headRefOid>\s*-->", text)
        assert m, f"{rel} no longer instructs the approval marker"
        example = m.group(0).replace("<headRefOid>", HEAD)
        assert watcher.APPROVE_MARKER.search(example), f"{rel}: marker format drifted"


# ------------------------------------------------------------ check reading --

def test_zero_checks_does_not_veto():
    assert watcher.checks_ok("br41s/demo", 1, runner=FakeGh(checks=[]))[0] is True


def test_gh_no_checks_error_does_not_veto():
    gh = FakeGh(checks_error="no checks reported on the 'feat' branch")
    assert watcher.checks_ok("br41s/demo", 1, runner=gh)[0] is True


def test_any_other_checks_error_vetoes():
    assert watcher.checks_ok("br41s/demo", 1, runner=FakeGh(checks_error="boom"))[0] is False


@pytest.mark.parametrize("state", ["FAILURE", "PENDING", "WEIRD"])
def test_failing_pending_or_unknown_check_vetoes(state):
    gh = FakeGh(checks=[{"name": "ci", "state": state}])
    ok, detail = watcher.checks_ok("br41s/demo", 1, runner=gh)
    assert ok is False and f"ci={state}" in detail


def test_skipped_and_neutral_do_not_veto():
    gh = FakeGh(checks=[{"name": "a", "state": "SUCCESS"},
                        {"name": "b", "state": "SKIPPED"},
                        {"name": "c", "state": "NEUTRAL"}])
    assert watcher.checks_ok("br41s/demo", 1, runner=gh)[0] is True


# -------------------------------------------------------------- happy path --

def test_merges_an_approved_labelled_ready_pr(led):
    gh = FakeGh(prs=[_pr()])
    out = _run(gh, led)
    assert gh.merged == ["7"]
    assert "merged" in out[0] and HEAD[:7] in out[0]
    entries = ledger.read(path=led)
    assert [e.event for e in entries] == [ledger.EVENT_APPLIED]
    assert entries[0].outcome == ledger.OUTCOME_SUCCESS


def test_silent_when_nothing_to_do(led):
    assert _run(FakeGh(prs=[]), led) == []


def test_asks_github_only_for_labelled_prs(led):
    seen = {}

    def runner(args):
        seen["args"] = list(args)
        return "[]"

    _run(runner, led)
    assert "--label" in seen["args"] and watcher.LABEL in seen["args"]


def test_draft_is_ignored(led):
    gh = FakeGh(prs=[_pr(draft=True)])
    assert _run(gh, led) == [] and gh.merged == []


# ------------------------------------------------------------- refusals -----

def test_never_merges_the_auditors_own_pr(led):
    gh = FakeGh(prs=[_pr(author=watcher.AUDITOR_LOGIN)])
    assert _run(gh, led) == [] and gh.merged == []


def test_no_approval_waits_silently(led):
    gh = FakeGh(prs=[_pr()], details={7: {"comments": []}})
    assert _run(gh, led) == [] and gh.merged == []


def test_no_approval_is_explained_when_verbose(led):
    gh = FakeGh(prs=[_pr()], details={7: {"comments": []}})
    assert "no hermes-auditor approval" in _run(gh, led, verbose=True)[0]


def test_push_after_approval_voids_it(led):
    gh = FakeGh(prs=[_pr(sha="f" * 40)], details={7: {"comments": [approval(HEAD)]}})
    assert _run(gh, led) == [] and gh.merged == []


@pytest.mark.parametrize("state", ["UNKNOWN", "CONFLICTING", None])
def test_not_mergeable_waits(led, state):
    gh = FakeGh(prs=[_pr()], details={7: {"mergeable": state}})
    assert _run(gh, led) == [] and gh.merged == []


def test_failing_check_waits(led):
    gh = FakeGh(prs=[_pr()], checks=[{"name": "ci", "state": "FAILURE"}])
    assert _run(gh, led) == [] and gh.merged == []


def test_empty_diff_needs_a_human(led):
    gh = FakeGh(prs=[_pr()], files=[])
    out = _run(gh, led)
    assert gh.merged == [] and "diff reported no files" in out[0]


def test_protected_path_needs_a_human(led):
    gh = FakeGh(prs=[_pr()], files=["README.md", ".github/workflows/ci.yml"],
                protected=".github/**")
    out = _run(gh, led)
    assert gh.merged == []
    assert "needs a human" in out[0] and ".github/workflows/ci.yml" in out[0]


def test_mass_deletion_needs_a_human(led):
    gh = FakeGh(prs=[_pr()])
    out = _run(gh, led, safety_check=lambda r, n: (False, "⚠️ mass-deletion guard: 40 files"))
    assert gh.merged == [] and "mass-deletion" in out[0]


def test_kill_switch_holds_and_says_so(led):
    gh = FakeGh(prs=[_pr()])
    out = _run(gh, led, env={"HERMES_AUTONOMY": "paused"})
    assert gh.merged == [] and "killswitch" in out[0]


# ------------------------------------------------ once-per-commit reporting -

@pytest.mark.parametrize("kw", [
    {"files": [".github/x.yml"], "protected": ".github/**"},
    {"files": []},
])
def test_a_refusal_is_reported_once_not_every_tick(led, kw):
    assert _run(FakeGh(prs=[_pr()], **kw), led)
    assert _run(FakeGh(prs=[_pr()], **kw), led) == []


def test_a_held_merge_is_reported_once_not_every_tick(led):
    paused = {"HERMES_AUTONOMY": "paused"}
    assert _run(FakeGh(prs=[_pr()]), led, env=paused)
    assert _run(FakeGh(prs=[_pr()]), led, env=paused) == []


def test_a_list_failure_is_reported_once_not_every_tick(led):
    assert "cannot list PRs" in _run(FakeGh(fail={"pr list": "down"}), led)[0]
    assert _run(FakeGh(fail={"pr list": "down"}), led) == []


def test_reporting_a_refusal_does_not_consume_merge_budget(led):
    for i in range(ledger.RATE_MAX_PER_CLASS + 2):
        _run(FakeGh(prs=[_pr(number=i, sha=f"{i + 1:040x}")], files=[]), led)
    gh = FakeGh(prs=[_pr(number=99)])
    _run(gh, led)
    assert gh.merged == ["99"]


def test_verbose_repeats_an_already_reported_refusal(led):
    kw = {"files": [".github/x.yml"], "protected": ".github/**"}
    _run(FakeGh(prs=[_pr()], **kw), led)
    assert _run(FakeGh(prs=[_pr()], **kw), led, verbose=True)


# ------------------------------------------------------- debounce + limits --

def test_same_commit_is_never_merged_twice(led):
    gh = FakeGh(prs=[_pr()])
    _run(gh, led)
    gh2 = FakeGh(prs=[_pr()])
    assert _run(gh2, led) == [] and gh2.merged == []


def test_failed_merge_is_not_retried_on_the_same_commit(led):
    out = _run(FakeGh(prs=[_pr()], fail={"pr merge": "not mergeable"}), led)
    assert "merge failed" in out[0]
    assert ledger.read(path=led)[0].outcome == ledger.OUTCOME_FAILURE
    gh2 = FakeGh(prs=[_pr()])
    assert _run(gh2, led) == [] and gh2.merged == []


def test_a_new_approved_commit_is_a_new_opportunity(led):
    _run(FakeGh(prs=[_pr(sha="a" * 40)]), led)
    gh2 = FakeGh(prs=[_pr(sha="b" * 40)])
    _run(gh2, led)
    assert gh2.merged == ["7"]


def test_rate_limit_applies_within_a_single_tick(led):
    n = ledger.RATE_MAX_PER_CLASS
    gh = FakeGh(prs=[_pr(number=i, sha=f"{i + 1:040x}") for i in range(n + 2)])
    out = _run(gh, led)
    assert len(gh.merged) == n
    assert sum("ratelimit" in line for line in out) == 2


def test_rate_limit_applies_across_ticks(led):
    for i in range(ledger.RATE_MAX_PER_CLASS):
        _run(FakeGh(prs=[_pr(number=i, sha=f"{i + 1:040x}")]), led)
    gh = FakeGh(prs=[_pr(number=99)])
    out = _run(gh, led)
    assert gh.merged == [] and "ratelimit" in out[0]


# --------------------------------------------------------------- dry run ----

def test_dry_run_merges_nothing_and_writes_no_ledger(led):
    gh = FakeGh(prs=[_pr()])
    out = _run(gh, led, dry_run=True)
    assert gh.merged == [] and "WOULD MERGE" in out[0]
    assert ledger.read(path=led) == []


def test_dry_run_reports_refusals_without_recording_them(led):
    gh = FakeGh(prs=[_pr()], files=[".github/x.yml"], protected=".github/**")
    assert "needs a human" in _run(gh, led, dry_run=True)[0]
    assert ledger.read(path=led) == []


# ---------------------------------------------------------------- repo set --

def test_default_repo_set_is_the_auditors(monkeypatch):
    import auditor.pending

    monkeypatch.setattr(auditor.pending, "review_repos", lambda: ["br41s/a", "br41s/b"])
    assert watcher.load_repos() == ["br41s/a", "br41s/b"]


def test_real_review_set_covers_engine_and_profiles():
    repos = watcher.load_repos()
    assert "br41s/hermes-sandbox" in repos and "br41s/FinView" in repos


def test_empty_repo_set_says_so():
    assert "no repos to watch" in watcher.run([], env={})[0]


# ------------------------------------------------------------ boot wiring ---

@pytest.fixture(scope="module")
def boot_text() -> str:
    if not BOOT_SCRIPT.exists():
        pytest.skip("docker/cont-init.d/03-biglobster-config not present")
    return BOOT_SCRIPT.read_text(encoding="utf-8")


def test_boot_seeds_the_wrapper_into_the_auditor_profile(boot_text):
    # A profile-scoped job resolves --script under that profile's scripts/ dir.
    assert 'cat > "$HERMES_HOME/profiles/auditor/scripts/merge_on_green.sh"' in boot_text
    assert "exec .venv/bin/python -m merge_on_green" in boot_text


def test_documented_registration_runs_under_the_auditor_profile(boot_text):
    assert "--profile auditor --no-agent" in boot_text
    assert "--script merge_on_green.sh" in boot_text
