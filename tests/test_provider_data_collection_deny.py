"""Contract test: every OpenRouter request Hermes makes asks for providers that neither
store the prompt nor train on it (provider.data_collection = "deny").

This replaces the account-wide Zero Data Retention switch on the OpenRouter account, which
left models without any provider. Four paths build the request, so each is pinned here:
the config the gateway and delegation read, the cron agent, the auditor judge, and (in
scripts/whatsapp-lead-bot) the WhatsApp assistant. Auxiliary tasks (vision, summaries) and
the image/video plugins do not read provider_routing; that is a known gap, not tested here.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from auditor import llm
from cron import scheduler
from hermes_cli.fork_ext import boot_reconcile as br


def test_the_override_is_in_the_table() -> None:
    assert br.OVERRIDES[("provider_routing", "data_collection")] == "deny"


@pytest.mark.parametrize("label", ["main", "auditor", "biglobster", "bl-shoroban"])
def test_every_profile_gets_it_and_a_second_boot_changes_nothing(label: str) -> None:
    cfg: dict = {}
    assert br.reconcile_cfg(cfg, label, {}) is True
    assert cfg["provider_routing"]["data_collection"] == "deny"
    assert br.reconcile_cfg(cfg, label, {}) is False


def test_a_profile_cannot_opt_itself_out() -> None:
    cfg = {"provider_routing": {"data_collection": "allow", "sort": "price"}}
    br.reconcile_cfg(cfg, "grow-shop", {})
    assert cfg["provider_routing"] == {"data_collection": "deny", "sort": "price"}


def test_the_cron_agent_receives_the_policy_from_its_profile_config() -> None:
    seen: dict = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    setup = SimpleNamespace(
        runtime={}, model="some/model", max_iterations=5, reasoning_config=None,
        prefill_messages=None, fallback_model=None, credential_pool=None)
    cfg = {"provider_routing": {"data_collection": "deny", "ignore": ["open-inference"]}}
    scheduler._construct_cron_agent(
        FakeAgent, {}, cfg, setup, workdir=None, session_id="s", session_db=None)
    assert seen["provider_data_collection"] == "deny"
    assert seen["providers_ignored"] == ["open-inference"]


def test_a_cron_agent_without_the_setting_sends_none_not_a_default() -> None:
    seen: dict = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    setup = SimpleNamespace(
        runtime={}, model="m", max_iterations=5, reasoning_config=None,
        prefill_messages=None, fallback_model=None, credential_pool=None)
    scheduler._construct_cron_agent(FakeAgent, {}, {}, setup, workdir=None, session_id="s", session_db=None)
    # The reconcile is what writes the setting; the code does not invent one.
    assert seen["provider_data_collection"] is None


@pytest.mark.parametrize("model", ["deepseek/deepseek-v4.1-flash", "xiaomi/mimo-v2.6-flash"])
def test_the_judge_asks_for_it_whatever_the_model(model: str) -> None:
    import json
    req = llm._build_request(model, [{"role": "user", "content": "hi"}], "sk-test")
    assert json.loads(req.data)["provider"]["data_collection"] == "deny"
