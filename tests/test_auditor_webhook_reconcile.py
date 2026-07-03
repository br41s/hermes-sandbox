"""Contract test: 03-biglobster-config wires the auditor's GitHub PR webhook
trigger and downgrades its own poll to a 6h safety net.

The auditor's cron job used to fire every 10 minutes just to find out there
was nothing to review (~95% wasted LLM turns). §6e enables the webhook
platform on the MAIN profile only (the auditor profile must never run its own
gateway — see _GATEWAYLESS_PROFILES in hermes_cli/container_boot.py, a prod
incident on 2026-06-24) with a route whose fixed prompt does nothing but force
the auditor's real job due via ``hermes cron run <id>``.

Content-assertion style (matching tests/test_auditor_provider_pinning.py):
executing the real cont-init script needs root + s6-setuidgid. We assert the
reconcile block's invariants on the script text, plus functionally exercise
the config- and schedule-reconcile logic on sample data.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOT_SCRIPT = REPO_ROOT / "docker" / "cont-init.d" / "03-biglobster-config"


@pytest.fixture(scope="module")
def boot_text() -> str:
    if not BOOT_SCRIPT.exists():
        pytest.skip("docker/cont-init.d/03-biglobster-config not present")
    return BOOT_SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def webhook_block(boot_text: str) -> str:
    """The full §6e section, from its comment header to the closing PYEOF."""
    start = boot_text.index("# --- 6e: auditor GitHub webhook trigger")
    end = boot_text.index("\nPYEOF\n", start) + len("\nPYEOF\n")
    return boot_text[start:end]


def test_webhook_route_targets_auditor_job_dynamically(webhook_block: str) -> None:
    # The job id must be discovered at boot (profile == "auditor"), never
    # hardcoded — a hardcoded id would silently stop matching if the auditor
    # job is ever recreated with a new id.
    assert 'job.get("profile") == "auditor"' in webhook_block
    assert 'hermes cron run {auditor_job_id}' in webhook_block


def test_webhook_route_is_content_free(webhook_block: str) -> None:
    # The route must never do the actual review — no skills, no
    # github_comment delivery — it only forces the real cron job due.
    assert '"deliver": "log"' in webhook_block
    assert 'github_comment' not in webhook_block
    assert '"skills"' not in webhook_block


def test_schedule_guard_compares_minutes_not_display_string(webhook_block: str) -> None:
    # parse_schedule("every 6 hours") normalizes to display "every 360m", not
    # "every 6 hours" — comparing against the literal phrase would never
    # match and would rewrite the job (and reset next_run_at) on every boot.
    assert 'sched.get("minutes") != 360' in webhook_block
    assert '"display") != "every 6 hours"' not in webhook_block


def test_webhook_platform_enabled_only_on_main_profile(webhook_block: str) -> None:
    # Must patch home / "config.yaml" (main), never a profiles/<name>/config.yaml
    # — the auditor profile is gatewayless and must never host this listener.
    assert 'config_path = home / "config.yaml"' in webhook_block
    assert "prof.name" not in webhook_block
    assert "for prof in" not in webhook_block


def test_config_reconcile_logic_on_sample_configs() -> None:
    """Functionally replay the config.yaml reconcile snippet."""
    def reconcile(cfg: dict, auditor_job_id: str, secret: str) -> bool:
        changed = False
        platforms = cfg.get("platforms")
        if not isinstance(platforms, dict):
            platforms = {}
            cfg["platforms"] = platforms
        webhook = platforms.get("webhook")
        if not isinstance(webhook, dict):
            webhook = {}
            platforms["webhook"] = webhook
        if webhook.get("enabled") is not True:
            webhook["enabled"] = True
            changed = True
        extra = webhook.get("extra")
        if not isinstance(extra, dict):
            extra = {}
            webhook["extra"] = extra
        if extra.get("port") != 8644:
            extra["port"] = 8644
            changed = True
        routes = extra.get("routes")
        if not isinstance(routes, dict):
            routes = {}
            extra["routes"] = routes
        route = {
            "secret": secret,
            "events": ["pull_request"],
            "prompt": f"...hermes cron run {auditor_job_id}",
            "deliver": "log",
        }
        if routes.get("auditor-pr-trigger") != route:
            routes["auditor-pr-trigger"] = route
            changed = True
        return changed

    # Fresh config: everything created, route wired.
    cfg: dict = {"model": {"default": "some/model"}}
    assert reconcile(cfg, "job_abc123", "s3cr3t") is True
    assert cfg["platforms"]["webhook"]["enabled"] is True
    assert cfg["platforms"]["webhook"]["extra"]["port"] == 8644
    route = cfg["platforms"]["webhook"]["extra"]["routes"]["auditor-pr-trigger"]
    assert route["events"] == ["pull_request"]
    assert "job_abc123" in route["prompt"]

    # Second boot, same inputs: idempotent, no rewrite.
    assert reconcile(cfg, "job_abc123", "s3cr3t") is False

    # A rotated secret or a recreated job id must be picked up.
    assert reconcile(cfg, "job_abc123", "new-secret") is True
    assert reconcile(cfg, "job_new456", "new-secret") is True

    # Unrelated existing webhook routes (e.g. hand-configured by the CEO) survive.
    cfg2: dict = {"platforms": {"webhook": {"enabled": True, "extra": {
        "port": 8644, "routes": {"other-route": {"secret": "x", "events": []}},
    }}}}
    assert reconcile(cfg2, "job_abc123", "s3cr3t") is True
    assert "other-route" in cfg2["platforms"]["webhook"]["extra"]["routes"]


def test_schedule_reconcile_logic_on_sample_jobs() -> None:
    """Functionally replay the schedule-downgrade guard (§6e)."""
    def needs_downgrade(job: dict) -> bool:
        sched = job.get("schedule") or {}
        return sched.get("kind") != "interval" or sched.get("minutes") != 360

    # The live job's actual schedule shape today: every 10 minutes.
    assert needs_downgrade(
        {"schedule": {"kind": "interval", "minutes": 10, "display": "every 10m"}}
    ) is True

    # Already downgraded: no-op.
    assert needs_downgrade(
        {"schedule": {"kind": "interval", "minutes": 360, "display": "every 360m"}}
    ) is False

    # A cron-expression or one-shot schedule must also be treated as needing
    # a downgrade (kind != "interval" short-circuits before checking minutes).
    assert needs_downgrade({"schedule": {"kind": "cron", "expr": "0 * * * *"}}) is True
    assert needs_downgrade({"schedule": {}}) is True
    assert needs_downgrade({}) is True
