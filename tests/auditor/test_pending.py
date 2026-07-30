"""Regression lock for auditor PR-dedup (auditor/pending.py).

Hermetic: monkeypatches the gh call (and the repo set) and injects a tmp
state_path, so no network or gh binary is touched. Guards the back-and-forth
contract — a PR is pending until reviewed at its head SHA, and reappears when the
author pushes (new SHA) — and the Phase-4 multi-repo contract: the dedup key is
repo-qualified so the same PR number on two repos never collides.
"""
import json

import auditor.pending as pending

TEST_REPO = "br41s/hermes-sandbox"


def _pr(number, head, login="claude-code", draft=False, files=("hermes/x.py",)):
    return {
        "number": number,
        "title": f"PR {number}",
        "headRefName": f"review/{number}",
        "headRefOid": head,
        "author": {"login": login},
        "isDraft": draft,
        "files": [{"path": p} for p in files],
        "url": f"https://github.com/o/r/pull/{number}",
    }


def _patch_repos(monkeypatch, repo_to_prs):
    """Patch the repo set + lister so each repo returns its own PR list."""
    monkeypatch.setattr(pending, "review_repos", lambda: list(repo_to_prs.keys()))
    monkeypatch.setattr(pending, "_gh_list_open_prs", lambda repo: repo_to_prs.get(repo, []))


def _patch_gh(monkeypatch, prs):
    """Single-repo convenience: all PRs belong to TEST_REPO."""
    _patch_repos(monkeypatch, {TEST_REPO: prs})


def test_new_pr_is_pending(monkeypatch, tmp_path):
    _patch_gh(monkeypatch, [_pr(1, "aaa")])
    out = pending.pending_prs(None, tmp_path / "state.json")
    assert [p["number"] for p in out] == [1]
    assert out[0]["changed_files"] == ["hermes/x.py"]
    assert out[0]["repo"] == TEST_REPO


def test_reviewed_head_is_not_pending(monkeypatch, tmp_path):
    sp = tmp_path / "state.json"
    _patch_gh(monkeypatch, [_pr(1, "aaa")])
    pending.mark_reviewed(TEST_REPO, 1, "aaa", sp)
    assert pending.pending_prs(None, sp) == []


def test_new_commit_makes_pr_pending_again(monkeypatch, tmp_path):
    sp = tmp_path / "state.json"
    pending.mark_reviewed(TEST_REPO, 1, "aaa", sp)
    # author pushed -> new head SHA
    _patch_gh(monkeypatch, [_pr(1, "bbb")])
    out = pending.pending_prs(None, sp)
    assert [p["number"] for p in out] == [1]
    assert out[0]["headRefOid"] == "bbb"


def test_drafts_skipped_by_default(monkeypatch, tmp_path):
    _patch_gh(monkeypatch, [_pr(1, "aaa", draft=True)])
    assert pending.pending_prs(None, tmp_path / "s.json") == []
    assert pending.pending_prs(None, tmp_path / "s.json", include_drafts=True)


def test_pr_missing_head_skipped(monkeypatch, tmp_path):
    bad = _pr(1, "")
    _patch_gh(monkeypatch, [bad])
    assert pending.pending_prs(None, tmp_path / "s.json") == []


def test_mark_is_bounded(monkeypatch, tmp_path):
    sp = tmp_path / "state.json"
    for i in range(pending._SEEN_CAP + 50):
        pending.mark_reviewed(TEST_REPO, i, "h", sp)
    seen = json.loads(sp.read_text())["seen"]
    assert len(seen) == pending._SEEN_CAP


def test_gh_failure_degrades_to_empty(monkeypatch, tmp_path):
    # lister returns [] for every repo -> no work, no crash (documented degrade).
    _patch_repos(monkeypatch, {TEST_REPO: []})
    assert pending.pending_prs(None, tmp_path / "s.json") == []


# --- Phase 4: multi-repo -----------------------------------------------------

def test_each_pr_carries_its_repo(monkeypatch, tmp_path):
    _patch_repos(monkeypatch, {"o/a": [_pr(1, "x")], "o/b": [_pr(2, "y")]})
    out = pending.pending_prs(None, tmp_path / "s.json")
    assert {(p["repo"], p["number"]) for p in out} == {("o/a", 1), ("o/b", 2)}


def test_state_key_is_repo_qualified_no_collision(monkeypatch, tmp_path):
    sp = tmp_path / "s.json"
    # Same PR number + same SHA on two different repos must be independent.
    pending.mark_reviewed("o/repoA", 5, "sha", sp)
    _patch_repos(monkeypatch, {"o/repoA": [_pr(5, "sha")], "o/repoB": [_pr(5, "sha")]})
    out = pending.pending_prs(None, sp)
    # repoA#5@sha is reviewed; repoB#5@sha is still pending — no collision.
    assert [(p["repo"], p["number"]) for p in out] == [("o/repoB", 5)]


def test_explicit_repo_polls_only_that_repo(monkeypatch, tmp_path):
    _patch_repos(monkeypatch, {"o/a": [_pr(1, "x")], "o/b": [_pr(2, "y")]})
    out = pending.pending_prs("o/b", tmp_path / "s.json")
    assert [(p["repo"], p["number"]) for p in out] == [("o/b", 2)]


def test_review_repos_reads_profile_union(tmp_path, monkeypatch):
    prof = tmp_path / "docker" / "profiles"
    (prof / "p1").mkdir(parents=True)
    (prof / "p2").mkdir(parents=True)
    (prof / "p1" / "repos.txt").write_text("# comment\nbr41s/biglobster\n\n")
    (prof / "p2" / "repos.txt").write_text("br41s/FinView\nbr41s/biglobster\n")
    monkeypatch.setattr(pending, "_repo_root", lambda: tmp_path)
    assert pending.review_repos() == sorted(
        {"br41s/hermes-sandbox", "br41s/biglobster", "br41s/FinView"}
    )


def test_review_repos_always_includes_engine(tmp_path, monkeypatch):
    # No profiles dir at all -> still returns the engine repo.
    monkeypatch.setattr(pending, "_repo_root", lambda: tmp_path)
    assert pending.review_repos() == ["br41s/hermes-sandbox"]
