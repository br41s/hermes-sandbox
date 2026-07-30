"""Regression lock for the agent git guard (scripts/git-guard/ + sweep source).

Why this exists: on 2026-06-22 a blind `git add -A && git commit` (commit
dd0e1f5) captured the deletion of 48 referenced images in br41s/biglobster
and was pushed straight to main — the blog 404'd for ~22h. The account is GitHub
Free + private (no Actions / branch protection / server hooks), so a client-side
pre-commit hook installed via core.hooksPath is the WHOLE enforcement path.

These tests drive the real hook scripts through a throwaway git repo and assert:
  * a mass deletion (> limit) is blocked,
  * the documented override lets it through,
  * a small deletion / clean commit passes,
  * the repo's own .githooks/pre-commit is chained (per-project ref checks),
  * a blocked commit drops a signal the incident watcher reports (with dedup).

No rm/mv/git clean is used — deletions are staged with `git update-index
--force-remove`, exactly reproducing the dd0e1f5 index state.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from incidents.sweep import blocked_commit_incidents, sweep

GUARD_DIR = Path(__file__).resolve().parents[1] / "scripts" / "git-guard"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _run(args, cwd, env=None, check=False):
    base = dict(os.environ)
    base.update(env or {})
    return subprocess.run(args, cwd=str(cwd), env=base, check=check,
                          capture_output=True, text=True)


def _init_repo(tmp_path, hermes_home):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q"], repo, check=True)
    _run(["git", "config", "user.email", "t@t.t"], repo, check=True)
    _run(["git", "config", "user.name", "t"], repo, check=True)
    _run(["git", "config", "core.hooksPath", str(GUARD_DIR)], repo, check=True)
    return repo


def _commit(repo, hermes_home, message, extra_env=None):
    env = {"HERMES_HOME": str(hermes_home)}
    env.update(extra_env or {})
    return _run(["git", "commit", "-q", "-m", message], repo, env=env)


def _seed(repo, hermes_home, n_files):
    for i in range(n_files):
        (repo / f"f{i}.txt").write_text("x\n")
    _run(["git", "add", "-A"], repo, check=True)
    res = _commit(repo, hermes_home, "seed")
    assert res.returncode == 0, res.stderr


class TestMassDeletionGuard:
    def test_mass_deletion_blocked(self, tmp_path):
        home = tmp_path / "home"
        repo = _init_repo(tmp_path, home)
        _seed(repo, home, 15)
        for i in range(15):  # > default limit 10
            _run(["git", "update-index", "--force-remove", f"f{i}.txt"], repo, check=True)
        res = _commit(repo, home, "chore: commit pending changes for rebase sync")
        assert res.returncode != 0, "mass deletion should be blocked"
        assert "mass-deletion guard" in res.stderr
        # signal written for the watcher
        sig = home / "incidents" / "blocked-commits.jsonl"
        assert sig.exists()
        rec = json.loads(sig.read_text().splitlines()[-1])
        assert rec["reason"].startswith("mass-deletion")

    def test_override_allows(self, tmp_path):
        home = tmp_path / "home"
        repo = _init_repo(tmp_path, home)
        _seed(repo, home, 15)
        for i in range(15):
            _run(["git", "update-index", "--force-remove", f"f{i}.txt"], repo, check=True)
        res = _commit(repo, home, "intentional purge", {"HERMES_ALLOW_MASS_DELETION": "1"})
        assert res.returncode == 0, res.stderr
        assert not (home / "incidents" / "blocked-commits.jsonl").exists()

    def test_small_deletion_passes(self, tmp_path):
        home = tmp_path / "home"
        repo = _init_repo(tmp_path, home)
        _seed(repo, home, 12)
        for i in range(8):  # <= limit
            _run(["git", "update-index", "--force-remove", f"f{i}.txt"], repo, check=True)
        res = _commit(repo, home, "remove 8")
        assert res.returncode == 0, res.stderr

    def test_custom_limit_env(self, tmp_path):
        home = tmp_path / "home"
        repo = _init_repo(tmp_path, home)
        _seed(repo, home, 6)
        for i in range(4):
            _run(["git", "update-index", "--force-remove", f"f{i}.txt"], repo, check=True)
        res = _commit(repo, home, "remove 4", {"HERMES_MASS_DELETION_LIMIT": "3"})
        assert res.returncode != 0
        assert "limit 3" in res.stderr


class TestRepoChain:
    def test_chains_to_repo_pre_commit(self, tmp_path):
        home = tmp_path / "home"
        repo = _init_repo(tmp_path, home)
        _seed(repo, home, 2)
        hooks = repo / ".githooks"
        hooks.mkdir()
        (hooks / "pre-commit").write_text(
            "#!/usr/bin/env bash\necho '[repo hook] ref-check failed' >&2\nexit 1\n")
        (hooks / "pre-commit").chmod(0o755)
        (repo / "new.txt").write_text("y\n")
        _run(["git", "add", "new.txt"], repo, check=True)
        res = _commit(repo, home, "should be blocked by repo hook")
        assert res.returncode != 0
        assert "ref-check failed" in res.stderr
        rec = json.loads((home / "incidents" / "blocked-commits.jsonl").read_text().splitlines()[-1])
        assert "project pre-commit" in rec["reason"]


class TestWatcherSource:
    def test_blocked_signal_becomes_incident_and_dedups(self, tmp_path):
        sig = tmp_path / "blocked-commits.jsonl"
        sig.write_text(
            '{"ts":"2026-06-22T10:00:00Z","repo":"r","cwd":"/c","reason":"mass-deletion guard blocked a commit"}\n'
        )
        incs = blocked_commit_incidents(sig)
        assert len(incs) == 1 and incs[0].kind == "blocked_commit"

        state = tmp_path / "state.json"
        out1 = sweep(jobs=[], langfuse=[], blocked=incs, state_path=state)
        assert "Blocked agent commit" in out1
        out2 = sweep(jobs=[], langfuse=[], blocked=incs, state_path=state)
        assert out2 == ""  # deduped -> silent

    def test_malformed_lines_ignored(self, tmp_path):
        sig = tmp_path / "blocked-commits.jsonl"
        sig.write_text("not json\n\n{bad}\n")
        assert blocked_commit_incidents(sig) == []

    def test_missing_file_is_empty(self, tmp_path):
        assert blocked_commit_incidents(tmp_path / "nope.jsonl") == []
