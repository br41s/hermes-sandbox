"""Fork: under multiplex the launch profile's secret scope falls back to the process env.

The 2026-09-28 incident: with multiplex on, the Telegram allowlists and bot token
(Zeabur container env only, never /opt/data/.env) were invisible to the default
profile's scope, so every sender was blocked and default-profile cron lost Telegram.
hermes_cli/fork_ext/process_env_scope.py restores the pre-multiplex view for the
launch profile only. These pin that it does, that a secondary never gets it, and
that the image turns it on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hermes_constants
from agent import secret_scope as ss
from gateway.platforms._shared import platform_gate_env
from hermes_cli.fork_ext import process_env_scope as pes

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWED = "TELEGRAM_ALLOWED_USERS"


@pytest.fixture(autouse=True)
def _reset_multiplex(monkeypatch):
    monkeypatch.setattr(hermes_constants, "_PINNED_PROCESS_HERMES_HOME", None, raising=False)
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


@pytest.fixture
def homes(tmp_path, monkeypatch):
    launch = tmp_path / "data"
    secondary = launch / "profiles" / "grow-shop"
    secondary.mkdir(parents=True)
    (launch / ".env").write_text("OPENROUTER_API_KEY=from-dotenv\n", encoding="utf-8")
    (secondary / ".env").write_text("OPENROUTER_API_KEY=grow\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setenv(ALLOWED, "178351069")
    monkeypatch.setenv(pes.FLAG, "1")
    return launch, secondary


def _gate_read(home: Path, name: str = ALLOWED) -> str:
    """What the Telegram adapter's allowlist read sees under a multiplexed turn for ``home``."""
    ss.set_multiplex_active(True)
    token = ss.set_secret_scope(ss.build_profile_secret_scope(home), profile_home=str(home))
    try:
        return platform_gate_env(name)
    finally:
        ss.reset_secret_scope(token)


def test_the_launch_profile_sees_its_container_env_allowlist(homes):
    launch, _ = homes
    assert _gate_read(launch) == "178351069"


def test_without_the_flag_it_is_upstream_and_the_allowlist_is_empty(homes, monkeypatch):
    """The incident, reproduced: this is what blocked every sender in General."""
    launch, _ = homes
    monkeypatch.delenv(pes.FLAG)
    assert _gate_read(launch) == ""


def test_a_secondary_profile_never_gets_the_launch_env(homes):
    _, secondary = homes
    assert _gate_read(secondary) == ""
    scope = ss.build_profile_secret_scope(secondary)
    assert ALLOWED not in scope and scope["OPENROUTER_API_KEY"] == "grow"


def test_dotenv_still_wins_over_the_process_env(homes, monkeypatch):
    launch, _ = homes
    monkeypatch.setenv("OPENROUTER_API_KEY", "stale-process-value")
    assert ss.build_profile_secret_scope(launch)["OPENROUTER_API_KEY"] == "from-dotenv"


def test_globals_and_the_flag_stay_out_of_the_scope(homes):
    launch, _ = homes
    scope = ss.build_profile_secret_scope(launch)
    assert "PATH" not in scope and "HERMES_HOME" not in scope and pes.FLAG not in scope


def test_default_profile_cron_delivery_sees_telegram_enabled(homes, monkeypatch):
    """The cron half: `platform 'telegram' not configured/enabled` came from here."""
    from gateway.config import Platform, load_gateway_config

    launch, _ = homes
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    ss.set_multiplex_active(True)
    token = ss.set_secret_scope(ss.build_profile_secret_scope(launch), profile_home=str(launch))
    try:
        pconfig = load_gateway_config().platforms.get(Platform.TELEGRAM)
    finally:
        ss.reset_secret_scope(token)
    assert pconfig is not None and pconfig.enabled


def test_the_image_turns_it_on():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f"ENV {pes.FLAG}=1" in dockerfile


# ── the production probe ───────────────────────────────────────────────────────


def test_probe_reports_ready_and_never_prints_a_value(homes, monkeypatch, capsys):
    launch, _ = homes
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", "-1004224848555")
    assert pes.check() == 0
    out = capsys.readouterr().out
    assert "VERDICT: OK" in out
    assert "TELEGRAM_BOT_TOKEN: present" in out
    for value in ("123:abc", "178351069", "-1004224848555", "from-dotenv"):
        assert value not in out
    assert ss.is_multiplex_active() is False


def test_probe_says_not_ready_without_the_flag(homes, monkeypatch, capsys):
    monkeypatch.delenv(pes.FLAG)
    assert pes.check() == 1
    out = capsys.readouterr().out
    assert f"{ALLOWED}: missing" in out and "VERDICT: NOT READY" in out


def test_probe_lists_jobs_only_multiplex_would_tick(homes, capsys):
    _, secondary = homes
    (secondary / "cron").mkdir()
    (secondary / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
        {"id": "3f6f866ce1af", "name": "weekly", "enabled": True, "deliver": "telegram"}]}),
        encoding="utf-8")
    pes.check()
    assert "grow-shop 3f6f866ce1af enabled deliver=telegram weekly" in capsys.readouterr().out
