"""Fork: the gateway's startup model/provider log line runs under the launch profile's scope.

Under multiplex an unscoped credential read fails closed (UnscopedSecretError), so the unscoped
preflight logged "Provider resolution FAILED ... OPENAI_BASE_URL" on every boot of sha-7b037f7b7
while real launch-profile turns, which bind the scope, resolved fine.
"""

from __future__ import annotations

import logging

import pytest

import hermes_constants
from agent import secret_scope as ss
from gateway import run as gateway_run


@pytest.fixture(autouse=True)
def _reset_multiplex(monkeypatch):
    monkeypatch.setattr(hermes_constants, "_PINNED_PROCESS_HERMES_HOME", None, raising=False)
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


def test_multiplexed_preflight_resolves_the_provider(tmp_path, monkeypatch, caplog):
    home = tmp_path / "data"
    home.mkdir()
    (home / ".env").write_text("OPENROUTER_API_KEY=sk-or-test\n", encoding="utf-8")
    (home / "config.yaml").write_text(
        "model:\n  default: deepseek/deepseek-v4.1-flash\n  provider: openrouter\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
    ss.set_multiplex_active(True)

    with caplog.at_level(logging.INFO, logger=gateway_run.logger.name):
        gateway_run._log_startup_model_preflight()

    text = caplog.text
    assert "Provider resolution FAILED" not in text, text
    assert "[startup] Model:" in text and "API key present: True" in text
    assert "sk-or-test" not in text
    assert ss.current_secret_scope() is None  # the scope is released after the check
