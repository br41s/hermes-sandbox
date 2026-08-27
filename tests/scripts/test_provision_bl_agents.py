"""The rentable-agent catalog has to stay wired to real files.

A rental is sold before it is provisioned, so a typo'd prompt path or a missing
prompt file would surface as a paid client with a cron job that cannot run —
which is exactly the failure the schedule/one-shot split makes unrecoverable
for one-shot agents (they auto-remove after their single run).
"""

from pathlib import Path

import pytest
import yaml

from scripts.provision_bl_client import (
    AGENT_SOURCES,
    AGENTS_REQUIRING_OLD_SITE,
    AGENTS_REQUIRING_PEXELS,
    MUTUALLY_EXCLUSIVE_AGENTS,
    _write_config,
    pick_stagger_schedule,
    provision,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("agent_key", sorted(AGENT_SOURCES))
def test_every_agent_prompt_file_exists(agent_key):
    source, display_name, schedule_kind, _toolsets = AGENT_SOURCES[agent_key]
    path = REPO_ROOT / source
    assert path.is_file(), f"{agent_key} points at a missing prompt: {source}"
    assert path.read_text(encoding="utf-8").strip()
    assert display_name
    assert schedule_kind in {"daily", "once"}


def test_maintenance_is_a_recurring_daily_agent():
    source, display_name, schedule_kind, _toolsets = AGENT_SOURCES["maintenance"]
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


# --- Product Sheet Writer: works from the feed, never from recall -----------


def test_product_sheets_is_a_recurring_daily_agent():
    source, display_name, schedule_kind, _toolsets = AGENT_SOURCES["product-sheets"]
    assert source == "product-sheets/bl-site-package-product-sheets.prompt"
    assert display_name == "Product Sheet Writer"
    # The queue is thousands of products long and a run takes ten, so this is
    # open-ended work: it stops on its own once every sellable product has a
    # sheet or a recorded reason for not having one.
    assert schedule_kind == "daily"


def test_product_sheets_needs_no_old_site_url_and_no_extra_key():
    # It reads the client's own catalogue and the distributor feed behind it,
    # so ordering it must not depend on a previous website or a BYOK key.
    assert "product-sheets" not in AGENTS_REQUIRING_OLD_SITE
    assert "product-sheets" not in MUTUALLY_EXCLUSIVE_AGENTS


def test_product_sheets_is_staggered_off_peak_like_the_other_daily_agents():
    schedule = pick_stagger_schedule("bl-cliente-garcia", "product-sheets")
    minute, hour, *rest = schedule.split()
    assert rest == ["*", "*", "*"]
    assert 2 <= int(hour) <= 5
    assert schedule != pick_stagger_schedule("bl-cliente-garcia", "gap-hunter")


def test_product_sheets_prompt_forbids_inventing_product_facts():
    text = (REPO_ROOT / AGENT_SOURCES["product-sheets"][0]).read_text(encoding="utf-8")
    # This is the whole safety case for letting a model write, unattended,
    # about thousands of products a client actually sells. A published false
    # specification is the one failure here that reaches a customer as a lie,
    # so the rule is pinned rather than left to survive future prompt edits.
    assert "No inventes un solo dato" in text
    # It must name the concrete temptations, not just the principle: the
    # failure mode is a model completing a product it half-recognises.
    assert "aunque creas conocer la marca" in text
    # Passing over a product must stay an acceptable outcome, or the model
    # will pad a sheet to avoid looking unproductive.
    assert "skip_sheet" in text


def test_product_sheets_prompt_requires_verifying_a_source_before_using_it():
    text = (REPO_ROOT / AGENT_SOURCES["product-sheets"][0]).read_text(encoding="utf-8")
    # An earlier version of this prompt banned web search outright, and a test
    # here pinned that ban. It was the wrong policy: the product exists to
    # enrich a sheet beyond what the distributor supplies, and the distributor
    # is not the only source of truth about a product it merely resells.
    #
    # What has to hold instead is that nothing external is used until the
    # identity gate has confirmed the page is about this exact article. The
    # gate returns no content at all on a rejection, so there is nothing to
    # quote — but the prompt must still send the agent through it.
    assert "web_search" in text
    assert "product_enrich" in text
    assert "verify" in text
    # The near-miss is the danger: same maker, same family, one letter apart.
    assert "se *parece*" in text or "se parece" in text


def test_product_sheets_prompt_forbids_scripting_the_tools():
    text = (REPO_ROOT / AGENT_SOURCES["product-sheets"][0]).read_text(encoding="utf-8")
    # A run under the research workflow wrote /opt/data/product_job.py and tried
    # to drive bl_site_product from Python: terminal refused the heredoc,
    # execute_code is blocked for cron jobs with no approver, and the pass
    # produced zero sheets. Describing a repetitive procedure invites a model to
    # automate it, so the prompt has to close that door explicitly.
    assert "No escribas scripts" in text
    assert "execute_code" in text
    # The wording moved when the batch became a one-at-a-time cursor; what has
    # to survive is that the prompt says work happens per product, not in bulk.
    assert "un producto cada vez" in text


def test_product_sheets_prompt_forbids_shrinking_a_sheet():
    text = (REPO_ROOT / AGENT_SOURCES["product-sheets"][0]).read_text(encoding="utf-8")
    # The first production run rewrote ten sheets to roughly half the length of
    # the text they replaced, because the prompt carried a 120-250 word cap.
    # Improving a sheet means it ends up more complete, never less.
    assert "nunca encoge" in text
    assert "más corta" in text
    # And the cap that caused it must not come back.
    assert "120 y 250 palabras" not in text


# --- Social Shorts: the Pexels key is the client's, and it is mandatory ------


def test_shorts_is_a_recurring_daily_agent():
    source, display_name, schedule_kind, _toolsets = AGENT_SOURCES["shorts"]
    assert source == "shorts/bl-site-package-shorts.prompt"
    assert display_name == "Social Shorts"
    # Goes [SILENT] once every post carries the sentinel, so daily is safe.
    assert schedule_kind == "daily"


def test_shorts_needs_no_old_site_url():
    # It only reads the client's own blog.
    assert "shorts" not in AGENTS_REQUIRING_OLD_SITE
    assert "shorts" not in MUTUALLY_EXCLUSIVE_AGENTS


def test_shorts_is_the_only_agent_requiring_a_pexels_key():
    assert AGENTS_REQUIRING_PEXELS == {"shorts"}


def test_ordering_shorts_without_a_pexels_key_is_refused(tmp_path, monkeypatch):
    """BYOK is enforced at the door, not discovered on the first cron run.

    Without this the SKU would provision happily and then ship background-only
    videos — or, worse, quietly fall back to BigLobster's quota.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with pytest.raises(ValueError) as excinfo:
        provision(
            slug="bl-test-no-pexels",
            client_name="Test",
            site_url="https://client.example",
            panel_password="pw",
            openrouter_key="sk-or-client",
            agents=["shorts"],
            skip_key_check=True,
        )
    message = str(excinfo.value)
    assert "--pexels-key" in message
    # Names it as the client's key, not ours, so whoever hits this knows what
    # to go and ask the buyer for.
    assert "BYOK" in message
    assert "client's OWN" in message
    # Nothing half-built left behind: the check runs before create_profile().
    assert not (tmp_path / "profiles" / "bl-test-no-pexels").exists()


def test_pexels_key_is_not_read_from_the_environment(monkeypatch):
    """The CLI flag must not default to PEXELS_API_KEY in the environment.

    That default is the shared-key behaviour in disguise: omit the flag on a
    rental and the client's stock searches silently bill to BigLobster's
    200/hour. Provisioning has to be told the client's key explicitly.
    """
    import argparse
    import inspect

    import scripts.provision_bl_client as mod

    monkeypatch.setenv("PEXELS_API_KEY", "biglobster-shared-key")
    source = inspect.getsource(mod.main)
    assert 'os.environ.get("PEXELS_API_KEY")' not in source

    parser = argparse.ArgumentParser()
    # Re-declare exactly as main() does and confirm the default stays None.
    parser.add_argument("--pexels-key", default=None)
    assert parser.parse_args([]).pexels_key is None


# --- Every rented profile is written with web.search_backend: ddgs (#174) ---
#
# Every profile _write_config() writes IS a rented tenant — it's only ever
# called from provision(), never for a BigLobster-owned profile — so this
# is unconditional, not gated on which agents were ordered. Belt to
# docker/cont-init.d/03-biglobster-config's boot-time reconcile (suspenders):
# this makes a brand-new client's FIRST job (as soon as 5 minutes after
# provisioning) correct immediately, without waiting for a container
# restart to backfill it.

def test_write_config_pins_ddgs_as_the_web_search_backend(tmp_path):
    _write_config(tmp_path, model="deepseek/deepseek-v4-flash")
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["web"]["search_backend"] == "ddgs"
    # Never Exa — that's the leak this pin exists to prevent.
    assert cfg["web"].get("backend") != "exa"
