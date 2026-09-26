"""Fork's own tests for cron/scheduler.py, kept out of upstream's file so upstream merges do not conflict."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cron.scheduler import _resolve_delivery_target, _send_kickoff_ping, run_job


class TestProfileAwareDeliveryTarget:
    """A profile-scoped job's routing.env-seeded chat/thread must win over
    the scheduler's global home-channel env, even though delivery resolution
    runs *after* run_job()'s per-job profile env context has already been
    torn down (see _job_profile_context / _read_profile_env_value). Regression
    for the BigLobster/FinView/Infographic jobs silently landing on the main
    Hermes thread instead of their own project thread (2026-07-08)."""

    @pytest.fixture
    def profile_root(self, tmp_path, monkeypatch):
        root = tmp_path / "hermes-root"
        (root / "profiles" / "biglobster").mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(root))
        return root

    def _write_profile_env(self, root, profile, content):
        (root / "profiles" / profile / ".env").write_text(content, encoding="utf-8")

    def test_profile_thread_wins_over_global_home_thread(self, monkeypatch, profile_root):
        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "-1004224848555")
        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_THREAD_ID", "1")  # global/default thread
        self._write_profile_env(
            profile_root,
            "biglobster",
            "TELEGRAM_HOME_CHANNEL=-1004224848555\nTELEGRAM_CRON_THREAD_ID=2\n",
        )

        job = {"deliver": "telegram", "profile": "biglobster", "origin": None}
        assert _resolve_delivery_target(job) == {
            "platform": "telegram",
            "chat_id": "-1004224848555",
            "thread_id": "2",
        }

    def test_profile_without_routing_env_falls_back_to_global(self, monkeypatch, profile_root):
        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "-1004224848555")
        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_THREAD_ID", "1")
        # biglobster profile dir exists but has no .env — must not error, must
        # fall back to the global home channel exactly like a profile-less job.
        job = {"deliver": "telegram", "profile": "biglobster", "origin": None}
        assert _resolve_delivery_target(job) == {
            "platform": "telegram",
            "chat_id": "-1004224848555",
            "thread_id": "1",
        }

    def test_unknown_profile_falls_back_to_global_without_raising(self, monkeypatch, profile_root):
        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "-1004224848555")
        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_THREAD_ID", "1")
        job = {"deliver": "telegram", "profile": "does-not-exist", "origin": None}
        assert _resolve_delivery_target(job) == {
            "platform": "telegram",
            "chat_id": "-1004224848555",
            "thread_id": "1",
        }

    def test_profile_scoped_job_without_profile_field_uses_global(self, monkeypatch, profile_root):
        """Sanity check: jobs with no `profile` key are completely unaffected."""
        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "-1004224848555")
        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_THREAD_ID", "1")
        self._write_profile_env(
            profile_root,
            "biglobster",
            "TELEGRAM_HOME_CHANNEL=-1004224848555\nTELEGRAM_CRON_THREAD_ID=2\n",
        )

        job = {"deliver": "telegram", "origin": None}
        assert _resolve_delivery_target(job) == {
            "platform": "telegram",
            "chat_id": "-1004224848555",
            "thread_id": "1",
        }


class TestRunJobSessionPersistenceFork:
    """Fork additions to upstream's TestRunJobSessionPersistence: tick() execution-source attribution."""

    def test_tick_records_cli_source_without_adapters(self, tmp_path):
        """A standalone `hermes cron tick` call (no gateway adapters) must be
        attributable after the fact — this is the exact gap that made an
        unattributed 2026-08-28 production run undiagnosable from the
        execution ledger alone (only reconstructible from raw process logs)."""
        from cron.scheduler import tick

        job = {
            "id": "cli-trigger-job",
            "name": "cli trigger job",
            "schedule": {"kind": "interval", "seconds": 60},
            "next_run_at": "2020-01-01T00:00:00+00:00",
            "enabled": True,
        }
        with patch("cron.scheduler.get_due_jobs", return_value=[job]), \
             patch("cron.scheduler.advance_next_run"), \
             patch("cron.scheduler.run_one_job", return_value=True), \
             patch("cron.scheduler.create_execution", return_value={"id": "exec-1"}) as mock_create:
            assert tick(verbose=False, sync=True, adapters=None) == 1

        mock_create.assert_called_once_with("cli-trigger-job", source="cli")

    def test_tick_records_builtin_source_with_gateway_adapters(self, tmp_path):
        """The gateway's own in-process ticker always passes `adapters` —
        those runs stay attributed as 'builtin', unchanged."""
        from cron.scheduler import tick

        job = {
            "id": "gateway-tick-job",
            "name": "gateway tick job",
            "schedule": {"kind": "interval", "seconds": 60},
            "next_run_at": "2020-01-01T00:00:00+00:00",
            "enabled": True,
        }
        with patch("cron.scheduler.get_due_jobs", return_value=[job]), \
             patch("cron.scheduler.advance_next_run"), \
             patch("cron.scheduler.run_one_job", return_value=True), \
             patch("cron.scheduler.create_execution", return_value={"id": "exec-2"}) as mock_create:
            assert tick(verbose=False, sync=True, adapters={"telegram": object()}) == 1

        mock_create.assert_called_once_with("gateway-tick-job", source="builtin")


class TestKickoffPing:
    """Verify the start-of-run kickoff ping: target reuse, config gate, non-fatal."""

    def _telegram_job(self, **extra):
        job = {
            "id": "kickoff-job",
            "name": "daily-report",
            "deliver": "origin",
            "origin": {"platform": "telegram", "chat_id": "123"},
        }
        job.update(extra)
        return job

    def _mock_gateway_cfg(self):
        from gateway.config import Platform
        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}
        return mock_cfg

    def test_kickoff_sends_lightweight_line_to_target(self):
        """A telegram-deliver job posts a one-line '🔄 Started' ping — no Cronjob envelope."""
        with patch("gateway.config.load_gateway_config", return_value=self._mock_gateway_cfg()), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock, \
             patch("cron.scheduler.load_config", return_value={"cron": {"progress_pings": True}}):
            _send_kickoff_ping(self._telegram_job(schedule_display="every day at 9am"))

        send_mock.assert_called_once()
        sent = send_mock.call_args.kwargs.get("content") or send_mock.call_args[0][-1]
        assert "🔄 Started: daily-report" in sent
        assert "every day at 9am" in sent
        assert "Cronjob Response" not in sent
        assert "job_id" not in sent

    def test_kickoff_preserves_thread_id(self):
        """Kickoff must land in the same thread as the final delivery (e.g. thread 61)."""
        job = self._telegram_job(origin={"platform": "telegram", "chat_id": "-1001", "thread_id": "61"})
        with patch("gateway.config.load_gateway_config", return_value=self._mock_gateway_cfg()), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock, \
             patch("cron.scheduler.load_config", return_value={"cron": {"progress_pings": True}}):
            _send_kickoff_ping(job)
        send_mock.assert_called_once()
        assert send_mock.call_args.kwargs["thread_id"] == "61"

    def test_kickoff_skipped_for_local_deliver(self):
        """deliver:local jobs must stay silent — no kickoff sent."""
        with patch("tools.send_message_tool._send_to_platform", new=AsyncMock()) as send_mock, \
             patch("cron.scheduler.load_config", return_value={"cron": {"progress_pings": True}}):
            _send_kickoff_ping({"id": "local-job", "name": "n", "deliver": "local"})
        send_mock.assert_not_called()

    def test_kickoff_disabled_by_config(self):
        """cron.progress_pings: false disables the kickoff entirely."""
        with patch("tools.send_message_tool._send_to_platform", new=AsyncMock()) as send_mock, \
             patch("cron.scheduler.load_config", return_value={"cron": {"progress_pings": False}}):
            _send_kickoff_ping(self._telegram_job())
        send_mock.assert_not_called()

    def test_kickoff_enabled_by_default_when_unset(self):
        """Absent config key defaults to on."""
        with patch("gateway.config.load_gateway_config", return_value=self._mock_gateway_cfg()), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock, \
             patch("cron.scheduler.load_config", return_value={}):
            _send_kickoff_ping(self._telegram_job())
        send_mock.assert_called_once()

    def test_per_job_false_overrides_global_on(self):
        """progress_ping=false silences a monitor cron even when the global switch is on."""
        with patch("tools.send_message_tool._send_to_platform", new=AsyncMock()) as send_mock, \
             patch("cron.scheduler.load_config", return_value={"cron": {"progress_pings": True}}):
            _send_kickoff_ping(self._telegram_job(progress_ping=False))
        send_mock.assert_not_called()

    def test_per_job_true_overrides_global_off(self):
        """progress_ping=true forces the ping even when the global switch is off."""
        with patch("gateway.config.load_gateway_config", return_value=self._mock_gateway_cfg()), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock, \
             patch("cron.scheduler.load_config", return_value={"cron": {"progress_pings": False}}):
            _send_kickoff_ping(self._telegram_job(progress_ping=True))
        send_mock.assert_called_once()

    def test_per_job_unset_falls_back_to_global(self):
        """No per-job key => the global default decides (here: off)."""
        with patch("tools.send_message_tool._send_to_platform", new=AsyncMock()) as send_mock, \
             patch("cron.scheduler.load_config", return_value={"cron": {"progress_pings": False}}):
            _send_kickoff_ping(self._telegram_job())  # no progress_ping key
        send_mock.assert_not_called()

    def test_kickoff_failure_is_non_fatal(self):
        """A send failure inside the ping must be swallowed, not raised."""
        with patch("cron.scheduler._send_to_targets", side_effect=RuntimeError("boom")), \
             patch("gateway.config.load_gateway_config", return_value=self._mock_gateway_cfg()), \
             patch("cron.scheduler.load_config", return_value={"cron": {"progress_pings": True}}):
            assert _send_kickoff_ping(self._telegram_job()) is None

    def test_kickoff_fires_before_run_job(self):
        """tick must post the kickoff ping before invoking run_job."""
        from unittest.mock import Mock
        manager = Mock()
        ping_mock = Mock()
        run_mock = Mock(return_value=(True, "# output", "done", None))
        manager.attach_mock(ping_mock, "ping")
        manager.attach_mock(run_mock, "run")

        with patch("cron.scheduler.get_due_jobs", return_value=[self._telegram_job()]), \
             patch("cron.scheduler._send_kickoff_ping", ping_mock), \
             patch("cron.scheduler.run_job", run_mock), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result", return_value=None), \
             patch("cron.scheduler.mark_job_run"):
            from cron.scheduler import tick
            tick(verbose=False)

        names = [c[0] for c in manager.mock_calls]
        assert "ping" in names and "run" in names
        assert names.index("ping") < names.index("run")


class TestJobProfileContextFailsClosed:
    """A job with an unresolvable profile must not silently run under the
    scheduler's default identity — see ProfileResolutionError's docstring
    for the incident this prevents (auditor briefly acting as the CEO's own
    GitHub account instead of its dedicated hermes-auditor bot)."""

    def test_valid_profile_yields_normalized_name(self, tmp_path, monkeypatch):
        from cron.fork_ext.profile_scope import _job_profile_context

        profile_dir = tmp_path / "profiles" / "auditor"
        profile_dir.mkdir(parents=True)
        monkeypatch.setattr(
            "hermes_cli.profiles.resolve_profile_env",
            lambda name: str(profile_dir),
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.normalize_profile_name", lambda name: name
        )

        with _job_profile_context("job-1", "auditor") as resolved:
            assert resolved == "auditor"

    def test_no_profile_yields_none(self):
        from cron.fork_ext.profile_scope import _job_profile_context

        with _job_profile_context("job-1", None) as resolved:
            assert resolved is None

    def test_unresolvable_profile_raises_instead_of_falling_back(self, monkeypatch):
        from cron.fork_ext.profile_scope import _job_profile_context, ProfileResolutionError

        def _boom(name):
            raise FileNotFoundError(f"Profile '{name}' does not exist.")

        monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env", _boom)
        monkeypatch.setattr(
            "hermes_cli.profiles.normalize_profile_name", lambda name: name
        )

        with pytest.raises(ProfileResolutionError):
            with _job_profile_context("auditor-job", "auditor"):
                pytest.fail("job body must not execute when profile resolution fails")

    def test_run_job_propagates_profile_resolution_error(self, monkeypatch):
        # run_job() must not swallow the error into a "ran under default
        # profile" outcome — it should surface so the caller (tick()'s
        # _process_job) marks the run as failed and retries later, rather
        # than delivering a review/action taken under the wrong identity.
        from cron.scheduler import ProfileResolutionError

        def _boom(job_id, profile):
            raise ProfileResolutionError("profile 'auditor' could not be resolved")

        monkeypatch.setattr("cron.scheduler._job_profile_context", _boom)

        with pytest.raises(ProfileResolutionError):
            run_job({"id": "auditor-job", "profile": "auditor"})


class TestJobRunLock:
    """Per-job concurrency lock: overlapping triggers of the same job_id must
    not both execute (the duplicate-auditor-review guard)."""

    def test_run_job_skips_when_job_lock_held(self, tmp_path, monkeypatch):
        import fcntl
        import cron.scheduler as sched

        monkeypatch.setattr(sched, "_get_hermes_home", lambda: tmp_path)
        (tmp_path / "cron").mkdir(parents=True, exist_ok=True)
        holder = open(tmp_path / "cron" / ".job-testjob.lock", "w")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            calls = {"n": 0}
            monkeypatch.setattr(
                sched, "_run_job_impl",
                lambda job, **kw: (calls.__setitem__("n", calls["n"] + 1), (True, "out", "resp", None))[1],
            )
            ok, out, resp, err = sched.run_job({"id": "testjob"})
            assert calls["n"] == 0                       # impl never ran
            assert (ok, out, resp, err) == (True, "", "", None)  # silent skip
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()

    def test_run_job_runs_when_lock_free(self, tmp_path, monkeypatch):
        import cron.scheduler as sched

        monkeypatch.setattr(sched, "_get_hermes_home", lambda: tmp_path)
        calls = {"n": 0}
        monkeypatch.setattr(
            sched, "_run_job_impl",
            lambda job, **kw: (calls.__setitem__("n", calls["n"] + 1), (True, "out", "resp", None))[1],
        )
        ok, out, resp, err = sched.run_job({"id": "testjob2"})
        assert calls["n"] == 1
        assert (ok, resp) == (True, "resp")


class TestDispatchJobAsync:
    """Webhook-triggered jobs must enqueue on the scheduler's own pools (so
    profile jobs serialize with tick and can't leak os.environ identity across
    concurrent runs), not run inline."""

    class _InlinePool:
        def submit(self, fn):
            import concurrent.futures
            fn()
            fut = concurrent.futures.Future()
            fut.set_result(True)
            return fut

    def test_profile_job_queues_on_sequential_pool(self, monkeypatch):
        import cron.scheduler as sched

        calls = {"run": 0, "seq": 0, "par": 0}
        monkeypatch.setattr(sched, "run_one_job", lambda job, **kw: calls.__setitem__("run", calls["run"] + 1))
        monkeypatch.setattr(sched, "_get_sequential_pool", lambda: (calls.__setitem__("seq", calls["seq"] + 1), self._InlinePool())[1])
        monkeypatch.setattr(sched, "_get_parallel_pool", lambda mw: (calls.__setitem__("par", calls["par"] + 1), self._InlinePool())[1])
        sched._running_job_ids.discard("j1")

        res = sched.dispatch_job_async({"id": "j1", "profile": "auditor"})

        assert res["queued"] is True
        assert calls == {"run": 1, "seq": 1, "par": 0}       # ran, via SEQUENTIAL pool
        assert "j1" not in sched._running_job_ids            # in-flight guard released

    def test_skips_when_already_running(self, monkeypatch):
        import cron.scheduler as sched

        calls = {"run": 0}
        monkeypatch.setattr(sched, "run_one_job", lambda job, **kw: calls.__setitem__("run", calls["run"] + 1))
        sched._running_job_ids.add("j2")
        try:
            res = sched.dispatch_job_async({"id": "j2", "profile": "auditor"})
            assert res["queued"] is False and res["reason"] == "already running"
            assert calls["run"] == 0
        finally:
            sched._running_job_ids.discard("j2")

    def test_workdirless_job_uses_parallel_pool(self, monkeypatch):
        import cron.scheduler as sched

        calls = {"seq": 0, "par": 0}
        monkeypatch.setattr(sched, "run_one_job", lambda job, **kw: None)
        monkeypatch.setattr(sched, "_get_sequential_pool", lambda: (calls.__setitem__("seq", calls["seq"] + 1), self._InlinePool())[1])
        monkeypatch.setattr(sched, "_get_parallel_pool", lambda mw: (calls.__setitem__("par", calls["par"] + 1), self._InlinePool())[1])
        sched._running_job_ids.discard("j3")

        res = sched.dispatch_job_async({"id": "j3"})  # no profile, no workdir

        assert res["queued"] is True
        assert calls == {"seq": 0, "par": 1}                 # parallel pool
        sched._running_job_ids.discard("j3")


class TestJobSubprocessIdentityTripwire:
    """A profile job must never start under another profile's ``HOME``.

    In the terminal lane ``HOME`` is the whole git/gh identity
    (``GITHUB_TOKEN``/``GH_TOKEN`` are stripped from every spawned subprocess,
    so ``~/.gitconfig`` / ``~/.git-credentials`` / ``~/.config/gh/hosts.yml``
    are the only credentials a command can reach). On 2026-09-12 a FinView
    content job ran with the auditor's ``HOME`` and opened FinView PR #245 as
    ``hermes-auditor``; the auditor skips PRs it appears to have authored, so
    the PR silently left the review queue.

    The leak itself is fixed in the terminal layer (a sandbox is no longer
    shared across profiles, and the shell snapshot can no longer override the
    per-spawn ``HOME``). This is the independent backstop: each run checks the
    identity it is about to act under rather than trusting the previous run to
    have cleaned up.
    """

    @staticmethod
    def _profiles(tmp_path, monkeypatch, active: str):
        root = tmp_path / "profiles"
        for name in ("auditor", "finview"):
            (root / name / "home").mkdir(parents=True)
        monkeypatch.setattr(
            "hermes_cli.profiles.resolve_profile_env",
            lambda name: str(root / name),
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.normalize_profile_name", lambda name: name
        )
        return root / active

    def test_sibling_profile_home_fails_closed(self, tmp_path, monkeypatch):
        from cron.fork_ext.profile_scope import _job_profile_context, ProfileIdentityError

        root = self._profiles(tmp_path, monkeypatch, "finview").parent
        # Exactly the production shape: the previous (auditor) run's HOME
        # survived into this one.
        monkeypatch.setattr(
            "hermes_constants.get_subprocess_home",
            lambda env=None: str(root / "auditor" / "home"),
        )

        with pytest.raises(ProfileIdentityError) as exc:
            with _job_profile_context("finview-job", "finview"):
                pytest.fail("the job body must not run under another identity")
        assert "another profile" in str(exc.value)

    def test_own_profile_home_is_accepted(self, tmp_path, monkeypatch):
        from cron.fork_ext.profile_scope import _job_profile_context

        own = self._profiles(tmp_path, monkeypatch, "finview")
        monkeypatch.setattr(
            "hermes_constants.get_subprocess_home",
            lambda env=None: str(own / "home"),
        )

        with _job_profile_context("finview-job", "finview") as resolved:
            assert resolved == "finview"

    def test_host_install_real_home_is_accepted(self, tmp_path, monkeypatch):
        """The ``auto`` home policy keeps the real OS-user home on non-container
        installs. That is not another profile's identity, so it must not fail —
        demanding a profile home here would break every host deployment."""
        from cron.fork_ext.profile_scope import _job_profile_context

        self._profiles(tmp_path, monkeypatch, "finview")
        real_home = tmp_path / "home" / "brais"
        real_home.mkdir(parents=True)
        monkeypatch.setattr(
            "hermes_constants.get_subprocess_home", lambda env=None: str(real_home)
        )

        with _job_profile_context("finview-job", "finview") as resolved:
            assert resolved == "finview"

    def test_no_subprocess_home_override_is_accepted(self, tmp_path, monkeypatch):
        from cron.fork_ext.profile_scope import _job_profile_context

        self._profiles(tmp_path, monkeypatch, "finview")
        monkeypatch.setattr(
            "hermes_constants.get_subprocess_home", lambda env=None: None
        )

        with _job_profile_context("finview-job", "finview") as resolved:
            assert resolved == "finview"

    def test_unprovisioned_profile_fails_closed_when_siblings_pin_identity(
        self, tmp_path, monkeypatch
    ):
        """``earthsaver`` exists in production as a bare directory — no
        ``SOUL.md``, no ``.env``, no ``home/`` — because ``resolve_profile_env``
        accepts any directory under ``profiles/`` as a profile. Its sibling
        profiles all have a ``home/``, so this install pins identity per
        profile and a job under ``earthsaver`` would silently run as the owner
        account."""
        from cron.fork_ext.profile_scope import _job_profile_context, ProfileIdentityError

        root = tmp_path / "profiles"
        (root / "earthsaver").mkdir(parents=True)
        (root / "finview" / "home").mkdir(parents=True)
        monkeypatch.setattr(
            "hermes_cli.profiles.resolve_profile_env", lambda name: str(root / name)
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.normalize_profile_name", lambda name: name
        )

        with pytest.raises(ProfileIdentityError) as exc:
            with _job_profile_context("es-job", "earthsaver"):
                pytest.fail("an unprovisioned profile must not run")
        assert "never fully" in str(exc.value)

    def test_host_install_without_any_profile_homes_still_runs(
        self, tmp_path, monkeypatch
    ):
        """The same check must stay silent on a laptop: with the ``auto`` home
        policy no profile has a ``home/`` and the real OS-user home is the
        correct answer. Calibrating off the siblings is what allows one check
        to be strict in production and quiet here."""
        from cron.fork_ext.profile_scope import _job_profile_context

        root = tmp_path / "profiles"
        (root / "finview").mkdir(parents=True)
        (root / "biglobster").mkdir(parents=True)
        monkeypatch.setattr(
            "hermes_cli.profiles.resolve_profile_env", lambda name: str(root / name)
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.normalize_profile_name", lambda name: name
        )

        with _job_profile_context("fv-job", "finview") as resolved:
            assert resolved == "finview"

    def test_environment_is_restored_even_when_the_tripwire_fires(
        self, tmp_path, monkeypatch
    ):
        """The tripwire raises from inside the context manager's ``try``, so
        the env snapshot/restore and the Hermes-home override reset must still
        run — otherwise the guard against leaking identity would itself leak."""
        import os

        from hermes_constants import get_hermes_home_override
        from cron.fork_ext.profile_scope import _job_profile_context, ProfileIdentityError

        root = self._profiles(tmp_path, monkeypatch, "finview").parent
        monkeypatch.setattr(
            "hermes_constants.get_subprocess_home",
            lambda env=None: str(root / "auditor" / "home"),
        )

        before = dict(os.environ)
        with pytest.raises(ProfileIdentityError):
            with _job_profile_context("finview-job", "finview"):
                pass
        assert dict(os.environ) == before
        assert get_hermes_home_override() is None
