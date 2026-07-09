#!/usr/bin/env python3
"""Provision a rented bl-site-package agent for a new client — the
deterministic version of the manual runbook in AGENT_RENTAL_SETUP.md.

Replaces the CEO-via-Telegram + Hermes-follows-a-markdown-runbook flow with
one call: creates the client's isolated profile, writes its SOUL.md and .env
(BL_SITE_URL / BL_SITE_PANEL_PASSWORD / their own OPENROUTER_API_KEY — BYOK,
never BigLobster's own key), and registers one cron job per agent they
ordered, each pointed at the *shared* prompt template for that agent
(gap-hunter/bl-site-package-gap-hunter.prompt, etc.) — no per-client prompt
file is ever created.

This does NOT wire up an automatic trigger from bl-site-package's customer
panel — that panel has no payment gate yet (see AGENT_RENTAL_SETUP.md), so
an unauthenticated auto-trigger would let anyone spin up profiles and cron
jobs for free. Until that gate exists, this script is meant to be run
explicitly (by the CEO or by Hermes acting on the CEO's explicit request),
the same trust boundary the manual runbook has today — it just removes the
chance of an LLM skipping a runbook step (e.g. forgetting to validate the
key before the job goes live).

Usage:
    python scripts/provision_bl_client.py \\
        --slug bl-cliente-nieto \\
        --client-name "Francisco Nieto" \\
        --site-url https://blcliente.zeabur.app \\
        --panel-password '...' \\
        --openrouter-key sk-or-... \\
        --agents gap-hunter,seo

Removing a client (unchanged from the runbook — still manual, still
confirmed by hand): remove its cron jobs, then `hermes profile delete <slug>`.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
os.environ.setdefault("HERMES_HOME", os.path.join(os.path.expanduser("~"), ".hermes"))

from hermes_cli.profiles import (  # noqa: E402
    create_profile,
    normalize_profile_name,
    profile_exists,
    validate_profile_name,
)
from cron.jobs import create_job  # noqa: E402

# agent key -> (prompt_source relative to repo root, display name)
AGENT_SOURCES = {
    "gap-hunter": ("gap-hunter/bl-site-package-gap-hunter.prompt", "Content Gap Hunter"),
    "seo": ("onsite-seo/bl-site-package-seo-agent.prompt", "SEO/GEO On-Site"),
}

SOUL_TEMPLATE = """# {client_name} — Hermes Agent (rented, bl-site-package)

You are Hermes Agent, an intelligent AI assistant created by Nous Research.

## Scope
- This profile is dedicated to ONE bl-site-package client: {client_name}.
- Their site is `BL_SITE_URL` (this profile's .env) — the only site you ever touch.
- You publish exclusively through the `bl_site_publish` tool. Never git, never PR,
  never another client's site or profile.
- Never modify, overwrite, or delete SOUL.md — restored automatically.

## Communication
- Reply in Spanish unless the client's own site config says otherwise.
- Escalate to the CEO only for: destructive ops, ambiguous requirements, security concerns.
- Never send/publish/schedule anything externally beyond what the agent's own
  prompt already authorizes (draft-only blog posts, direct on-site text edits).
"""


def _validate_openrouter_key(key: str) -> None:
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/auth/key",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                raise ValueError(f"OpenRouter key check returned HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        raise ValueError(f"OpenRouter key rejected: HTTP {exc.code} — {exc.read().decode(errors='replace')}")
    except urllib.error.URLError as exc:
        raise ValueError(f"Could not reach OpenRouter to validate the key: {exc.reason}")


def _write_env(profile_dir: Path, site_url: str, panel_password: str, openrouter_key: str) -> Path:
    env_path = profile_dir / ".env"
    env_path.write_text(
        f"BL_SITE_URL={site_url}\n"
        f"BL_SITE_PANEL_PASSWORD={panel_password}\n"
        f"OPENROUTER_API_KEY={openrouter_key}\n",
        encoding="utf-8",
    )
    os.chmod(env_path, 0o600)
    return env_path


def pick_stagger_schedule(slug: str, agent_key: str) -> str:
    """Pick a daily off-peak cron expression for this client+agent's job.

    TODO(product decision): this just hashes (slug, agent_key) into a minute
    inside a 02:00-06:00 window so customers don't all fire at once — it's
    deterministic and collision-*resistant* (not collision-free: two clients
    can still land on the same minute, which is fine for cron, just not
    perfectly spread). Revisit if you'd rather track "next free slot" against
    existing jobs for a truly collision-free spread, or want the window itself
    configurable per agent type. Returns a 5-field cron expression (the
    scheduler has no "daily HH:MM" shorthand — see cron/jobs.py:parse_schedule).
    """
    import hashlib

    digest = hashlib.sha256(f"{slug}:{agent_key}".encode()).hexdigest()
    minute_offset = int(digest[:8], 16) % (4 * 60)  # spread across 4h window
    hour = 2 + minute_offset // 60
    minute = minute_offset % 60
    return f"{minute} {hour} * * *"


def provision(
    slug: str,
    client_name: str,
    site_url: str,
    panel_password: str,
    openrouter_key: str,
    agents: list[str],
    deliver: str = "local",
    skip_key_check: bool = False,
) -> dict:
    canon = normalize_profile_name(slug)
    validate_profile_name(canon)
    if profile_exists(canon):
        raise FileExistsError(
            f"Profile '{canon}' already exists. Remove it first "
            f"(`hermes profile delete {canon}`) if you want to re-provision, "
            f"or pick a different slug."
        )

    unknown = [a for a in agents if a not in AGENT_SOURCES]
    if unknown:
        raise ValueError(f"Unknown agent(s) {unknown} — choose from {list(AGENT_SOURCES)}")
    if not agents:
        raise ValueError("At least one agent must be ordered")

    if not skip_key_check:
        _validate_openrouter_key(openrouter_key)

    profile_dir = create_profile(canon, no_skills=True, description=f"bl-site-package rental: {client_name}")
    (profile_dir / "SOUL.md").write_text(
        SOUL_TEMPLATE.format(client_name=client_name), encoding="utf-8"
    )
    env_path = _write_env(profile_dir, site_url, panel_password, openrouter_key)

    created_jobs = []
    for agent_key in agents:
        source, display_name = AGENT_SOURCES[agent_key]
        cron_expr = pick_stagger_schedule(canon, agent_key)
        job = create_job(
            prompt=Path(REPO_ROOT, source).read_text(encoding="utf-8"),
            schedule=cron_expr,
            name=f"{display_name} — {client_name}",
            deliver=deliver,
            profile=canon,
            prompt_source=source,
        )
        created_jobs.append({"job_id": job["id"], "name": job["name"], "schedule": cron_expr, "source": source})

    return {
        "profile": canon,
        "profile_dir": str(profile_dir),
        "env_path": str(env_path),
        "jobs": created_jobs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--client-name", required=True)
    parser.add_argument("--site-url", required=True)
    parser.add_argument("--panel-password", required=True)
    parser.add_argument("--openrouter-key", required=True)
    parser.add_argument("--agents", required=True, help="Comma-separated: gap-hunter,seo")
    parser.add_argument("--deliver", default="local", help="Cron job delivery target (default: local)")
    parser.add_argument("--skip-key-check", action="store_true", help="Skip the live OpenRouter key validation call")
    args = parser.parse_args()

    agents = [a.strip() for a in args.agents.split(",") if a.strip()]

    try:
        result = provision(
            slug=args.slug,
            client_name=args.client_name,
            site_url=args.site_url,
            panel_password=args.panel_password,
            openrouter_key=args.openrouter_key,
            agents=agents,
            deliver=args.deliver,
            skip_key_check=args.skip_key_check,
        )
    except (ValueError, FileExistsError) as exc:
        print(f"Provisioning failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    print(f"\nProfile '{result['profile']}' ready with {len(result['jobs'])} job(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
