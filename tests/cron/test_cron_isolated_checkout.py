"""Tests for per-run isolated checkouts (cron/fork_ext/isolated_checkout.py).

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

import logging
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


@pytest.fixture(autouse=True)
def _no_network_git(monkeypatch):
    """Keep git off the network: the https origin above then fails fast and
    locally, so provisioning exercises the warn-and-continue path."""
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")


# ---------------------------------------------------------------------------
# _is_git_worktree
# ---------------------------------------------------------------------------

class TestIsGitWorktree:
    def test_git_tree_detected(self, source_repo):
        from cron.fork_ext.isolated_checkout import _is_git_worktree
        assert _is_git_worktree(str(source_repo)) is True

    def test_plain_dir_not_detected(self, tmp_path):
        from cron.fork_ext.isolated_checkout import _is_git_worktree
        assert _is_git_worktree(str(tmp_path)) is False


# ---------------------------------------------------------------------------
# _provision_isolated_checkout — passthrough cases
# ---------------------------------------------------------------------------

class TestProvisionPassthrough:
    def test_kill_switch_returns_original(self, source_repo, checkout_base, monkeypatch):
        from cron.fork_ext.isolated_checkout import _provision_isolated_checkout
        monkeypatch.setenv("HERMES_CRON_ISOLATE_WORKDIR", "0")
        eff, cleanup = _provision_isolated_checkout("job1", "biglobster", str(source_repo))
        assert eff == str(source_repo)
        assert cleanup is None

    def test_non_git_workdir_returns_original(self, tmp_path, checkout_base):
        from cron.fork_ext.isolated_checkout import _provision_isolated_checkout
        plain = tmp_path / "plain"
        plain.mkdir()
        eff, cleanup = _provision_isolated_checkout("job1", "x", str(plain))
        assert eff == str(plain)
        assert cleanup is None

    def test_empty_workdir_returns_original(self, checkout_base):
        from cron.fork_ext.isolated_checkout import _provision_isolated_checkout
        eff, cleanup = _provision_isolated_checkout("job1", "x", "")
        assert eff == ""
        assert cleanup is None


# ---------------------------------------------------------------------------
# _provision_isolated_checkout — real clone
# ---------------------------------------------------------------------------

class TestProvisionClone:
    def test_creates_separate_clone(self, source_repo, checkout_base):
        from cron.fork_ext.isolated_checkout import _provision_isolated_checkout, _cleanup_isolated_checkout
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

    def test_concurrent_runs_of_one_job_get_separate_checkouts(self, source_repo, checkout_base):
        """Stage 3 step 5 put main's workdir jobs on main's unbounded pool, so two runs of the
        same job can now provision at the same time. Each must get its own tree: a write in
        one is invisible to the other and to the source."""
        import threading

        from cron.fork_ext.isolated_checkout import _provision_isolated_checkout, _cleanup_isolated_checkout

        barrier = threading.Barrier(2, timeout=10)
        results: list = []

        def _provision():
            barrier.wait()
            results.append(_provision_isolated_checkout("jobA", "biglobster", str(source_repo)))

        threads = [threading.Thread(target=_provision) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        try:
            assert len(results) == 2
            (eff_a, _), (eff_b, _) = results
            assert eff_a != eff_b
            assert str(source_repo) not in (eff_a, eff_b)
            (Path(eff_a) / "web" / "article.html").write_text("<h1>run A</h1>\n", encoding="utf-8")
            assert (Path(eff_b) / "web" / "article.html").read_text() == "<h1>v1</h1>\n"
            assert (source_repo / "web" / "article.html").read_text() == "<h1>v1</h1>\n"
        finally:
            for _eff, cleanup in results:
                _cleanup_isolated_checkout(cleanup)

    def test_origin_points_at_source_remote(self, source_repo, checkout_base):
        from cron.fork_ext.isolated_checkout import _provision_isolated_checkout, _cleanup_isolated_checkout
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
        from cron.fork_ext.isolated_checkout import _provision_isolated_checkout, _cleanup_isolated_checkout

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
        from cron.fork_ext.isolated_checkout import _provision_isolated_checkout, _cleanup_isolated_checkout
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
        from cron.fork_ext.isolated_checkout import _provision_isolated_checkout, _cleanup_isolated_checkout

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
        from cron.fork_ext.isolated_checkout import _strip_url_credentials
        assert _strip_url_credentials(url) == expected


# ---------------------------------------------------------------------------
# cleanup + sweep
# ---------------------------------------------------------------------------

class TestCleanup:
    def test_cleanup_removes_ephemeral(self, source_repo, checkout_base):
        from cron.fork_ext.isolated_checkout import _provision_isolated_checkout, _cleanup_isolated_checkout
        eff, cleanup = _provision_isolated_checkout("jobA", "bl", str(source_repo))
        assert Path(eff).is_dir()
        _cleanup_isolated_checkout(cleanup)
        assert not Path(eff).exists()

    def test_cleanup_none_is_noop(self):
        from cron.fork_ext.isolated_checkout import _cleanup_isolated_checkout
        _cleanup_isolated_checkout(None)  # must not raise

    def test_cleanup_refuses_non_ephemeral_path(self, source_repo, checkout_base):
        """Guard: never rmtree a path that isn't one of our ephemeral dirs."""
        from cron.fork_ext.isolated_checkout import _cleanup_isolated_checkout
        _cleanup_isolated_checkout(str(source_repo))
        assert source_repo.is_dir()  # still there


class TestSafeDirectory:
    def test_git_helper_disables_dubious_ownership_guard(self):
        """Provisioning must tolerate a source tree owned by another user
        (shared clone is hermes-owned; maintenance may run as root)."""
        import cron.fork_ext.isolated_checkout as sched

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
        import cron.fork_ext.isolated_checkout as sched

        calls = []

        class R:
            def __init__(self, rc, out="", err=""):
                self.returncode, self.stdout, self.stderr = rc, out, err

        def fake_git(args, cwd=None, timeout=300, **_kw):
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
        _eff, cleanup = sched._provision_isolated_checkout("job", "bl", "/fake/src")
        clone_calls = [c for c in calls if c[0] == "clone"]
        assert len(clone_calls) == 2
        assert "--no-hardlinks" in clone_calls[1]
        sched._cleanup_isolated_checkout(cleanup)

    def test_non_crossdevice_clone_failure_still_fails_closed(self, checkout_base, monkeypatch):
        """A clone failure that ISN'T cross-device must not retry — it fails closed."""
        import cron.fork_ext.isolated_checkout as sched

        class R:
            returncode, stdout, stderr = 128, "", "fatal: some other error"

        monkeypatch.setattr(sched, "_is_git_worktree", lambda p: True)
        monkeypatch.setattr(sched, "_git", lambda *a, **k: R())
        with pytest.raises(sched.IsolatedCheckoutError):
            sched._provision_isolated_checkout("job", "bl", "/fake/src")


# ---------------------------------------------------------------------------
# _sync_to_origin_default — the clone starts at origin's default branch tip
# ---------------------------------------------------------------------------

class TestSyncToOriginDefault:
    def test_clone_starts_at_origin_default_branch_tip(self, tmp_path, checkout_base, monkeypatch):
        """The shared tree can be months stale and parked on an unpushed branch
        (2026-11-18: 682 commits behind). The clone must come from origin's
        default branch tip, not from the source tree's HEAD."""
        import cron.fork_ext.isolated_checkout as sched
        monkeypatch.setattr(sched, "_FETCH_ALLOW_PROTOCOL", "file")  # local bare upstream
        from cron.fork_ext.isolated_checkout import _provision_isolated_checkout, _cleanup_isolated_checkout

        upstream = tmp_path / "up.git"
        _git(["init", "-q", "--bare", "-b", "main", str(upstream)], tmp_path)

        seed = tmp_path / "seed"
        _git(["clone", "-q", str(upstream), str(seed)], tmp_path)
        _git(["config", "user.email", "hermes@agent.local"], seed)
        _git(["config", "user.name", "hermes"], seed)
        (seed / "a.txt").write_text("A", encoding="utf-8")
        _git(["add", "."], seed)
        _git(["commit", "-q", "-m", "A"], seed)
        _git(["push", "-q", "origin", "main"], seed)

        # The shared tree, cloned at commit A and never pulled since.
        source = tmp_path / "shared"
        _git(["clone", "-q", str(upstream), str(source)], tmp_path)
        _git(["config", "user.email", "hermes@agent.local"], source)
        _git(["config", "user.name", "hermes"], source)

        # Upstream moves ahead of the shared tree.
        (seed / "b.txt").write_text("B", encoding="utf-8")
        _git(["add", "."], seed)
        _git(["commit", "-q", "-m", "B"], seed)
        _git(["push", "-q", "origin", "main"], seed)
        upstream_sha = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=str(upstream), capture_output=True, text=True, check=True,
        ).stdout.strip()

        # The shared tree is parked on an unpushed branch.
        _git(["checkout", "-q", "-b", "stale-branch"], source)
        (source / "c.txt").write_text("C", encoding="utf-8")
        _git(["add", "."], source)
        _git(["commit", "-q", "-m", "C"], source)

        eff, cleanup = _provision_isolated_checkout("job", "bl", str(source))
        try:
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=eff, capture_output=True, text=True, check=True,
            ).stdout.strip()
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=eff, capture_output=True, text=True, check=True,
            ).stdout.strip()
            assert branch == "main"
            assert sha == upstream_sha
            assert (Path(eff) / "b.txt").exists()
            assert not (Path(eff) / "c.txt").exists()
        finally:
            _cleanup_isolated_checkout(cleanup)

    def test_fetch_failure_keeps_source_head_and_warns(self, source_repo, checkout_base, caplog):
        """An unreachable origin must not fail the run: the clone stays on the
        source tree's committed HEAD and a warning is logged."""
        from cron.fork_ext.isolated_checkout import _provision_isolated_checkout, _cleanup_isolated_checkout

        caplog.set_level(logging.WARNING, logger="cron.scheduler")
        eff, cleanup = _provision_isolated_checkout("job", "bl", str(source_repo))
        try:
            assert (Path(eff) / "web" / "article.html").read_text() == "<h1>v1</h1>\n"
            assert any("could not sync to origin" in r.message for r in caplog.records)
        finally:
            _cleanup_isolated_checkout(cleanup)

    def test_sync_timeout_does_not_fail_closed(self, checkout_base, monkeypatch):
        """subprocess.TimeoutExpired is a SubprocessError; letting it escape
        would fail the job closed. A failed sync must only warn."""
        import cron.fork_ext.isolated_checkout as sched

        class R:
            def __init__(self, rc, out="", err=""):
                self.returncode, self.stdout, self.stderr = rc, out, err

        def fake_git(args, cwd=None, timeout=300, **_kw):
            if args[0] == "clone":
                return R(0)
            if args[:2] == ["remote", "get-url"]:
                return R(0, out="https://github.com/x/y.git")
            if args[0] == "fetch":
                raise subprocess.TimeoutExpired(cmd="git fetch", timeout=120)
            return R(0)

        monkeypatch.setattr(sched, "_is_git_worktree", lambda p: True)
        monkeypatch.setattr(sched, "_git", fake_git)
        _eff, cleanup = sched._provision_isolated_checkout("job", "bl", "/fake/src")
        sched._cleanup_isolated_checkout(cleanup)


class TestSweep:
    def test_sweeps_old_keeps_fresh(self, checkout_base):
        from cron.fork_ext.isolated_checkout import _sweep_stale_checkouts
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


class TestSchedulerCredentialIsolation:
    """The scheduler's fetch must never run a credential helper copied from the
    shared tree: agents can write that tree's .git/config, and the scheduler's
    env holds every service secret (security review 2026-11-18)."""

    @pytest.mark.parametrize("key", ["credential.helper", "credential.https://example.com.helper"])
    def test_copied_helper_is_not_run(self, tmp_path, monkeypatch, key):
        from cron.fork_ext.isolated_checkout import _SCHEDULER_CREDENTIALS

        repo = tmp_path / "clone"
        repo.mkdir()
        _git(["init", "-q"], repo)
        marker = tmp_path / "helper-ran"
        _git(["config", key, f"!touch {marker}"], repo)
        monkeypatch.setenv("HOME", str(tmp_path))  # empty store, no global config
        monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")

        def fill(extra):
            subprocess.run(
                ["git", *extra, "credential", "fill"], cwd=repo,
                input="protocol=https\nhost=example.com\n\n",
                capture_output=True, text=True, check=False,
            )

        fill([])  # control: without the reset the planted helper runs
        assert marker.exists()
        marker.unlink()
        fill(list(_SCHEDULER_CREDENTIALS))
        assert not marker.exists()

    def test_fetch_and_set_head_use_scheduler_credentials(self, checkout_base, monkeypatch):
        import cron.fork_ext.isolated_checkout as sched

        calls = []

        class R:
            def __init__(self, rc=0, out="", err=""):
                self.returncode, self.stdout, self.stderr = rc, out, err

        def fake_git(args, cwd=None, timeout=300, **_kw):
            calls.append(list(args))
            if args[:2] == ["remote", "get-url"]:
                return R(out="https://github.com/x/y.git")
            if "symbolic-ref" in args:
                return R(out="origin/main\n")
            return R()

        monkeypatch.setattr(sched, "_is_git_worktree", lambda p: True)
        monkeypatch.setattr(sched, "_git", fake_git)
        _eff, cleanup = sched._provision_isolated_checkout("job", "bl", "/fake/src")
        network = [c for c in calls if "fetch" in c or "set-head" in c]
        assert len(network) == 2
        for c in network:
            assert c[:len(sched._SCHEDULER_CREDENTIALS)] == list(sched._SCHEDULER_CREDENTIALS)
        sched._cleanup_isolated_checkout(cleanup)

    def test_logged_reason_drops_url_credentials(self, checkout_base, monkeypatch, caplog):
        import cron.fork_ext.isolated_checkout as sched

        class R:
            def __init__(self, rc=0, out="", err=""):
                self.returncode, self.stdout, self.stderr = rc, out, err

        def fake_git(args, cwd=None, timeout=300, **_kw):
            if args[:2] == ["remote", "get-url"]:
                return R(out="https://github.com/x/y.git")
            if "fetch" in args:
                return R(128, err="fatal: unable to access 'HTTPS://x-access-token:ghp_SECRET@github.com/x/y.git/' "
                                  "token ghp_" + "A" * 36)
            return R()

        caplog.set_level(logging.WARNING, logger="cron.scheduler")
        monkeypatch.setattr(sched, "_is_git_worktree", lambda p: True)
        monkeypatch.setattr(sched, "_git", fake_git)
        _eff, cleanup = sched._provision_isolated_checkout("job", "bl", "/fake/src")
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "could not sync to origin" in text
        assert "ghp_SECRET" not in text
        assert "ghp_" + "A" * 36 not in text
        sched._cleanup_isolated_checkout(cleanup)

    def test_ext_transport_never_runs_even_if_config_allows_it(self, tmp_path, monkeypatch):
        """The origin URL comes from the agent-writable shared tree. Even with
        `protocol.ext.allow=always` in a config, the scheduler's fetch must not
        run an `ext::` command."""
        import cron.fork_ext.isolated_checkout as sched

        repo = tmp_path / "clone"
        repo.mkdir()
        _git(["init", "-q"], repo)
        marker = tmp_path / "ext-ran"
        _git(["remote", "add", "origin", f"ext::sh -c touch% {marker}"], repo)
        _git(["config", "protocol.ext.allow", "always"], repo)
        monkeypatch.delenv("GIT_ALLOW_PROTOCOL", raising=False)

        # control: with ext allowed by config, a plain fetch runs the planted command
        subprocess.run(["git", "fetch", "origin"], cwd=repo, capture_output=True, check=False, timeout=30)
        assert marker.exists()
        marker.unlink()
        sched._git([*sched._SCHEDULER_CREDENTIALS, "fetch", "origin"], cwd=str(repo), timeout=30,
                   allow_protocol=sched._FETCH_ALLOW_PROTOCOL)
        assert not marker.exists()
