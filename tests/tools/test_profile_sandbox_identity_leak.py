"""Regression tests: a terminal sandbox must not carry one profile's identity
into another profile's run.

Incident (2026-09-12, production). Two cron jobs that did NOT overlap:

    cron_c19bb95c0a62_20260912_140355 (profile: auditor)  07:04:20 -> 07:05:04
    cron_3988cc0c189f_20260912_140629 (profile: finview)  07:06:31 -> 07:11:48

The auditor job created the shared ``default`` local environment at 07:04:24.
Nothing reaped it when the job ended -- per-turn ``cleanup_vm`` is called with
the agent's ``session_id``, which never matches the ``"default"`` key, so only
the 5-minute idle reaper collects it. The FinView job reused that environment
and pushed FinView PR #245 as ``hermes-auditor``; the auditor skips PRs it
appears to have authored, so the PR silently left the review queue.

Two independent defects made that possible, and each is pinned below:

1. ``_resolve_container_task_id`` collapsed every task_id to one ``"default"``
   key regardless of profile, so the cached environment object was shared
   across a credential boundary.
2. Every command sources the session's ``export -p`` snapshot, which re-exported
   the ``HOME`` captured when the environment was created -- overriding the
   ``HOME`` Hermes recomputes per spawn. In the terminal lane ``HOME`` IS the
   git/gh identity: ``GITHUB_TOKEN``/``GH_TOKEN`` are Tier-1 stripped from every
   spawned subprocess, so ``~/.gitconfig`` / ``~/.git-credentials`` /
   ``~/.config/gh/hosts.yml`` are the only credentials a command can reach.

``os.environ`` was fully restored and the on-disk credentials were correct
throughout, so neither a token check nor an env-restore audit would have caught
this. The tests therefore assert on the *effective git identity a command sees*,
not on process state.
"""

import os
import shutil
import subprocess

import pytest

from tools import terminal_tool
from tools.environments import base as env_base
from tools.environments.local import LocalEnvironment


BASH = shutil.which("bash")
GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(
    BASH is None, reason="bash is required to exercise the snapshot wrapper"
)


def _make_profile_home(tmp_path, name: str, email: str):
    """A profile subprocess home shaped like /opt/data/profiles/<p>/home."""
    home = tmp_path / "profiles" / name / "home"
    home.mkdir(parents=True)
    (home / ".gitconfig").write_text(
        f"[user]\n\tname = {name}\n\temail = {email}\n"
    )
    return home


def _run_wrapped(env_obj, command: str, spawn_home: str) -> str:
    """Run *command* through the real snapshot wrapper with HOME=*spawn_home*,
    the way ``_make_run_env`` would hand it to bash."""
    script = env_obj._wrap_command(command, str(env_obj.cwd))
    proc = subprocess.run(
        [BASH, "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": spawn_home, "HERMES_HOME": os.path.dirname(spawn_home)},
        cwd=str(env_obj.cwd),
    )
    return proc.stdout


# ---------------------------------------------------------------------------
# Defect 2 -- the session snapshot must not override Hermes-owned identity.
# ---------------------------------------------------------------------------


def test_snapshot_cannot_override_spawn_home(tmp_path):
    """A snapshot captured under profile A must not set HOME for a spawn that
    Hermes resolved to profile B.

    This is the exact production shape: /tmp/hermes-snap-<id>.sh held
    ``declare -x HOME="/opt/data/profiles/auditor/home"`` and every later
    command sourced it.
    """
    auditor_home = _make_profile_home(tmp_path, "auditor", "auditor@bot.local")
    finview_home = _make_profile_home(tmp_path, "finview", "hermes@agent.local")

    workdir = tmp_path / "work"
    workdir.mkdir()

    env_obj = LocalEnvironment(cwd=str(workdir), timeout=30)
    # Poison the snapshot exactly as a prior auditor run would have left it.
    with open(env_obj._snapshot_path, "w", encoding="utf-8") as fh:
        fh.write(f'declare -x HOME="{auditor_home}"\n')
        fh.write(f'declare -x HERMES_HOME="{auditor_home.parent}"\n')
    env_obj._snapshot_ready = True

    out = _run_wrapped(env_obj, 'printf "HOME=%s\\n" "$HOME"', str(finview_home))

    assert f"HOME={finview_home}" in out, (
        "the snapshot overrode the per-spawn HOME -- this is the leak that "
        "signed FinView PR #245 as hermes-auditor"
    )
    assert str(auditor_home) not in out


@pytest.mark.skipif(GIT is None, reason="git is required")
def test_git_identity_follows_spawn_home_not_snapshot(tmp_path):
    """The user-visible consequence: ``git`` must resolve the identity of the
    profile Hermes resolved for THIS spawn."""
    auditor_home = _make_profile_home(tmp_path, "auditor", "auditor@bot.local")
    finview_home = _make_profile_home(tmp_path, "finview", "hermes@agent.local")

    workdir = tmp_path / "repo"
    workdir.mkdir()

    env_obj = LocalEnvironment(cwd=str(workdir), timeout=30)
    with open(env_obj._snapshot_path, "w", encoding="utf-8") as fh:
        fh.write(f'declare -x HOME="{auditor_home}"\n')
    env_obj._snapshot_ready = True

    out = _run_wrapped(env_obj, "git config --global user.email", str(finview_home))

    assert "hermes@agent.local" in out
    assert "auditor@bot.local" not in out, (
        "a content job would commit under the auditor identity, and the "
        "auditor skips PRs it authored -- the PR ships unreviewed"
    )


def test_snapshot_still_carries_ordinary_user_exports(tmp_path):
    """The pin is narrow: only Hermes-owned vars are re-asserted. Everything a
    user's command exported still survives across calls."""
    home = _make_profile_home(tmp_path, "solo", "solo@x.local")
    workdir = tmp_path / "work"
    workdir.mkdir()

    env_obj = LocalEnvironment(cwd=str(workdir), timeout=30)
    with open(env_obj._snapshot_path, "w", encoding="utf-8") as fh:
        fh.write('declare -x MY_USER_VAR="kept"\n')
    env_obj._snapshot_ready = True

    out = _run_wrapped(env_obj, 'printf "V=%s\\n" "$MY_USER_VAR"', str(home))
    assert "V=kept" in out


def test_unset_pinned_var_is_not_exported_as_empty(tmp_path):
    """An unset HERMES_HOME at spawn time stays unset rather than becoming ""
    -- an empty HERMES_HOME would silently re-home every downstream tool."""
    home = _make_profile_home(tmp_path, "solo", "solo@x.local")
    workdir = tmp_path / "work"
    workdir.mkdir()

    env_obj = LocalEnvironment(cwd=str(workdir), timeout=30)
    with open(env_obj._snapshot_path, "w", encoding="utf-8") as fh:
        fh.write('declare -x HERMES_HOME="/from/snapshot"\n')
    env_obj._snapshot_ready = True

    script = env_obj._wrap_command('printf "HH=%s\\n" "${HERMES_HOME-UNSET}"', str(workdir))
    spawn_env = {k: v for k, v in os.environ.items() if k != "HERMES_HOME"}
    spawn_env["HOME"] = str(home)
    proc = subprocess.run(
        [BASH, "-c", script], capture_output=True, text=True,
        env=spawn_env, cwd=str(workdir),
    )
    # Nothing to pin, so the snapshot value is inherited as before -- the guard
    # must not export an empty string over it.
    assert "HH=/from/snapshot" in proc.stdout


# ---------------------------------------------------------------------------
# Defect 1 -- the shared sandbox key must not span profiles.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_overrides():
    before = dict(terminal_tool._task_env_overrides)
    terminal_tool._task_env_overrides.clear()
    yield
    terminal_tool._task_env_overrides.clear()
    terminal_tool._task_env_overrides.update(before)


def _set_home(monkeypatch, root, hermes_home):
    import hermes_constants

    monkeypatch.setattr(
        hermes_constants, "_get_platform_default_hermes_home", lambda: root
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))


def test_default_home_keeps_the_historical_key(tmp_path, monkeypatch):
    """Single-profile installs are unchanged -- still the literal "default"."""
    root = tmp_path / "hermes"
    root.mkdir()
    _set_home(monkeypatch, root, root)
    assert terminal_tool._resolve_container_task_id(None) == "default"
    assert terminal_tool._resolve_container_task_id("subagent-0-deadbeef") == "default"


def test_two_profiles_never_share_a_sandbox_key(tmp_path, monkeypatch):
    """The auditor and a content profile must land on different cache keys, so
    the second job cannot inherit the first's environment object."""
    root = tmp_path / "hermes"
    (root / "profiles" / "auditor").mkdir(parents=True)
    (root / "profiles" / "finview").mkdir(parents=True)

    _set_home(monkeypatch, root, root / "profiles" / "auditor")
    auditor_key = terminal_tool._resolve_container_task_id(None)

    _set_home(monkeypatch, root, root / "profiles" / "finview")
    finview_key = terminal_tool._resolve_container_task_id(None)

    assert auditor_key != finview_key
    assert auditor_key != "default" and finview_key != "default"


def test_subagents_still_share_within_one_profile(tmp_path, monkeypatch):
    """The collapse that this scoping narrows must still do its original job:
    one bash / one workspace for an agent and all of its subagents."""
    root = tmp_path / "hermes"
    (root / "profiles" / "finview").mkdir(parents=True)
    _set_home(monkeypatch, root, root / "profiles" / "finview")

    parent = terminal_tool._resolve_container_task_id(None)
    child = terminal_tool._resolve_container_task_id("subagent-3-cafef00d")
    assert parent == child


def test_isolation_override_still_wins(tmp_path, monkeypatch):
    """RL / benchmark rollouts keep their own sandbox regardless of profile."""
    root = tmp_path / "hermes"
    (root / "profiles" / "finview").mkdir(parents=True)
    _set_home(monkeypatch, root, root / "profiles" / "finview")

    terminal_tool.register_task_env_overrides("tb2-task", {"docker_image": "tb2:x"})
    try:
        assert terminal_tool._resolve_container_task_id("tb2-task") == "tb2-task"
    finally:
        terminal_tool.clear_task_env_overrides("tb2-task")


def test_key_is_safe_as_a_container_name_component(tmp_path, monkeypatch):
    """The key is handed to backends that use it in container names/labels."""
    import re

    root = tmp_path / "hermes"
    (root / "profiles" / "bl-shoroban").mkdir(parents=True)
    _set_home(monkeypatch, root, root / "profiles" / "bl-shoroban")

    key = terminal_tool._resolve_container_task_id(None)
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", key), key
