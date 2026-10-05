"""Fork: the boot hook's env sync and config reconcile, on a fixture volume.

hermes_cli/fork_ext/boot_reconcile.py was a heredoc in docker/cont-init.d/03-biglobster-config
until 2026-09-28; the per-key contract tests (token propagation, BYOK images, delegation
pins, request timeout, auditor pinning) live beside the incidents they came from. These
cover the rest: the auditor's identity, topic routing, lane-scoped MCP servers, and the
hook's own invocation.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from hermes_cli.fork_ext import boot_reconcile as br

REPO_ROOT = Path(__file__).resolve().parents[2]


def _profile(home: Path, name: str, env: str = "", cfg: dict | None = None) -> Path:
    prof = home / "profiles" / name
    prof.mkdir(parents=True)
    (prof / "SOUL.md").write_text("soul", encoding="utf-8")
    (prof / ".env").write_text(env, encoding="utf-8")
    if cfg is not None:
        (prof / "config.yaml").write_text(yaml.dump(cfg), encoding="utf-8")
    return prof


def _env(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _cfg(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture
def home(tmp_path) -> Path:
    home = tmp_path / "data"
    home.mkdir()
    return home


# ── the auditor's identity ─────────────────────────────────────────────────────


def test_auditor_gets_its_own_github_identity_never_the_shared_one(home):
    auditor = _profile(home, "auditor", "GITHUB_TOKEN=shared\nOTHER=1\n")
    br.sync_envs(home, {"GITHUB_TOKEN": "shared", "GH_TOKEN": "shared",
                        "HERMES_AUDITOR_GITHUB_TOKEN": "bot"})
    env = _env(auditor / ".env")
    assert "shared" not in env
    assert "GITHUB_TOKEN=bot\n" in env and "GH_TOKEN=bot\n" in env
    assert (home / ".env").read_text(encoding="utf-8").count("GITHUB_TOKEN=shared") == 1


def test_auditor_fails_closed_without_its_token(home, capsys):
    auditor = _profile(home, "auditor", "GITHUB_TOKEN=stale\nGH_TOKEN=stale\nOTHER=1\n")
    br.sync_envs(home, {"GITHUB_TOKEN": "shared", "GH_TOKEN": "shared"})
    assert _env(auditor / ".env") == "OTHER=1\n"
    assert "NO GitHub credential" in capsys.readouterr().out


def test_auditor_token_falls_back_to_the_main_env_file(home):
    (home / ".env").write_text("HERMES_AUDITOR_GITHUB_TOKEN=from-file\n", encoding="utf-8")
    auditor = _profile(home, "auditor")
    br.sync_envs(home, {})
    assert "GITHUB_TOKEN=from-file\n" in _env(auditor / ".env")


def test_auditor_dedicated_openrouter_key_wins_over_the_shared_one(home):
    auditor = _profile(home, "auditor")
    other = _profile(home, "biglobster")
    br.sync_envs(home, {"OPENROUTER_API_KEY": "shared",
                        "HERMES_AUDITOR_OPENROUTER_API_KEY": "auditor-only"})
    assert "OPENROUTER_API_KEY=auditor-only\n" in _env(auditor / ".env")
    assert "shared" not in _env(auditor / ".env")
    assert "OPENROUTER_API_KEY=shared\n" in _env(other / ".env")


def test_auditor_review_model_knobs_are_stamped_only_when_set(home):
    auditor = _profile(home, "auditor", "HERMES_AUDITOR_SYSTEM_MODEL=old\n")
    br.sync_envs(home, {"HERMES_AUDITOR_SYSTEM_MODEL": "new", "HERMES_AUDITOR_CONTENT_MODEL": "  "})
    env = _env(auditor / ".env")
    assert env.count("HERMES_AUDITOR_SYSTEM_MODEL=") == 1 and "HERMES_AUDITOR_SYSTEM_MODEL=new" in env
    assert "HERMES_AUDITOR_CONTENT_MODEL" not in env


def test_a_dir_without_soul_is_not_a_profile(home):
    ghost = home / "profiles" / "ghost"
    ghost.mkdir(parents=True)
    (ghost / ".env").write_text("A=1\n", encoding="utf-8")
    br.sync_envs(home, {"OPENROUTER_API_KEY": "k"})
    assert _env(ghost / ".env") == "A=1\n"


# ── config.yaml ────────────────────────────────────────────────────────────────


def test_group_topics_are_rebuilt_from_routing_env(home, tmp_path):
    src = tmp_path / "src"
    for name, thread in (("biglobster", "7"), ("grow-shop", "3"), ("broken", "x")):
        (src / name).mkdir(parents=True)
        (src / name / "routing.env").write_text(
            f"# c\nTELEGRAM_HOME_CHANNEL=-100\nTELEGRAM_HOME_CHANNEL_THREAD_ID={thread}\n",
            encoding="utf-8")
    cfg = {"telegram": {"extra": {"group_topics": [
        {"chat_id": -100, "topics": [{"thread_id": 3, "profile": "wrong", "name": "kept"},
                                     {"thread_id": 99, "profile": "mine", "name": "user"}]}]}}}

    assert br.reconcile_group_topics(cfg, src) is True
    topics = cfg["telegram"]["extra"]["group_topics"][0]["topics"]
    assert topics == [
        {"thread_id": 3, "profile": "grow-shop", "name": "kept"},
        {"thread_id": 99, "profile": "mine", "name": "user"},
        {"name": "biglobster", "profile": "biglobster", "thread_id": 7},
    ]
    assert br.reconcile_group_topics(cfg, src) is False


def test_gsc_server_is_biglobster_only_and_shed_from_main():
    main = {"mcp_servers": {"gsc": {"old": True}, "other": {}}}
    br.reconcile_cfg(main, "main", {}, profiles_src=Path("/nonexistent"))
    assert main["mcp_servers"] == {"other": {}}

    biglobster: dict = {}
    br.reconcile_cfg(biglobster, "biglobster", {})
    assert biglobster["mcp_servers"]["gsc"] == br.GSC_SERVER
    assert biglobster["mcp_servers"]["gsc"] is not br.GSC_SERVER

    tenant: dict = {}
    br.reconcile_cfg(tenant, "grow-shop", {})
    assert "gsc" not in tenant.get("mcp_servers", {})


def test_langfuse_is_enabled_only_with_both_keys():
    cfg = {"plugins": {"enabled": ["x"]}}
    br.reconcile_cfg(cfg, "main", {"HERMES_LANGFUSE_PUBLIC_KEY": "p"})
    assert cfg["plugins"]["enabled"] == ["x"]
    both = {"HERMES_LANGFUSE_PUBLIC_KEY": "p", "HERMES_LANGFUSE_SECRET_KEY": "s"}
    br.reconcile_cfg(cfg, "main", both)
    assert cfg["plugins"]["enabled"] == ["x", "observability/langfuse"]
    assert br.reconcile_cfg(cfg, "main", both) is False


def test_models_default_fallback_and_the_auditor_exemption():
    environ = {"HERMES_DEFAULT_MODEL": "def/m", "HERMES_FALLBACK_MODEL": "fb/m"}
    cfg = {"model": "old/m", "fallback_model": {"model": "def/m"}}
    br.reconcile_cfg(cfg, "grow-shop", environ)
    assert cfg["model"] == {"default": "def/m", "provider": "openrouter", "base_url": ""}
    assert cfg["fallback_model"] == {"model": "fb/m", "provider": "openrouter"}

    auditor = {"fallback_model": {"model": "untouched"}}
    br.reconcile_cfg(auditor, "auditor", {**environ, "HERMES_AUDITOR_ORCHESTRATOR_MODEL": "orch/m"})
    assert auditor["model"]["default"] == "orch/m"
    assert auditor["fallback_model"] == {"model": "untouched"}


def test_a_current_config_is_not_rewritten(home):
    _profile(home, "biglobster", cfg={})
    path = home / "profiles" / "biglobster" / "config.yaml"
    br.reconcile_configs(home, {}, profiles_src=home / "none")
    first = path.read_text(encoding="utf-8")
    os.utime(path, (0, 0))
    br.reconcile_configs(home, {}, profiles_src=home / "none")
    assert path.stat().st_mtime == 0 and path.read_text(encoding="utf-8") == first


def test_a_broken_config_warns_and_the_rest_still_reconcile(home, capsys):
    (home / "config.yaml").write_text("- not\n- a mapping\n", encoding="utf-8")
    _profile(home, "biglobster", cfg={})
    br.reconcile_configs(home, {}, profiles_src=home / "none")
    assert "Warning: main config.yaml reconcile failed" in capsys.readouterr().out
    assert _cfg(home / "profiles" / "biglobster" / "config.yaml")["agent"]["max_turns"] == 90


# ── the hook's invocation ──────────────────────────────────────────────────────


def test_runs_as_the_boot_hook_invokes_it(home):
    """`PYTHONPATH=$INSTALL_DIR HERMES_HOME=... $PY -m hermes_cli.fork_ext.boot_reconcile`."""
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    prof = _profile(home, "biglobster", cfg={})
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO_ROOT),
           "HERMES_HOME": str(home), "OPENROUTER_API_KEY": "k"}
    result = subprocess.run([sys.executable, "-m", "hermes_cli.fork_ext.boot_reconcile"],
                            env=env, cwd="/", capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "biglobster: reconciled config.yaml keys" in result.stdout
    assert "OPENROUTER_API_KEY=k\n" in _env(prof / ".env")
    cfg = _cfg(home / "config.yaml")
    assert cfg["agent"]["max_turns"] == 90
    # Stage 3 step 4: multiplex is upstream's default; boot no longer pins it either way.
    assert "multiplex_profiles" not in (cfg.get("gateway") or {})


def test_boot_keeps_an_existing_multiplex_flag(home):
    """Production's config.yaml already carries `multiplex_profiles: true` from the old pin.
    Boot only ever sets keys, so dropping the pin must leave it, not strip it."""
    (home / "config.yaml").write_text("gateway:\n  multiplex_profiles: true\n", encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO_ROOT),
           "HERMES_HOME": str(home), "OPENROUTER_API_KEY": "k"}
    result = subprocess.run([sys.executable, "-m", "hermes_cli.fork_ext.boot_reconcile"],
                            env=env, cwd="/", capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert _cfg(home / "config.yaml")["gateway"]["multiplex_profiles"] is True


def test_the_hook_calls_the_module():
    hook = (REPO_ROOT / "docker" / "cont-init.d" / "03-biglobster-config").read_text(encoding="utf-8")
    [call] = [line for line in hook.splitlines() if "-m hermes_cli.fork_ext.boot_reconcile" in line]
    # Without either, the import or HERMES_HOME silently fails and §1+§2 degrade to the
    # hook's `|| echo Warning` fallback on every boot.
    assert 'PYTHONPATH="$INSTALL_DIR"' in call
    assert 'HERMES_HOME="$HERMES_HOME"' in call
    assert 'as_hermes "$PY" -m hermes_cli.fork_ext.boot_reconcile' in call
    assert "overrides = {" not in hook and "inject = [" not in hook
