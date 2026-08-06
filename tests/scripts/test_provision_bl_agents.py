"""The rentable-agent catalog has to stay wired to real files.

A rental is sold before it is provisioned, so a typo'd prompt path or a missing
prompt file would surface as a paid client with a cron job that cannot run —
which is exactly the failure the schedule/one-shot split makes unrecoverable
for one-shot agents (they auto-remove after their single run).
"""

from pathlib import Path

import pytest

from scripts.provision_bl_client import (
    AGENT_SOURCES,
    AGENTS_REQUIRING_OLD_SITE,
    MUTUALLY_EXCLUSIVE_AGENTS,
    pick_stagger_schedule,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("agent_key", sorted(AGENT_SOURCES))
def test_every_agent_prompt_file_exists(agent_key):
    source, display_name, schedule_kind = AGENT_SOURCES[agent_key]
    path = REPO_ROOT / source
    assert path.is_file(), f"{agent_key} points at a missing prompt: {source}"
    assert path.read_text(encoding="utf-8").strip()
    assert display_name
    assert schedule_kind in {"daily", "once"}


def test_maintenance_is_a_recurring_daily_agent():
    source, display_name, schedule_kind = AGENT_SOURCES["maintenance"]
    assert source == "maintenance/bl-site-package-maintenance.prompt"
    assert display_name == "Website Maintenance"
    # Availability and publish-drift checks are only meaningful checked often;
    # a one-shot or weekly cadence would let a site sit broken for days.
    assert schedule_kind == "daily"


def test_maintenance_needs_no_old_site_url():
    # It only ever reads the client's own site, so ordering it must never be
    # blocked on the client having had a previous website.
    assert "maintenance" not in AGENTS_REQUIRING_OLD_SITE
    assert "maintenance" not in MUTUALLY_EXCLUSIVE_AGENTS


def test_maintenance_is_staggered_off_peak_like_the_other_daily_agents():
    schedule = pick_stagger_schedule("bl-cliente-garcia", "maintenance")
    minute, hour, *rest = schedule.split()
    assert rest == ["*", "*", "*"]
    assert 2 <= int(hour) <= 5
    assert 0 <= int(minute) <= 59
    # Different agents for the same client must not all fire at once.
    assert schedule != pick_stagger_schedule("bl-cliente-garcia", "gap-hunter")


def test_maintenance_prompt_states_its_boundaries():
    text = (REPO_ROOT / AGENT_SOURCES["maintenance"][0]).read_text(encoding="utf-8")
    # The price tier is only defensible if the scope stays closed: these are
    # the claims the prompt must keep making, in the same spirit as the
    # Site Launch prompt's "fuera del alcance del producto" rule.
    assert "bl_site_health" in text
    assert "fuera del alcance del producto" in text
    assert "record_report" in text
    # It must never claim to patch a shared codebase per client.
    assert "No puedes parchear dependencias" in text
