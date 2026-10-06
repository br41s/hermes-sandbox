"""Cron jobs for a profile live in that profile's own store (stage 3).

The fork's per-job ``profile`` field was retired in step 2: the data layer and
the cronjob tool no longer take it, and a job runs under a profile because the
multiplex ticker (or a routed webhook) runs it from that profile's store, with
the profile's home override installed. These cover the field's absence and the
run path under that override: HERMES_HOME scoping, the ``.env`` secret scope and
the scripts dir.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import pytest


@contextmanager
def _from_profile_store(profile_home):
    """The home override the multiplex ticker installs around a profile's tick, plus
    the secret scope ``run_one_job`` installs from that home before ``run_job``."""
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(profile_home)
    scope = set_secret_scope(build_profile_secret_scope(profile_home), profile_home=str(profile_home))
    try:
        yield
    finally:
        reset_secret_scope(scope)
        reset_hermes_home_override(token)


@pytest.fixture()
def isolated_cron_profile_home(tmp_path, monkeypatch):
    """Create an isolated Hermes root with a named profile and temp cron store."""
    root = tmp_path / "hermes-root"
    profile_home = root / "profiles" / "support"
    profile_home.mkdir(parents=True)
    # v2026.9.24: a dir is a profile only with an identity marker; SOUL.md is what
    # `hermes profile create` always seeds.
    (profile_home / "SOUL.md").write_text("", encoding="utf-8")
    (root / "cron").mkdir(parents=True)

    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr("cron.jobs.CRON_DIR", root / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", root / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", root / "cron" / "output")

    return root, profile_home


class TestProfileFieldRetired:
    def test_create_job_takes_no_profile(self, isolated_cron_profile_home):
        from cron.jobs import create_job, get_job

        with pytest.raises(TypeError):
            create_job(prompt="hi", schedule="every 1h", profile="support")
        job = create_job(prompt="hi", schedule="every 1h")
        assert "profile" not in job and "profile" not in get_job(job["id"])

    def test_cronjob_tool_neither_advertises_nor_forwards_profile(self, isolated_cron_profile_home):
        import inspect

        from tools.cronjob_tools import _HANDLER_FORWARDED_ARGS, CRONJOB_SCHEMA, cronjob

        assert "profile" not in CRONJOB_SCHEMA["parameters"]["properties"]
        assert "profile" not in _HANDLER_FORWARDED_ARGS
        assert "profile" not in inspect.signature(cronjob).parameters


class TestRunJobProfileContext:
    @staticmethod
    def _install_agent_stubs(monkeypatch, observed: dict):
        import sys
        import cron.scheduler as sched

        class FakeAgent:
            def __init__(self, **kwargs):
                from hermes_constants import get_hermes_home

                observed["env_home_during_init"] = os.environ.get("HERMES_HOME")
                observed["profile_env_only_during_init"] = os.environ.get(
                    "HERMES_PROFILE_TEST_ONLY"
                )
                observed["profile_env_shared_during_init"] = os.environ.get(
                    "HERMES_PROFILE_TEST_SHARED"
                )
                from agent.secret_scope import get_secret

                observed["scoped_only_during_init"] = get_secret("HERMES_PROFILE_TEST_ONLY")
                observed["scoped_shared_during_init"] = get_secret("HERMES_PROFILE_TEST_SHARED")
                observed["hermes_home_during_init"] = str(get_hermes_home())
                observed["scheduler_home_during_init"] = str(sched._get_hermes_home())
                observed["skip_context_files"] = kwargs.get("skip_context_files")

            def run_conversation(self, *_a, **_kw):
                from hermes_constants import get_hermes_home

                observed["env_home_during_run"] = os.environ.get("HERMES_HOME")
                observed["profile_env_only_during_run"] = os.environ.get(
                    "HERMES_PROFILE_TEST_ONLY"
                )
                observed["profile_env_shared_during_run"] = os.environ.get(
                    "HERMES_PROFILE_TEST_SHARED"
                )
                from agent.secret_scope import get_secret

                observed["scoped_only_during_run"] = get_secret("HERMES_PROFILE_TEST_ONLY")
                observed["scoped_shared_during_run"] = get_secret("HERMES_PROFILE_TEST_SHARED")
                observed["hermes_home_during_run"] = str(get_hermes_home())
                observed["scheduler_home_during_run"] = str(sched._get_hermes_home())
                return {"final_response": "done", "messages": []}

            def get_activity_summary(self):
                return {"seconds_since_activity": 0.0}

            def close(self):
                observed["closed"] = True

        fake_mod = type(sys)("run_agent")
        fake_mod.AIAgent = FakeAgent
        monkeypatch.setitem(sys.modules, "run_agent", fake_mod)

        from hermes_cli import runtime_provider as runtime_provider

        monkeypatch.setattr(
            runtime_provider,
            "resolve_runtime_provider",
            lambda **_kw: {
                "provider": "test",
                "api_key": "test-key",
                "base_url": "http://test.local",
                "api_mode": "chat_completions",
            },
        )

        monkeypatch.setattr(sched, "_build_job_prompt", lambda job, prerun_script=None, **_kw: "hi")
        import cron.scheduler_delivery as sched_delivery  # v2026.9.24 moved _resolve_origin here

        monkeypatch.setattr(sched_delivery, "_resolve_origin", lambda job: None)
        monkeypatch.setattr(sched, "_resolve_delivery_target", lambda job: None)
        monkeypatch.setattr(sched, "_resolve_cron_enabled_toolsets", lambda job, cfg: None)
        monkeypatch.setattr(sched, "_hermes_home", None)
        monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")

        from hermes_cli import env_loader

        def fake_load_dotenv_with_fallback(path, *, override, **_kw):
            observed.setdefault("dotenv_paths", []).append(str(path))

        monkeypatch.setattr(env_loader, "_load_dotenv_with_fallback", fake_load_dotenv_with_fallback)

    def test_run_job_sets_and_restores_profile_home(
        self, isolated_cron_profile_home, monkeypatch
    ):
        import cron.scheduler as sched

        root, profile_home = isolated_cron_profile_home
        (profile_home / ".env").write_text("", encoding="utf-8")
        observed: dict = {}
        self._install_agent_stubs(monkeypatch, observed)

        job = {
            "id": "abc",
            "name": "profile-job",
            "schedule_display": "manual",
        }

        with _from_profile_store(profile_home):
            success, _output, response, error = sched.run_job(job)

        assert success is True, f"run_job failed: error={error!r} response={response!r}"
        # The profile's .env is the run's secret scope, never loaded into
        # os.environ (cron/fork_ext/profile_scope.py).
        assert "dotenv_paths" not in observed
        assert observed["env_home_during_init"] == str(root)
        assert observed["env_home_during_run"] == str(root)
        assert observed["hermes_home_during_init"] == str(profile_home.resolve())
        assert observed["hermes_home_during_run"] == str(profile_home.resolve())
        assert observed["scheduler_home_during_init"] == str(profile_home.resolve())
        assert observed["scheduler_home_during_run"] == str(profile_home.resolve())
        assert observed["skip_context_files"] is True
        assert os.environ["HERMES_HOME"] == str(root)
        assert sched._get_hermes_home() == root

    def test_profile_dotenv_is_scoped_not_loaded_into_os_environ(
        self, isolated_cron_profile_home, monkeypatch
    ):
        """A profile job reads its own .env through the secret scope, and the
        process environment never carries it — so a job running concurrently
        on another thread cannot read the profile's keys. This is the
        guarantee that lets upstream v2026.8.31 drop _terminal_cwd_lock."""
        import cron.scheduler as sched

        root, profile_home = isolated_cron_profile_home
        (profile_home / ".env").write_text(
            "HERMES_PROFILE_TEST_SHARED=profile-value\n"
            "HERMES_PROFILE_TEST_ONLY=profile-only\n",
            encoding="utf-8",
        )
        observed: dict = {}
        self._install_agent_stubs(monkeypatch, observed)
        monkeypatch.setenv("HERMES_PROFILE_TEST_SHARED", "outer")
        monkeypatch.delenv("HERMES_PROFILE_TEST_ONLY", raising=False)

        job = {
            "id": "env-profile",
            "name": "profile-env-job",
            "schedule_display": "manual",
        }

        with _from_profile_store(profile_home):
            success, _output, _response, error = sched.run_job(job)

        assert success is True, error
        assert "dotenv_paths" not in observed
        # The job itself sees its profile's values through get_secret ...
        assert observed["scoped_only_during_init"] == "profile-only"
        assert observed["scoped_shared_during_init"] == "profile-value"
        assert observed["scoped_only_during_run"] == "profile-only"
        assert observed["scoped_shared_during_run"] == "profile-value"
        # ... while the process environment is untouched throughout.
        assert observed["profile_env_only_during_init"] is None
        assert observed["profile_env_shared_during_init"] == "outer"
        assert observed["profile_env_only_during_run"] is None
        assert observed["profile_env_shared_during_run"] == "outer"
        assert os.environ["HERMES_PROFILE_TEST_SHARED"] == "outer"
        assert "HERMES_PROFILE_TEST_ONLY" not in os.environ
        assert os.environ["HERMES_CRON_TIMEOUT"] == "0"
        assert os.environ["HERMES_HOME"] == str(root)
        assert sched._get_hermes_home() == root

    def test_no_agent_profile_uses_profile_scripts_dir_and_restores_env(
        self, isolated_cron_profile_home, monkeypatch
    ):
        import cron.scheduler as sched

        root, profile_home = isolated_cron_profile_home
        scripts_dir = profile_home / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "print_home.py").write_text(
            "import os\nprint(os.environ.get('HERMES_HOME', ''))\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(sched, "_hermes_home", None)

        job = {
            "id": "script1",
            "name": "profile-script",
            "script": "print_home.py",
            "no_agent": True,
        }

        with _from_profile_store(profile_home):
            success, _doc, response, error = sched.run_job(job)

        assert success is True, error
        assert response.strip() == str(profile_home.resolve())
        assert os.environ["HERMES_HOME"] == str(root)
        assert sched._get_hermes_home() == root

    def test_run_job_without_profile_leaves_hermes_home_untouched(
        self, isolated_cron_profile_home, monkeypatch
    ):
        import cron.scheduler as sched

        root, _profile_home = isolated_cron_profile_home
        observed: dict = {}
        self._install_agent_stubs(monkeypatch, observed)

        job = {
            "id": "noprof",
            "name": "no-profile-job",
            "schedule_display": "manual",
        }

        success, *_ = sched.run_job(job)

        assert success is True
        assert observed["hermes_home_during_init"] == str(root)
        assert os.environ["HERMES_HOME"] == str(root)

class TestTickProfilePartition:
    def test_profile_store_and_workdir_combined(self, isolated_cron_profile_home, monkeypatch):
        """A profile-store job with a workdir — verify both are applied and restored."""
        import cron.scheduler as sched

        root, profile_home = isolated_cron_profile_home
        observed: dict = {}
        TestRunJobProfileContext._install_agent_stubs(monkeypatch, observed)
        fake_workdir = str(root / "myproject")
        (root / "myproject").mkdir()

        job = {
            "id": "combo",
            "name": "combo-job",
            "workdir": fake_workdir,
            "schedule_display": "manual",
        }

        with _from_profile_store(profile_home):
            success, _output, _response, error = sched.run_job(job)

        assert success is True, error
        assert observed["hermes_home_during_init"] == str(profile_home.resolve())
        assert os.environ.get("TERMINAL_CWD", "") != fake_workdir, \
            "TERMINAL_CWD should be restored after job"
        assert os.environ["HERMES_HOME"] == str(root)
        assert sched._get_hermes_home() == root

    def test_workdir_jobs_share_the_parallel_pool(self, isolated_cron_profile_home, monkeypatch):
        import threading
        import cron.scheduler as sched

        root, _profile_home = isolated_cron_profile_home
        profile_job = {"id": "a", "name": "A", "workdir": str(root)}
        parallel_job = {"id": "b", "name": "B"}

        monkeypatch.setattr(sched, "get_due_jobs", lambda: [profile_job, parallel_job])
        monkeypatch.setattr(sched, "advance_next_runs", lambda *_a, **_kw: None)
        _jobs = {j["id"]: j for j in (profile_job, parallel_job)}
        monkeypatch.setattr(
            sched, "claim_job_for_fire",
            lambda jid, return_job=False, **_kw: dict(_jobs[jid]) if return_job else True,
        )
        monkeypatch.setattr(sched, "claim_dispatch", lambda *_a, **_kw: True)
        monkeypatch.setattr(sched, "_send_kickoff_ping", lambda *_a, **_kw: None)

        calls: list[tuple[str, str]] = []

        def fake_run_job(job, **_kw):
            calls.append((job["id"], threading.current_thread().name))
            return True, "output", "response", None

        monkeypatch.setattr(sched, "run_job", fake_run_job)
        monkeypatch.setattr(sched, "save_job_output", lambda _jid, _o: None)
        monkeypatch.setattr(sched, "mark_job_run", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "_deliver_result", lambda *_a, **_kw: None)

        n = sched.tick(verbose=False)

        assert n == 2
        ids = [job_id for job_id, _thread_name in calls]
        # Each ran exactly once. No order between them: main's pool is unbounded.
        assert sorted(ids) == ["a", "b"]
        # Stage 3 step 5 retired the fork's single-thread lane: a workdir job runs on the
        # store's own pool beside a plain one (each run gets its own isolated checkout),
        # never on a separate "cron-seq" thread, and never inline on the ticker's thread.
        assert all(thread.startswith("cron-parallel") for _job_id, thread in calls)


class TestProfileHomeDoesNotLeakAcrossThreads:
    """A profile-store run must not change HERMES_HOME for jobs on other threads.

    Regression for the 2026-07-31 production incident: the old per-job profile
    context also assigned the module-global ``cron.scheduler._hermes_home``,
    which ``_get_hermes_home()`` prefers over ``get_hermes_home()``. The global
    is process-wide, so while a profile job ran, EVERY concurrent job resolved
    its home — and therefore its ``scripts/`` dir — under that profile: the
    hourly incident-watcher failed with
    ``Script not found: /opt/data/profiles/biglobster/scripts/incident_sweep.sh``.

    The fix relies on ``set_hermes_home_override`` being a ContextVar, which is
    per-thread and takes precedence in ``get_hermes_home()``; the multiplex
    ticker and ``_satellite_store_context`` both stand on it.
    """

    def test_concurrent_launch_store_job_keeps_the_default_home(
        self, isolated_cron_profile_home, monkeypatch
    ):
        import threading

        from cron import scheduler
        from cron.fork_ext.profile_scope import _satellite_store_context

        root, profile_home = isolated_cron_profile_home
        monkeypatch.setattr(scheduler, "_hermes_home", None)

        inside_profile = threading.Event()
        release_profile = threading.Event()
        observed: dict[str, object] = {}

        def _profile_job():
            # Hold the profile run open so the other thread is guaranteed to
            # observe the process state while it is active.
            with _from_profile_store(profile_home), _satellite_store_context("job-profile"):
                inside_profile.set()
                release_profile.wait(timeout=5)

        def _plain_job():
            inside_profile.wait(timeout=5)
            with _satellite_store_context("job-plain"):
                observed["home"] = scheduler._get_hermes_home()
            release_profile.set()

        threads = [
            threading.Thread(target=_profile_job),
            threading.Thread(target=_plain_job),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert observed["home"] == root, (
            "a launch-store job resolved its Hermes home under the concurrently "
            f"running profile: {observed['home']}"
        )
        # The concrete symptom: script lookups land in the wrong profile.
        assert observed["home"] / "scripts" == root / "scripts"

    def test_profile_store_run_still_scopes_its_own_home(
        self, isolated_cron_profile_home, monkeypatch
    ):
        """Guard the fix didn't over-reach — scoping must still work in-thread."""
        from cron import scheduler
        from cron.fork_ext.profile_scope import _satellite_store_context

        root, profile_home = isolated_cron_profile_home
        monkeypatch.setattr(scheduler, "_hermes_home", None)

        with _from_profile_store(profile_home), _satellite_store_context("job-profile"):
            assert scheduler._get_hermes_home() == profile_home.resolve()
        assert scheduler._get_hermes_home() == root
