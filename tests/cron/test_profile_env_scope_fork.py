"""A cron profile job's .env is scoped to its run, never written to os.environ.

Upstream v2026.8.31 removes ``_terminal_cwd_lock``, which today is what keeps
a plain job from running while a profile job's keys sit in ``os.environ``.
These tests pin the replacement guarantee: the profile's ``.env`` is the run's
secret scope, the readers that pick a client's site/keys see it, the child
processes it spawns inherit it, and nothing on another thread ever does.
"""

import os
import threading

import pytest


@pytest.fixture
def rental_profile(tmp_path, monkeypatch):
    root = tmp_path / "hermes-root"
    profile_home = root / "profiles" / "rental"
    profile_home.mkdir(parents=True)
    (root / ".env").write_text("BL_SITE_URL=https://default.example\n", encoding="utf-8")
    (profile_home / ".env").write_text(
        "BL_SITE_URL=https://client.example\n"
        "FAL_KEY=fal-client-key\n"
        "OPENROUTER_API_KEY=or-client-key\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    for key in ("BL_SITE_URL", "FAL_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-process-key")
    return root, profile_home


def _profile_context():
    from cron.fork_ext.profile_scope import _job_profile_context

    return _job_profile_context("job-1", "rental")


def test_profile_keys_reach_scoped_readers_but_not_os_environ(rental_profile):
    from agent.secret_scope import get_secret
    from hermes_cli.config import get_env_value

    before = dict(os.environ)
    with _profile_context():
        assert get_env_value("BL_SITE_URL") == "https://client.example"
        # A key the process env also holds: the profile's value must win
        # (auditor/llm and the bl_site_* tools read through get_env_value).
        assert get_env_value("OPENROUTER_API_KEY") == "or-client-key"
        assert get_secret("FAL_KEY") == "fal-client-key"
        assert get_secret("OPENROUTER_API_KEY") == "or-client-key"
        assert "BL_SITE_URL" not in os.environ
        assert "FAL_KEY" not in os.environ
        assert os.environ["OPENROUTER_API_KEY"] == "or-process-key"
    assert dict(os.environ) == before


def test_profile_scope_replaces_the_schedulers_default_scope(rental_profile):
    """run_one_job installs the scheduler's own .env as the scope BEFORE the
    profile context is entered; the profile's scope must win inside the run
    and the default one must come back after it."""
    from agent.secret_scope import (
        build_profile_secret_scope,
        get_secret,
        reset_secret_scope,
        set_secret_scope,
    )

    root, _profile_home = rental_profile
    token = set_secret_scope(build_profile_secret_scope(root))
    try:
        assert get_secret("BL_SITE_URL") == "https://default.example"
        with _profile_context():
            assert get_secret("BL_SITE_URL") == "https://client.example"
        assert get_secret("BL_SITE_URL") == "https://default.example"
    finally:
        reset_secret_scope(token)


def test_a_concurrent_job_on_another_thread_never_sees_profile_keys(rental_profile):
    from hermes_cli.config import get_env_value

    inside = threading.Event()
    done = threading.Event()
    seen = {}

    def plain_job():
        inside.wait(5)
        seen["BL_SITE_URL"] = get_env_value("BL_SITE_URL")
        seen["environ"] = os.environ.get("BL_SITE_URL")
        done.set()

    worker = threading.Thread(target=plain_job)
    worker.start()
    with _profile_context():
        inside.set()
        assert done.wait(5)
    worker.join(5)

    # The plain job resolves the default home's value, not the client's.
    assert seen["BL_SITE_URL"] == "https://default.example"
    assert seen["environ"] is None


def test_children_of_a_profile_run_inherit_its_keys_through_the_usual_filter(rental_profile):
    from hermes_cli.fork_ext.profile_env import child_env_overlay
    from tools.environments.local import _make_run_env, _sanitize_subprocess_env

    assert child_env_overlay() == {}
    with _profile_context():
        assert child_env_overlay()["BL_SITE_URL"] == "https://client.example"
        for env in (_sanitize_subprocess_env(os.environ.copy()), _make_run_env({})):
            assert env["BL_SITE_URL"] == "https://client.example"
            # Provider keys stay stripped from children, as before.
            assert "OPENROUTER_API_KEY" not in env
            assert "FAL_KEY" not in env
    assert child_env_overlay() == {}
    assert "BL_SITE_URL" not in _sanitize_subprocess_env(os.environ.copy())


def test_fal_submits_with_the_profiles_own_key(rental_profile, monkeypatch):
    from hermes_cli.fork_ext import profile_env

    monkeypatch.setattr(profile_env, "_fal_clients", {})

    class FakeSyncClient:
        def __init__(self, key):
            self.key = key

    class FakeFal:
        SyncClient = FakeSyncClient

    with _profile_context():
        client = profile_env.fal_client_for_current_key(FakeFal)
        assert isinstance(client, FakeSyncClient) and client.key == "fal-client-key"
        assert profile_env.fal_client_for_current_key(FakeFal) is client

    # No key in scope or env: the module itself, i.e. the old call.
    assert profile_env.fal_client_for_current_key(FakeFal) is FakeFal
