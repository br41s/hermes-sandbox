"""Fork's own tests for cron/scheduler.py, kept out of upstream's file so upstream merges do not conflict."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cron.scheduler import _resolve_delivery_target, _send_kickoff_ping, run_job


class TestHomeDeliveryTarget:
    """A launch-store job's bare platform ``deliver`` lands on the global home
    channel/thread. The fork's per-job ``profile`` routing (2026-07-08, the
    BigLobster/FinView/Infographic jobs on the main Hermes thread) went with the
    field in stage 3 step 2: a job in a profile's own store reads that profile's
    routing env through its secret scope
    (``test_multiplex_default_delivery_fork.py``)."""

    @pytest.fixture
    def profile_root(self, tmp_path, monkeypatch):
        root = tmp_path / "hermes-root"
        (root / "profiles" / "biglobster").mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(root))
        return root

    def _write_profile_env(self, root, profile, content):
        (root / "profiles" / profile / ".env").write_text(content, encoding="utf-8")

    def test_profile_scoped_job_without_profile_field_uses_global(self, monkeypatch, profile_root):
        """A profile's routing env never reaches a launch-store job."""
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
            "_resolved_from": "home",  # upstream v2026.9.24 tags home-channel targets
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
             patch("cron.scheduler.advance_next_runs"), \
             patch("cron.scheduler.claim_job_for_fire", side_effect=lambda jid, return_job=False, **_kw: dict(job)), \
             patch("cron.scheduler.run_one_job", return_value=True), \
             patch("cron.scheduler.create_execution", return_value={"id": "exec-1"}) as mock_create:
            assert tick(verbose=False, sync=True, adapters=None) == 1

        mock_create.assert_called_once()
        assert mock_create.call_args.args == ("cli-trigger-job",)
        assert mock_create.call_args.kwargs["source"] == "cli"  # upstream adds scheduled_instant=

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
             patch("cron.scheduler.advance_next_runs"), \
             patch("cron.scheduler.claim_job_for_fire", side_effect=lambda jid, return_job=False, **_kw: dict(job)), \
             patch("cron.scheduler.run_one_job", return_value=True), \
             patch("cron.scheduler.create_execution", return_value={"id": "exec-2"}) as mock_create:
            assert tick(verbose=False, sync=True, adapters={"telegram": object()}) == 1

        mock_create.assert_called_once()
        assert mock_create.call_args.args == ("gateway-tick-job",)
        assert mock_create.call_args.kwargs["source"] == "builtin"  # upstream adds scheduled_instant=


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

        job = self._telegram_job()
        with patch("cron.scheduler.get_due_jobs", return_value=[job]), \
             patch("cron.scheduler.advance_next_runs"), \
             patch("cron.scheduler.claim_job_for_fire", side_effect=lambda jid, return_job=False, **_kw: dict(job)), \
             patch("cron.scheduler.claim_dispatch", return_value=True), \
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


class TestLegacyProfileRecordRefused:
    """Stage 3 step 2 retired the fork's per-job ``profile`` field. A record that
    still carries it must never run under the scheduler's default identity (the
    auditor once acted as the CEO's own GitHub account that way): it is refused
    with an error naming ``hermes cron move``, and the caller marks the run
    failed, which alerts."""

    def test_a_record_carrying_profile_is_refused_before_it_runs(self, tmp_path, monkeypatch):
        import cron.scheduler as sched
        from cron.fork_ext.run_guard import guarded_run_job
        from cron.scheduler import LegacyProfileJobError

        monkeypatch.setattr(sched, "_get_hermes_home", lambda: tmp_path)

        with pytest.raises(LegacyProfileJobError) as exc:
            guarded_run_job({"id": "auditor-job", "profile": "auditor"},
                            lambda job: pytest.fail("a legacy profile record must not run"))
        assert "hermes cron move auditor-job --to-profile auditor --apply" in str(exc.value)

    def test_run_job_propagates_the_refusal(self, tmp_path, monkeypatch):
        # run_job() must not swallow the error into a "ran under the default
        # profile" outcome — tick()'s _process_job marks the run failed.
        import cron.scheduler as sched
        from cron.scheduler import LegacyProfileJobError

        monkeypatch.setattr(sched, "_get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(sched, "_run_job_impl",
                            lambda job, **kw: pytest.fail("a legacy profile record must not run"))

        with pytest.raises(LegacyProfileJobError):
            run_job({"id": "auditor-job", "profile": "auditor"})

    @pytest.mark.parametrize("profile", [None, "", "   ", "default", "Default"])
    def test_an_empty_or_default_profile_value_is_no_profile(self, tmp_path, monkeypatch, profile):
        """``default`` ran under the root home before step 2, as a plain job does."""
        import cron.scheduler as sched
        from cron.fork_ext.run_guard import guarded_run_job

        monkeypatch.setattr(sched, "_get_hermes_home", lambda: tmp_path)

        assert guarded_run_job({"id": "plain", "profile": profile},
                               lambda job: (True, "ran", "", None)) == (True, "ran", "", None)


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


    def test_run_job_forwards_every_keyword_to_impl(self, tmp_path, monkeypatch):
        """The wrapper must not name upstream's keywords: from v2026.8.31
        run_one_job also passes extra_prompt / execution_id / cancel_event,
        and a wrapper that rejected them would fail every cron run."""
        import cron.scheduler as sched

        monkeypatch.setattr(sched, "_get_hermes_home", lambda: tmp_path)
        seen = {}
        monkeypatch.setattr(
            sched, "_run_job_impl",
            lambda job, **kw: (seen.update(kw), (True, "out", "resp", None))[1],
        )
        holder: list = []
        sched.run_job(
            {"id": "kwjob"}, defer_agent_teardown=holder,
            extra_prompt="x", execution_id="e1", cancel_event=None,
        )
        assert seen == {
            "defer_agent_teardown": holder, "extra_prompt": "x",
            "execution_id": "e1", "cancel_event": None,
        }


class TestDispatchJobAsync:
    """Webhook-triggered jobs enqueue on the pool tick uses for their store (sized 1
    for a profile, so they queue behind its tick jobs), never inline in the gateway
    event loop. Stage 3 step 5 removed the fork's lane: a workdir job is no exception."""

    class _InlinePool:
        def submit(self, fn):
            import concurrent.futures
            fn()
            fut = concurrent.futures.Future()
            fut.set_result(True)
            return fut

    @pytest.mark.parametrize("job_id, fields", [("j1", {"workdir": "/repo"}), ("j3", {})])
    def test_every_job_queues_on_its_stores_pool(self, monkeypatch, job_id, fields):
        import cron.scheduler as sched

        calls = {"run": 0, "par": 0}
        monkeypatch.setattr(sched, "run_one_job", lambda job, **kw: calls.__setitem__("run", calls["run"] + 1))
        monkeypatch.setattr(sched, "_get_parallel_pool", lambda mw: (calls.__setitem__("par", calls["par"] + 1), self._InlinePool())[1])

        res = sched.dispatch_job_async({"id": job_id, **fields})

        assert res == {"queued": True, "reason": None}
        assert calls == {"run": 1, "par": 1}                 # ran, via the store's pool
        assert sched.try_register_running_job(job_id), "in-flight guard was not released"
        sched.release_running_job(job_id)

    def test_skips_when_already_running(self, monkeypatch):
        import cron.scheduler as sched

        calls = {"run": 0}
        monkeypatch.setattr(sched, "run_one_job", lambda job, **kw: calls.__setitem__("run", calls["run"] + 1))
        # The public claim API, not a raw add: since v2026.9.24 the in-flight set is keyed by
        # (job id, home), so a bare id no longer registers anything.
        assert sched.try_register_running_job("j2")
        try:
            res = sched.dispatch_job_async({"id": "j2", "workdir": "/repo"})
            assert res["queued"] is False and res["reason"] == "already running"
            assert calls["run"] == 0
        finally:
            sched.release_running_job("j2")


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

    # Since stage 3 step 2 every profile job runs from its profile's OWN cron
    # store (step 0b), so the tripwire fires through ``guarded_run_job``'s
    # satellite path.

    @staticmethod
    def _satellite_run(home, job, impl):
        from cron.fork_ext.run_guard import guarded_run_job
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        token = set_hermes_home_override(str(home))
        try:
            return guarded_run_job(job, impl)
        finally:
            reset_hermes_home_override(token)

    def test_satellite_store_job_under_a_sibling_home_fails_closed(self, tmp_path, monkeypatch):
        from cron.fork_ext.profile_scope import ProfileIdentityError

        own = self._profiles(tmp_path, monkeypatch, "finview")
        monkeypatch.setattr(
            "hermes_constants.get_subprocess_home",
            lambda env=None: str(own.parent / "auditor" / "home"),
        )

        def impl(job):
            pytest.fail("a satellite job must not run under another identity")

        with pytest.raises(ProfileIdentityError, match="another profile"):
            self._satellite_run(own, {"id": "fv-sat"}, impl)

    def test_satellite_store_job_under_its_own_home_runs_as_a_profile_run(
        self, tmp_path, monkeypatch
    ):
        from hermes_cli.fork_ext import profile_env

        own = self._profiles(tmp_path, monkeypatch, "finview")
        monkeypatch.setattr(
            "hermes_constants.get_subprocess_home", lambda env=None: str(own / "home")
        )

        def impl(job):
            return profile_env._IN_PROFILE_RUN.get(), "", "", None

        assert self._satellite_run(own, {"id": "fv-sat"}, impl) == (True, "", "", None)

    def test_satellite_store_job_from_an_unprovisioned_profile_fails_closed(
        self, tmp_path, monkeypatch
    ):
        from cron.fork_ext.profile_scope import ProfileIdentityError

        root = tmp_path / "profiles"
        (root / "earthsaver").mkdir(parents=True)
        (root / "finview" / "home").mkdir(parents=True)

        with pytest.raises(ProfileIdentityError, match="never fully"):
            self._satellite_run(root / "earthsaver", {"id": "es-sat"},
                                lambda job: pytest.fail("must not run"))

    def test_launch_store_plain_job_skips_the_tripwire(self, monkeypatch):
        from cron.fork_ext import profile_scope
        from cron.fork_ext.run_guard import guarded_run_job
        from hermes_cli.fork_ext import profile_env

        monkeypatch.setattr(
            profile_scope, "_assert_own_subprocess_identity",
            lambda *a: pytest.fail("the launch store's plain jobs carry no profile identity"))

        def impl(job):
            return profile_env._IN_PROFILE_RUN.get(), "", "", None

        assert guarded_run_job({"id": "plain"}, impl) == (False, "", "", None)

    def test_host_install_real_home_is_accepted(self, tmp_path, monkeypatch):
        """The ``auto`` home policy keeps the real OS-user home on non-container
        installs. That is not another profile's identity, so it must not fail —
        demanding a profile home here would break every host deployment."""
        own = self._profiles(tmp_path, monkeypatch, "finview")
        real_home = tmp_path / "home" / "brais"
        real_home.mkdir(parents=True)
        monkeypatch.setattr(
            "hermes_constants.get_subprocess_home", lambda env=None: str(real_home)
        )

        assert self._satellite_run(own, {"id": "fv-sat"},
                                   lambda job: (True, "", "", None))[0] is True

    def test_no_subprocess_home_override_is_accepted(self, tmp_path, monkeypatch):
        own = self._profiles(tmp_path, monkeypatch, "finview")
        monkeypatch.setattr(
            "hermes_constants.get_subprocess_home", lambda env=None: None
        )

        assert self._satellite_run(own, {"id": "fv-sat"},
                                   lambda job: (True, "", "", None))[0] is True

    def test_host_install_without_any_profile_homes_still_runs(
        self, tmp_path, monkeypatch
    ):
        """The same check must stay silent on a laptop: with the ``auto`` home
        policy no profile has a ``home/`` and the real OS-user home is the
        correct answer. Calibrating off the siblings is what allows one check
        to be strict in production and quiet here."""
        root = tmp_path / "profiles"
        (root / "finview").mkdir(parents=True)
        (root / "biglobster").mkdir(parents=True)

        assert self._satellite_run(root / "finview", {"id": "fv-sat"},
                                   lambda job: (True, "", "", None))[0] is True

    def test_a_profile_job_is_checked_once_not_twice(self, tmp_path, monkeypatch):
        from cron.fork_ext import profile_scope

        own = self._profiles(tmp_path, monkeypatch, "finview")
        calls = []
        monkeypatch.setattr(profile_scope, "_assert_own_subprocess_identity",
                            lambda *a: calls.append(a[0]))

        assert self._satellite_run(own, {"id": "fv-job"},
                                   lambda job: (True, "", "", None))[0] is True
        assert calls == ["fv-job"]

    def test_environment_is_restored_even_when_the_tripwire_fires(
        self, tmp_path, monkeypatch
    ):
        """The tripwire raises before ``profile_run()`` is entered, so neither
        ``os.environ`` nor the profile-run flag may be left changed — otherwise
        the guard against leaking identity would itself leak."""
        import os

        from cron.fork_ext.profile_scope import ProfileIdentityError
        from hermes_cli.fork_ext import profile_env

        own = self._profiles(tmp_path, monkeypatch, "finview")
        monkeypatch.setattr(
            "hermes_constants.get_subprocess_home",
            lambda env=None: str(own.parent / "auditor" / "home"),
        )

        before = dict(os.environ)
        with pytest.raises(ProfileIdentityError):
            self._satellite_run(own, {"id": "fv-sat"}, lambda job: (True, "", "", None))
        assert dict(os.environ) == before
        assert not profile_env._IN_PROFILE_RUN.get()


class TestKickoffPingFromAProfileStore:
    """Stage 3 step 0g (plan fact 12): a job in a profile's own store kept its
    result delivery but lost its "🔄 Started" ping, for two reasons: the ping ran
    before the run's secret scope (``get_secret`` raises under multiplex), and it
    resolved its transport target-less, which ``SharedRouteAdapters`` refuses."""

    @staticmethod
    def _satellite(tmp_path, monkeypatch, routes):
        import yaml

        root = tmp_path / "root"
        home = root / "profiles" / "fitness"
        home.mkdir(parents=True)
        (root / "config.yaml").write_text(yaml.safe_dump(
            {"gateway": {"multiplex_profiles": True, "profile_routes": routes}}), encoding="utf-8")
        monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: root)
        return home

    @staticmethod
    def _send(job, adapters, *, satellite_platforms=None):
        import asyncio
        from concurrent.futures import Future

        loop = MagicMock()
        loop.is_running.return_value = True

        def fake_run_coro(coro, _loop):
            future = Future()
            future.set_result(asyncio.run(coro))
            return future

        standalone = []

        async def fake_standalone(platform, pconfig, chat_id, text, **kwargs):
            standalone.append(chat_id)
            return {"success": False, "error": "DISCORD_BOT_TOKEN is not set"}

        from gateway.config import Platform, PlatformConfig
        config = MagicMock()
        config.platforms = ({Platform.DISCORD: PlatformConfig(enabled=True)}
                            if satellite_platforms is None else satellite_platforms)
        config.get_home_channel = lambda p: None
        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("cron.scheduler.load_config", return_value={"cron": {"progress_pings": True}}), \
             patch("tools.send_message_tool._send_to_platform", fake_standalone), \
             patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro):
            _send_kickoff_ping(job, adapters=adapters, loop=loop)
        return standalone

    @staticmethod
    def _primary():
        adapter = MagicMock()
        adapter.sent = []

        async def send(chat_id, content, metadata=None):
            adapter.sent.append((chat_id, content))
            return {"success": True, "message_id": "m1"}

        adapter.send = send
        return adapter

    def test_a_routed_target_pings_through_the_primary_adapter(self, tmp_path, monkeypatch):
        from cron.scheduler_preflight import SharedRouteAdapters, _primary_profile_routes_for_current_home
        from gateway.config import Platform
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        home = self._satellite(tmp_path, monkeypatch, [
            {"platform": "discord", "chat_id": "C1", "profile": "fitness"},
            {"platform": "discord", "chat_id": "C9", "profile": "other"}])
        primary = self._primary()
        token = set_hermes_home_override(str(home))
        try:
            shared = SharedRouteAdapters({Platform.DISCORD: primary},
                                         _primary_profile_routes_for_current_home())
            standalone = self._send({"id": "j1", "name": "brief", "deliver": "discord:C1"}, shared)
            assert [chat for chat, _ in primary.sent] == ["C1"] and standalone == []
            assert "🔄 Started: brief" in primary.sent[0][1]

            # Another profile's chat: the primary bot is never used for it.
            primary.sent.clear()
            standalone = self._send({"id": "j2", "name": "brief", "deliver": "discord:C9"}, shared)
            assert primary.sent == [] and standalone == ["C9"]
        finally:
            reset_hermes_home_override(token)

    def test_a_satellite_whose_own_platform_block_is_disabled_still_pings(self, tmp_path, monkeypatch):
        """Production's shape: the satellite's own block for a platform it holds no
        credential for reads ``enabled: false``. Re-resolving the transport from that
        config refuses the shared adapter, so
        the ping must reuse the transport the shared route already authorized — or it
        falls back to a standalone send with no token (2026-10-03 canary, be8a4add42b0)."""
        from cron.scheduler_preflight import SharedRouteAdapters, _primary_profile_routes_for_current_home
        from gateway.config import Platform, PlatformConfig
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        home = self._satellite(tmp_path, monkeypatch, [
            {"platform": "discord", "chat_id": "C1", "profile": "fitness"}])
        primary = self._primary()
        token = set_hermes_home_override(str(home))
        try:
            shared = SharedRouteAdapters({Platform.DISCORD: primary},
                                         _primary_profile_routes_for_current_home())
            standalone = self._send({"id": "j1", "name": "brief", "deliver": "discord:C1"}, shared,
                                    satellite_platforms={Platform.DISCORD: PlatformConfig(enabled=False)})
        finally:
            reset_hermes_home_override(token)
        assert standalone == [], "fell back to a token-less standalone send"
        assert [chat for chat, _ in primary.sent] == ["C1"]

    def test_a_bare_deliver_resolves_its_home_target_under_multiplex(self, tmp_path, monkeypatch):
        from agent import secret_scope
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        home = self._satellite(tmp_path, monkeypatch, [])
        (home / ".env").write_text(
            "TELEGRAM_HOME_CHANNEL=-100777\nTELEGRAM_HOME_CHANNEL_THREAD_ID=3\n", encoding="utf-8")
        captured = []
        token = set_hermes_home_override(str(home))
        secret_scope.set_multiplex_active(True)
        try:
            assert secret_scope.current_secret_scope() is None  # the ticker thread has none
            with patch("cron.scheduler._send_to_targets",
                       side_effect=lambda job, targets, *a, **k: captured.extend(targets) or []), \
                 patch("gateway.config.load_gateway_config", return_value=MagicMock()), \
                 patch("cron.scheduler.load_config", return_value={"cron": {"progress_pings": True}}):
                _send_kickoff_ping({"id": "j3", "name": "weekly", "deliver": "telegram"})
            assert secret_scope.current_secret_scope() is None  # and still has none after
        finally:
            secret_scope.set_multiplex_active(False)
            reset_hermes_home_override(token)
        assert [(t["platform"], str(t["chat_id"]), str(t.get("thread_id"))) for t in captured] == [
            ("telegram", "-100777", "3")]

    def test_a_scope_the_caller_installed_is_kept(self, tmp_path, monkeypatch):
        from agent import secret_scope

        seen = []
        token = secret_scope.set_secret_scope({"TELEGRAM_HOME_CHANNEL": "-1"})
        try:
            with patch("cron.scheduler._resolve_delivery_targets",
                       side_effect=lambda job: seen.append(secret_scope.current_secret_scope()) or []), \
                 patch("cron.scheduler.load_config", return_value={"cron": {"progress_pings": True}}):
                _send_kickoff_ping({"id": "j4", "name": "n", "deliver": "telegram"})
        finally:
            secret_scope.reset_secret_scope(token)
        assert seen == [{"TELEGRAM_HOME_CHANNEL": "-1"}]
