"""Fork: GitHub tokens are stripped on the terminal spawn path even if the blocklist forgets them.

Moved out of test_local_env_blocklist.py so upstream's file merges clean.
"""

import os
from unittest.mock import MagicMock, patch

from tools.environments.local import LocalEnvironment


# Upstream's test_local_env_blocklist.py dropped these helpers in v0.22; kept here.
def _make_fake_popen(captured: dict):
    """Return a fake Popen constructor that records the env kwarg."""
    def fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env", {})
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.returncode = 0
        proc.stdout = MagicMock(__iter__=lambda s: iter([]), __next__=lambda s: (_ for _ in ()).throw(StopIteration))
        proc.stdin = MagicMock()
        return proc
    return fake_popen


def _run_with_env(extra_os_env=None, self_env=None):
    """Execute a command via LocalEnvironment with mocked Popen
    and return the env dict passed to the subprocess."""
    captured = {}
    test_environ = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/user",
        "USER": "testuser",
    }
    if extra_os_env:
        test_environ.update(extra_os_env)

    env = LocalEnvironment(cwd="/tmp", timeout=10, env=self_env)

    with patch("tools.environments.local._find_bash", return_value="/bin/bash"), \
         patch("subprocess.Popen", side_effect=_make_fake_popen(captured)), \
         patch.dict(os.environ, test_environ, clear=True):
        env.execute("echo hello")

    return captured.get("env", {})


class TestTier1AppliesToTerminalPath:
    """Regression for the auditor identity leak (biglobster#408): a cron
    ``AIAgent`` run under a profile (e.g. ``auditor``) executes its ``gh``
    calls through the terminal tool's spawn path (``_make_run_env`` /
    ``_sanitize_subprocess_env``), not through ``hermes_subprocess_env()``.

    Before this fix, that path's only protection for GITHUB_TOKEN/GH_TOKEN
    was membership in ``_HERMES_PROVIDER_ENV_BLOCKLIST`` — a frozenset
    computed once at import time, indirectly, via ``OPTIONAL_ENV_VARS``
    category lookups. GH_TOKEN has an explicit hardcoded blocklist entry;
    GITHUB_TOKEN did not — it relied entirely on that indirect path. If the
    blocklist ever failed to carry GITHUB_TOKEN (a stale computation, a
    degraded import, a config drift), a container-level ``GITHUB_TOKEN``
    would flow straight into a cron job's ``gh pr comment`` / ``gh pr
    merge`` subprocess. ``gh`` treats ``GH_TOKEN``/``GITHUB_TOKEN`` env vars
    as taking precedence over the on-disk ``hosts.yml`` identity, so the
    auditor's correctly-provisioned ``hermes-auditor`` credential would be
    silently overridden by the CEO's own token — exactly what happened on
    biglobster#408.

    These tests simulate that degraded blocklist directly (rather than
    relying on the real one still containing GITHUB_TOKEN today) to prove
    the fix — wiring ``_ALWAYS_STRIP_KEYS`` into the terminal spawn path
    unconditionally — closes the gap independent of blocklist contents.
    """

    def test_make_run_env_strips_github_token_even_if_blocklist_forgets_it(self):
        from tools.environments import local as local_mod

        degraded = frozenset(
            local_mod._HERMES_PROVIDER_ENV_BLOCKLIST - {"GITHUB_TOKEN", "GH_TOKEN"}
        )
        with patch.object(local_mod, "_HERMES_PROVIDER_ENV_BLOCKLIST", degraded), \
             patch.dict(os.environ, {
                 "PATH": "/usr/bin:/bin",
                 "GITHUB_TOKEN": "ceo-personal-token",
                 "GH_TOKEN": "ceo-personal-token",
             }, clear=True):
            run_env = local_mod._make_run_env({})

        assert "GITHUB_TOKEN" not in run_env
        assert "GH_TOKEN" not in run_env

    def test_sanitize_subprocess_env_strips_github_token_even_if_blocklist_forgets_it(self):
        from tools.environments import local as local_mod

        degraded = frozenset(
            local_mod._HERMES_PROVIDER_ENV_BLOCKLIST - {"GITHUB_TOKEN", "GH_TOKEN"}
        )
        base = {
            "HOME": "/home/user",
            "GITHUB_TOKEN": "ceo-personal-token",
            "GH_TOKEN": "ceo-personal-token",
        }
        with patch.object(local_mod, "_HERMES_PROVIDER_ENV_BLOCKLIST", degraded):
            result = local_mod._sanitize_subprocess_env(base)

        assert "GITHUB_TOKEN" not in result
        assert "GH_TOKEN" not in result
        assert result.get("HOME") == "/home/user"

    def test_end_to_end_cron_shaped_terminal_call_never_sees_ambient_github_token(self):
        """Full LocalEnvironment.execute() path, simulating a cron job's
        ambient container env carrying a GITHUB_TOKEN that belongs to a
        different identity than the profile's on-disk gh credential."""
        result_env = _run_with_env(extra_os_env={
            "GITHUB_TOKEN": "ceo-personal-token",
            "GH_TOKEN": "ceo-personal-token",
        })
        assert "GITHUB_TOKEN" not in result_env
        assert "GH_TOKEN" not in result_env
