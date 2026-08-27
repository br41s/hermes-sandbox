"""A rented agent must actually have the tools its prompt tells it to call.

The Product Sheet Writer spent five days doing no work anyone could see. Its
prompt opened with "llama a `bl_site_product`", the tool was registered, it
worked when called by hand — and it was never in the agent's schema, because
the cron default toolset is an allowlist (``_HERMES_CORE_TOOLS``) that carries
no fork tool. The agent improvised: it imported the module under execute_code,
published a batch with no per-product judgement, and reported success. Three
rounds of prompt and tool-shape fixes went past the real cause.

Nothing in the system objected, because nothing compares the two halves. This
does: for every provisioned agent, take the fork tools its prompt names and
check they are reachable under the toolsets that agent is provisioned with.
"""

import re
from pathlib import Path

import pytest

# Imported for their side effect: each registers its toolset, and
# resolve_toolset() only sees a fork toolset once its module has been imported.
import tools.bl_site_product_tool  # noqa: F401
import tools.bl_site_publish_tool  # noqa: F401
import tools.product_enrich_tool  # noqa: F401
from scripts.provision_bl_client import AGENT_SOURCES, REPO_ROOT
from toolsets import resolve_toolset

# The fork's own tools. Core tools (web_search, terminal, read_file) are always
# present and are not what goes missing; these are the ones a prompt can name
# with nothing behind them.
FORK_TOOLS = {"bl_site_product", "product_enrich", "bl_site_publish"}

# What is left of the gap: the agents nobody has run yet.
#
# All eight content agents instructed `bl_site_publish` without having it. The
# three that are live for a client — gap-hunter, infographic, maintenance — are
# fixed, because their run logs say exactly which tools they use and a list can
# be built that adds the missing one without taking anything away.
#
# These five have never run. Their prompts name the tools they intend, but a
# prompt is what the agent was told to do, not what it turned out to need — the
# three that did run all reached for terminal and file, which none of their
# prompts mention. Guessing a list here would swap a known gap for an unknown
# one, so they keep the cron default until a first run says what to declare.
#
# Shrinking this set is the point — do not add to it without a note here.
AGENTS_WITHOUT_THEIR_TOOLS = {
    "seo",
    "onboarding-content",
    "product-articles",
    "site-setup",
    "shorts",
}


def tools_named_in(prompt: str) -> set:
    """Fork tools the prompt actually instructs, by name."""
    return {tool for tool in FORK_TOOLS if re.search(rf"\b{tool}\b", prompt)}


def reachable_from(toolsets) -> set:
    """Every tool the agent may call, or None for 'the cron default'."""
    if not toolsets:
        return set()  # the default carries no fork tool — that is the bug
    return {tool for name in toolsets for tool in resolve_toolset(name)}


@pytest.mark.parametrize("agent_key", sorted(AGENT_SOURCES))
def test_prompt_only_instructs_tools_the_agent_has(agent_key):
    source, _display, _kind, toolsets = AGENT_SOURCES[agent_key]
    prompt = Path(REPO_ROOT, source).read_text(encoding="utf-8")

    missing = tools_named_in(prompt) - reachable_from(toolsets)

    if agent_key in AGENTS_WITHOUT_THEIR_TOOLS:
        pytest.xfail(f"{agent_key} reaches the site over the shell; missing {sorted(missing)}")

    assert not missing, (
        f"{agent_key}'s prompt tells it to call {sorted(missing)}, which its "
        f"toolsets {toolsets} do not carry. The agent will improvise — add the "
        f"toolset in AGENT_SOURCES rather than rewording the prompt."
    )


def test_the_known_gaps_are_still_real():
    """Guard the xfail list: an agent that got fixed must leave it."""
    for agent_key in AGENTS_WITHOUT_THEIR_TOOLS:
        source, _display, _kind, toolsets = AGENT_SOURCES[agent_key]
        prompt = Path(REPO_ROOT, source).read_text(encoding="utf-8")
        assert tools_named_in(prompt) - reachable_from(toolsets), (
            f"{agent_key} now has every tool its prompt names — remove it from "
            "AGENTS_WITHOUT_THEIR_TOOLS so the real check applies to it."
        )


def test_product_sheets_carries_exactly_what_its_prompt_asks_for():
    # The regression that started this. Explicit rather than parametrised so a
    # rename cannot quietly drop the case.
    _source, _display, _kind, toolsets = AGENT_SOURCES["product-sheets"]
    reachable = reachable_from(toolsets)

    assert {"bl_site_product", "product_enrich", "web_search"} <= reachable
    # And deliberately not these: with no shell there is no way to turn the
    # per-product workflow back into a script.
    assert not {"terminal", "execute_code"} & reachable


# Tools each live agent's runs have actually used, read off the production logs
# for profile bl-shoroban. Declaring toolsets removes as well as adds, so this
# is the floor: a narrower list would take away something an agent is using and
# the failure would show up as improvisation, not as an error.
OBSERVED_IN_PRODUCTION = {
    "gap-hunter": {
        "image_generate", "patch", "read_file", "search_files", "skill_manage",
        "skill_view", "terminal", "web_extract", "web_search", "write_file",
    },
    "infographic": {
        "patch", "read_file", "skill_manage", "skill_view", "terminal", "write_file",
    },
    "maintenance": {"terminal"},
}


@pytest.mark.parametrize("agent_key", sorted(OBSERVED_IN_PRODUCTION))
def test_narrowing_never_removes_a_tool_the_agent_uses(agent_key):
    _source, _display, _kind, toolsets = AGENT_SOURCES[agent_key]
    assert toolsets, f"{agent_key} should declare toolsets"

    removed = OBSERVED_IN_PRODUCTION[agent_key] - reachable_from(toolsets)

    assert not removed, (
        f"declaring toolsets for {agent_key} takes away {sorted(removed)}, which "
        f"its production runs use. Add the toolset that carries them."
    )


def test_the_live_agents_finally_have_their_publish_tool():
    # The whole point of the exercise: their prompts have instructed
    # bl_site_publish since the day they were written.
    for agent_key in ("gap-hunter", "infographic", "maintenance"):
        _source, _display, _kind, toolsets = AGENT_SOURCES[agent_key]
        assert "bl_site_publish" in reachable_from(toolsets), agent_key


def test_todo_survives_narrowing():
    # todo lives in the core set, so declaring toolsets would drop it. All
    # three prompts use it, and losing it would be invisible.
    for agent_key in ("gap-hunter", "infographic", "maintenance"):
        _source, _display, _kind, toolsets = AGENT_SOURCES[agent_key]
        assert "todo" in reachable_from(toolsets), agent_key
