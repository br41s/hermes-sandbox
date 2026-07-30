"""Tests for per-run isolated checkouts (cron/scheduler.py).

Background: cron AGENT jobs used to share one physical git working tree as their
workdir (e.g. /opt/data/biglobster for the biglobster SEO agent + the content
gap-hunter). Uncommitted edits from one agent survived in the shared tree and the
next agent's `git checkout -- <tracked>` reverted them — a near-miss data-loss
class (2026-06-28). The scheduler now runs git-workdir agent jobs in an ephemeral
local clone, one per run.

Covers:
  - _is_git_worktree: detection
  - _provision_isolated_checkout: passthrough (kill-switch / non-git), clone,
    origin rewrite, identity copy, and isolation from a DIRTY source tree
    (reproducing the incident)
  - _cleanup_isolated_checkout: removes ephemeral, refuses anything else
  - _sweep_stale_checkouts: reaps old leaked dirs, keeps fresh ones
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def _git(args, cwd):
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def source_repo(tmp_path):
    """A git working tree with one commit, an `origin` remote, and identity set."""
    repo = tmp_path / "shared"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "hermes@agent.local"], repo)
    _git(["config", "user.name", "hermes"], repo)
    _git(["remote", "add", "origin", "https://github.com/br41s/biglobster.git"], repo)
    (repo / "web").mkdir()
    (repo / "web" / "article.html").write_text("<h1>v1</h1>\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "initial"], repo)
    return repo


@pytest.fixture()
def checkout_base(tmp_path, monkeypatch):
    base = tmp_path / "checkouts"
    base.mkdir()
    monkeypatch.setenv("HERMES_CRON_CHECKOUT_DIR", str(base))
    monkeypatch.delenv("HERMES_CRON_ISOLATE_WORKDIR", raising=False)
    return base


# ---------------------------------------------------------------------------
# _is_git_worktree
# ---------------------------------------------------------------------------

class TestIsGitWorktree:
    def test_git_tree_detected(self, source_repo):
        from cron.scheduler import _is_git_worktree
        assert _is_git_worktree(str(source_repo)) is True

    def test_plain_dir_not_detected(self, tmp_path):
        from cron.scheduler import _is_git_worktree
        assert _is_git_worktree(str(tmp_path)) is False


# ---------------------------------------------------------------------------
# _provision_isolated_checkout — passthrough cases
# ---------------------------------------------------------------------------

class TestProvisionPassthrough:
    def test_kill_switch_returns_original(self, source_repo, checkout_base, monkeypatch):
        from cron.scheduler import _provision_isolated_checkout
        monkeypatch.setenv("HERMES_CRON_ISOLATE_WORKDIR", "0")
        eff, cleanup = _provision_isolated_checkout("job1", "biglobster", str(source_repo))
        assert eff == str(source_repo)
        assert cleanup is None

    def test_non_git_workdir_returns_original(self, tmp_path, checkout_base):
        from cron.scheduler import _provision_isolated_checkout
        plain = tmp_path / "plain"
        plain.mkdir()
        eff, cleanup = _provision_isolated_checkout("job1", "x", str(plain))
        assert eff == str(plain)
        assert cleanup is None

    def test_empty_workdir_returns_original(self, checkout_base):
        from cron.scheduler import _provision_isolated_checkout
        eff, cleanup = _provision_isolated_checkout("job1", "x", "")
        assert eff == ""
        assert cleanup is None


# ---------------------------------------------------------------------------
# _provision_isolated_checkout — real clone
# ---------------------------------------------------------------------------

class TestProvisionClone:
    def test_creates_separate_clone(self, source_repo, checkout_base):
        from cron.scheduler import _provision_isolated_checkout, _cleanup_isolated_checkout
        eff, cleanup = _provision_isolated_checkout("jobA", "biglobster", str(source_repo))
        try:
            assert cleanup == eff
            assert eff != str(source_repo)
            assert Path(eff).is_dir()
            assert (Path(eff) / ".git").exists()
            # Under the configured checkout base, with the expected prefix.
            assert Path(eff).parent == checkout_base
            assert Path(eff).name.startswith("cron-checkout-")
            # Same committed content.
            assert (Path(eff) / "web" / "article.html").read_text() == "<h1>v1</h1>\n"
        finally:
            _cleanup_isolated_checkout(cleanup)

    def test_origin_points_at_source_remote(self, source_repo, checkout_base):
        from cron.scheduler import _provision_isolated_checkout, _cleanup_isolated_checkout
        eff, cleanup = _provision_isolated_checkout("jobA", "bl", str(source_repo))
        try:
            url = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=eff, capture_output=True, text=True, check=True,
            ).stdout.strip()
            assert url == "https://github.com/br41s/biglobster.git"
        finally:
            _cleanup_isolated_checkout(cleanup)

    def test_tokenized_source_origin_stripped_in_clone(self, source_repo, checkout_base):
        """A PAT baked into the source tree's origin URL must NOT land in the
        ephemeral clone's .git/config — the leak this migration closes."""
        from cron.scheduler import _provision_isolated_checkout, _cleanup_isolated_checkout

        _git(
            ["remote", "set-url", "origin",
             "https://x-access-token:github_pat_SECRET123@github.com/br41s/biglobster.git"],
            source_repo,
        )
        eff, cleanup = _provision_isolated_checkout("jobA", "bl", str(source_repo))
        try:
            url = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=eff, capture_output=True, text=True, check=True,
            ).stdout.strip()
            assert url == "https://github.com/br41s/biglobster.git"
            assert "github_pat_SECRET123" not in (Path(eff) / ".git" / "config").read_text()
        finally:
            _cleanup_isolated_checkout(cleanup)

    def test_commit_identity_copied(self, source_repo, checkout_base):
        from cron.scheduler import _provision_isolated_checkout, _cleanup_isolated_checkout
        eff, cleanup = _provision_isolated_checkout("jobA", "bl", str(source_repo))
        try:
            email = subprocess.run(
                ["git", "config", "--local", "user.email"],
                cwd=eff, capture_output=True, text=True, check=True,
            ).stdout.strip()
            assert email == "hermes@agent.local"
        finally:
            _cleanup_isolated_checkout(cleanup)

    def test_isolated_from_dirty_source_tree(self, source_repo, checkout_base):
        """The incident: a dirty tracked edit in the shared tree must NOT leak
        into the clone, and reverting in the clone must NOT touch the source."""
        from cron.scheduler import _provision_isolated_checkout, _cleanup_isolated_checkout

        # Agent A leaves uncommitted work in the shared tree.
        dirty = source_repo / "web" / "article.html"
        dirty.write_text("<h1>AGENT-A-UNCOMMITTED</h1>\n", encoding="utf-8")

        # Agent B is provisioned an isolated clone.
        eff, cleanup = _provision_isolated_checkout("jobB", "bl", str(source_repo))
        try:
            clone_file = Path(eff) / "web" / "article.html"
            # Clone reflects the committed state, NOT agent A's dirty edit.
            assert clone_file.read_text() == "<h1>v1</h1>\n"

            # Agent B's clean-tree protocol runs inside the clone only.
            _git(["checkout", "--", "web/article.html"], eff)

            # The shared tree's uncommitted work is untouched — no clobber.
            assert dirty.read_text() == "<h1>AGENT-A-UNCOMMITTED</h1>\n"
        finally:
            _cleanup_isolated_checkout(cleanup)


# ---------------------------------------------------------------------------
# _strip_url_credentials
# ---------------------------------------------------------------------------

class TestStripUrlCredentials:
    @pytest.mark.parametrize("url, expected", [
        ("https://x-access-token:github_pat_ABC@github.com/o/r.git",
         "https://github.com/o/r.git"),
        ("https://github.com/o/r.git", "https://github.com/o/r.git"),
        ("git@github.com:o/r.git", "git@github.com:o/r.git"),
        ("/opt/data/biglobster", "/opt/data/biglobster"),
        ("https://user:p%40ss@github.com/o/r.git", "https://github.com/o/r.git"),
    ])
    def test_strip(self, url, expected):
        from cron.scheduler import _strip_url_credentials
        assert _strip_url_credentials(url) == expected


# ---------------------------------------------------------------------------
# cleanup + sweep
# ---------------------------------------------------------------------------

class TestCleanup:
    def test_cleanup_removes_ephemeral(self, source_repo, checkout_base):
        from cron.scheduler import _provision_isolated_checkout, _cleanup_isolated_checkout
        eff, cleanup = _provision_isolated_checkout("jobA", "bl", str(source_repo))
        assert Path(eff).is_dir()
        _cleanup_isolated_checkout(cleanup)
        assert not Path(eff).exists()

    def test_cleanup_none_is_noop(self):
        from cron.scheduler import _cleanup_isolated_checkout
        _cleanup_isolated_checkout(None)  # must not raise

    def test_cleanup_refuses_non_ephemeral_path(self, source_repo, checkout_base):
        """Guard: never rmtree a path that isn't one of our ephemeral dirs."""
        from cron.scheduler import _cleanup_isolated_checkout
        _cleanup_isolated_checkout(str(source_repo))
        assert source_repo.is_dir()  # still there


class TestSafeDirectory:
    def test_git_helper_disables_dubious_ownership_guard(self):
        """Provisioning must tolerate a source tree owned by another user
        (shared clone is hermes-owned; maintenance may run as root)."""
        import cron.scheduler as sched

        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv

            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()

        orig = sched.subprocess.run
        sched.subprocess.run = fake_run
        try:
            sched._git(["clone", "--local", "/src", "/dst"])
        finally:
            sched.subprocess.run = orig

        argv = captured["argv"]
        assert argv[:3] == ["git", "-c", "safe.directory=*"]


class TestCrossDeviceFallback:
    def test_retries_without_hardlinks_on_cross_device(self, checkout_base, monkeypatch):
        """When the base is on a different filesystem than the source, the
        hardlink clone fails with 'Invalid cross-device link' and we must retry
        with --no-hardlinks instead of aborting the run."""
        import cron.scheduler as sched

        calls = []

        class R:
            def __init__(self, rc, out="", err=""):
                self.returncode, self.stdout, self.stderr = rc, out, err

        def fake_git(args, cwd=None, timeout=300):
            calls.append(args)
            if args[0] == "clone":
                if "--no-hardlinks" not in args:
                    return R(128, err="fatal: failed to create link ... Invalid cross-device link")
                return R(0)  # retry succeeds
            if args[:2] == ["remote", "get-url"]:
                return R(0, out="https://github.com/x/y.git")
            return R(0)

        monkeypatch.setattr(sched, "_is_git_worktree", lambda p: True)
        monkeypatch.setattr(sched, "_git", fake_git)
        eff, cleanup = sched._provision_isolated_checkout("job", "bl", "/fake/src")
        clone_calls = [c for c in calls if c[0] == "clone"]
        assert len(clone_calls) == 2
        assert "--no-hardlinks" in clone_calls[1]
        sched._cleanup_isolated_checkout(cleanup)

    def test_non_crossdevice_clone_failure_still_fails_closed(self, checkout_base, monkeypatch):
        """A clone failure that ISN'T cross-device must not retry — it fails closed."""
        import cron.scheduler as sched

        class R:
            returncode, stdout, stderr = 128, "", "fatal: some other error"

        monkeypatch.setattr(sched, "_is_git_worktree", lambda p: True)
        monkeypatch.setattr(sched, "_git", lambda *a, **k: R())
        with pytest.raises(sched.IsolatedCheckoutError):
            sched._provision_isolated_checkout("job", "bl", "/fake/src")


class TestSweep:
    def test_sweeps_old_keeps_fresh(self, checkout_base):
        from cron.scheduler import _sweep_stale_checkouts
        old = checkout_base / "cron-checkout-bl-old-123"
        fresh = checkout_base / "cron-checkout-bl-fresh-456"
        unrelated = checkout_base / "something-else"
        for d in (old, fresh, unrelated):
            d.mkdir()
        # Age `old` past the 6h cutoff.
        past = (Path(__file__).stat().st_mtime) - 7 * 3600
        os.utime(old, (past, past))

        _sweep_stale_checkouts(max_age_h=6.0)

        assert not old.exists()
        assert fresh.exists()
        assert unrelated.exists()  # not our prefix — left alone
