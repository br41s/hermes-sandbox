#!/usr/bin/env python3
"""Provision a rented bl-site-package agent for a new client — the
deterministic version of the manual runbook in AGENT_RENTAL_SETUP.md.

Replaces the CEO-via-Telegram + Hermes-follows-a-markdown-runbook flow with
one call: creates the client's isolated profile, writes its SOUL.md, config.yaml
(the base/orchestrator model + the client's chosen FAL image model — without it
the profile has no model and every cron run 400s), and .env (BL_SITE_URL /
BL_SITE_PANEL_PASSWORD / their own OPENROUTER_API_KEY and FAL_KEY — BYOK, never
BigLobster's own keys), and registers one cron
job per agent they ordered, each pointed at the *shared* prompt template for
that agent (gap-hunter/bl-site-package-gap-hunter.prompt, etc.) — no per-client
prompt file is ever created.

Two callers, one code path:

* the CEO (or Hermes acting on the CEO's explicit request) running it by hand
  for a rental ordered through the manual flow, and
* ``hermes_cli/bl_rental_webhook.py`` — the authenticated, payment-confirmed
  webhook BigLobster's Stripe side calls, which provisions with no human in
  the per-order loop. See AGENT_RENTAL_SETUP.md for that contract.

Both go through ``provision()`` so an LLM can never skip a runbook step (e.g.
forgetting to validate the key before the job goes live).

The ``site-setup`` agent is the *Site Launch* checkout product. Ordering it
requires ``--questionnaire`` — the buyer's structured form answers — and
applies bl-site-package's fixed five-page template deterministically
(``scripts/bl_site_setup.py``) before scheduling the one-shot copywriting job.

Usage (must run with the repo's venv Python — the bare `python3` on PATH
won't have PyYAML and other deps this imports, e.g. via cron/jobs.py):
    .venv/bin/python3 scripts/provision_bl_client.py \\
        --slug bl-cliente-nieto \\
        --client-name "Francisco Nieto" \\
        --site-url https://blcliente.zeabur.app \\
        --panel-password '...' \\
        --openrouter-key sk-or-... \\
        --fal-key <key_id>:<key_secret> \\
        --agents gap-hunter,seo,onboarding-content,product-articles,infographic,maintenance \\
        --old-site-url https://their-old-site.example.com

`--fal-key` is the client's own FAL key (BYOK) for image generation — blog
covers and page images are billed to it, never BigLobster's. It's validated
against FAL at provision time and written to the profile .env as FAL_KEY. Omit
it if the client didn't order image generation; agents then publish text-only
(they never block on a missing image). The FAL image model is taken from
`--image-model`, else the client's panel choice (GET /api/site/config
`image_model`), else the FAL default.

`--old-site-url` is required when `onboarding-content` and/or
`product-articles` is ordered. `onboarding-content` is a one-shot agent that
scans the old site once, shortly after provisioning, and populates the new
site's blank pages from it. `product-articles` is daily (like gap-hunter/seo)
but scoped to the old site's product catalog: it crawls it for product pages,
skips ones it's already covered, and writes up to 3 new product blog posts
per run (draft, with a CTA button back to the original product page) until
the catalog is exhausted. Omit both flags if the client has no existing site.

`maintenance` is the *Website Maintenance* subscription product: a daily
deterministic health check (`tools/bl_site_health_tool.py`) plus a closed list
of mechanical fixes, and one client-facing report per calendar month. It needs
no extra flags — it only ever reads the client's own site.

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
from scripts.bl_site_setup import apply_site_template, validate_answers  # noqa: E402

# agent key -> (prompt_source relative to repo root, display name, schedule kind)
# schedule kind "daily" = recurring via pick_stagger_schedule(); "once" = single
# run a few minutes after provisioning (see provision()).
AGENT_SOURCES = {
    "gap-hunter": ("gap-hunter/bl-site-package-gap-hunter.prompt", "Content Gap Hunter", "daily"),
    "seo": ("onsite-seo/bl-site-package-seo-agent.prompt", "SEO/GEO On-Site", "daily"),
    "onboarding-content": (
        "onboarding-content/bl-site-package-onboarding-content.prompt",
        "Onboarding Content Agent",
        "once",
    ),
    "product-articles": (
        "product-articles/bl-site-package-product-articles.prompt",
        "Product Article Agent",
        "daily",
    ),
    # Adds ONE inline-SVG infographic to ONE existing blog post per run, editing
    # it in place. Needs no --old-site-url: it only ever reads the client's own
    # blog. Goes quiet ([SILENT]) once every post already carries one, so it's
    # safe to leave scheduled daily on a small blog.
    "infographic": (
        "infographic/bl-site-package-infographic.prompt",
        "Infographic Engineer",
        "daily",
    ),
    # The "Website Maintenance" subscription product. Daily, like gap-hunter:
    # availability and publish-drift are only meaningful checked often, and a
    # weekly cadence would let a site sit broken for six days. The monthly
    # client report is NOT a second job — bl_site_health returns report_due
    # once per calendar month, so the same daily run produces it exactly once
    # (two jobs would race for the same "have I reported yet" state).
    "maintenance": (
        "maintenance/bl-site-package-maintenance.prompt",
        "Website Maintenance",
        "daily",
    ),
    # The "Site Launch" checkout product. One-shot, like onboarding-content,
    # but it is the *whole* create-your-website job: the deterministic half
    # (setup wizard, identity/legal fields, logo) runs in-process here via
    # scripts/bl_site_setup.py BEFORE the job is scheduled, and this prompt
    # only writes copy into the same fixed five-page field list. Needs no
    # --old-site-url: a buyer with no previous site is the normal case.
    "site-setup": (
        "site-setup/bl-site-package-site-setup.prompt",
        "Site Launch",
        "once",
    ),
}

# Agents that need --old-site-url (the client's existing site to migrate/read from).
AGENTS_REQUIRING_OLD_SITE = {"onboarding-content", "product-articles"}

# site-setup and onboarding-content both do the initial page fill. Ordering
# both would have two one-shot jobs racing to write the same fields.
MUTUALLY_EXCLUSIVE_AGENTS = ("site-setup", "onboarding-content")

# Delay before the one-shot onboarding-content job fires — long enough that
# the profile/.env writes below are certainly flushed to disk first.
ONBOARDING_CONTENT_DELAY = "5m"

# Base/orchestrator model for the rented profile. Billed to the CLIENT's own
# BYOK OpenRouter key (profile .env OPENROUTER_API_KEY), so the default is the
# cheap orchestrator the auditor already uses. A profile with no config.yaml
# has NO base model, and the orchestrator loop then calls OpenRouter with no
# model → RuntimeError: 400 "No models provided" and the agent silently does
# nothing (confirmed on bl-shoroban, 2026-07-24). Override with --model.
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"

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


class KeyValidationError(ValueError):
    """The client's own BYOK credentials are bad — not an infrastructure fault.

    Kept distinct from the generic ValueError so the payment-confirmed webhook
    can answer "the key the buyer gave us is rejected, ask them for a new one"
    (400, don't retry) instead of "something broke, retry" (502). It stays a
    ValueError so the CLI's existing handler keeps catching it.
    """


def _validate_openrouter_key(key: str) -> None:
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/auth/key",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                raise KeyValidationError(f"OpenRouter key check returned HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        raise KeyValidationError(f"OpenRouter key rejected: HTTP {exc.code} — {exc.read().decode(errors='replace')}")
    except urllib.error.URLError as exc:
        raise ValueError(f"Could not reach OpenRouter to validate the key: {exc.reason}")


def _validate_fal_key(key: str) -> None:
    """Prove the client's FAL image key is valid BEFORE the profile goes live.

    Mirrors _validate_openrouter_key. FAL has no free "check key" endpoint like
    OpenRouter's /auth/key, so we use FAL's short-lived-token exchange
    (rest.alpha.fal.ai/tokens/ — the same call fal's own browser SDK makes).
    It does NOT generate an image, so it costs nothing. A valid key authenticates
    (200, or 422 if the request body shape drifts — still past auth); an invalid
    key is rejected at auth (401/403). Any other status / unreachable is treated
    as "couldn't verify" rather than a hard fail, so a FAL API change never bricks
    provisioning after the format check already passed.
    """
    if ":" not in key or len(key.strip()) < 16:
        raise KeyValidationError("FAL key doesn't look valid (expected '<key_id>:<key_secret>').")
    body = json.dumps({"allowed_apps": ["fal-ai/flux-2/klein/9b"], "token_expiration": 300}).encode()
    req = urllib.request.Request(
        "https://rest.alpha.fal.ai/tokens/",
        data=body,
        headers={"Authorization": f"Key {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=15).close()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise KeyValidationError(f"FAL key rejected: HTTP {exc.code} — check the key is correct.")
        # Other status (422/404/5xx): got past auth or endpoint drifted — accept.
    except urllib.error.URLError as exc:
        # Unreachable — don't block provisioning; the format check already ran.
        print(f"Warning: could not reach FAL to validate the key ({exc.reason}); continuing.", file=sys.stderr)


def _read_panel_image_model(site_url: str) -> str | None:
    """Best-effort read of the client's chosen image_model from their panel.

    The client picks their FAL image model in the site panel; it's exposed at
    GET /api/site/config. We snapshot it here and pin it into the profile
    config.yaml (image_gen.model) so generation uses the client's choice.
    Returns None on any error or if unset — the caller then leaves the model
    unset and _resolve_fal_model() falls back to the FAL default.
    """
    try:
        result = _http_json("GET", f"{site_url}/api/site/config")
        value = result.get("image_model")
        return value.strip() if isinstance(value, str) and value.strip() else None
    except Exception:
        return None


def _http_json(method: str, url: str) -> dict:
    req = urllib.request.Request(url, method=method)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _validate_model_call(key: str, model: str) -> None:
    """Prove the profile can actually make a model call before it goes live.

    The key check above only proves the key is *valid*; it says nothing about
    whether the chosen model is callable on this client's account (typo'd id,
    model retired, no credits for it). Because the one-shot onboarding-content
    job auto-removes itself after its single run, a first run that 400s can't
    be re-run by id — so we validate the model here, BEFORE the profile and its
    jobs are created, and fail loudly instead of every cron run failing
    silently. Costs one 1-token completion, billed to the client's own key.
    """
    body = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    ).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                raise KeyValidationError(f"Model check for '{model}' returned HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        raise KeyValidationError(
            f"Model '{model}' is not callable on this key: HTTP {exc.code} — "
            f"{exc.read().decode(errors='replace')}"
        )
    except urllib.error.URLError as exc:
        raise ValueError(f"Could not reach OpenRouter to validate the model: {exc.reason}")


def _write_config(profile_dir: Path, model: str, image_model: str | None = None) -> Path:
    """Write the profile's config.yaml with a base/orchestrator model.

    Only the model block is written — everything else is deep-merged from
    DEFAULT_CONFIG at runtime (see hermes_cli.config.load_config), so this
    mirrors exactly what `hermes -p <slug> config set model.default …` produces.
    Without this file the profile has no base model at all (the Shoroban bug).

    When ``image_model`` is given (the client's panel choice), pin it as
    ``image_gen.model`` so the shared image_generate tool's _resolve_fal_model()
    picks it up per-profile — the client's FAL image model, billed to their own
    FAL_KEY. Omitted → _resolve_fal_model() falls back to the FAL default.
    """
    import yaml

    data: dict = {"model": {"default": model, "provider": "openrouter"}}
    if image_model:
        data["image_gen"] = {"model": image_model}

    config_path = profile_dir / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(data, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    return config_path


def _write_env(
    profile_dir: Path,
    site_url: str,
    panel_password: str,
    openrouter_key: str,
    old_site_url: str | None = None,
    fal_key: str | None = None,
) -> Path:
    env_path = profile_dir / ".env"
    contents = (
        f"BL_SITE_URL={site_url}\n"
        f"BL_SITE_PANEL_PASSWORD={panel_password}\n"
        f"OPENROUTER_API_KEY={openrouter_key}\n"
    )
    if old_site_url:
        contents += f"OLD_SITE_URL={old_site_url}\n"
    # Client's own FAL key (BYOK) — image generation is billed to it, never
    # BigLobster's. Same per-profile resolution as OPENROUTER_API_KEY, so the
    # shared image_generate tool picks it up when a job runs under this profile.
    if fal_key:
        contents += f"FAL_KEY={fal_key}\n"
    env_path.write_text(contents, encoding="utf-8")
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
    old_site_url: str | None = None,
    model: str = DEFAULT_MODEL,
    fal_key: str | None = None,
    image_model: str | None = None,
    questionnaire: dict | None = None,
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
    needs_old_site = AGENTS_REQUIRING_OLD_SITE & set(agents)
    if needs_old_site and not old_site_url:
        raise ValueError(
            f"{sorted(needs_old_site)} need(s) --old-site-url (the client's "
            "existing site to read/migrate content from) — omit the agent(s) if "
            "the client has no existing site to draw from."
        )
    if set(MUTUALLY_EXCLUSIVE_AGENTS) <= set(agents):
        raise ValueError(
            f"{list(MUTUALLY_EXCLUSIVE_AGENTS)} both do the initial page fill and "
            "would race each other. Order site-setup (the Site Launch product) or "
            "onboarding-content, not both."
        )
    if "site-setup" in agents and not questionnaire:
        raise ValueError(
            "site-setup needs --questionnaire (the buyer's structured form "
            "answers) — it is what the template is applied from."
        )

    # Validate the questionnaire against the fixed schema BEFORE anything is
    # created or any key is spent: a drifted BigLobster form must fail here,
    # not halfway through a paid order.
    if questionnaire is not None:
        questionnaire = validate_answers(questionnaire)

    # Validate the key AND that the chosen model is callable BEFORE creating
    # the profile/jobs — a broken model must never reach the point where a
    # one-shot job auto-removes itself on a silent 400.
    if not skip_key_check:
        _validate_openrouter_key(openrouter_key)
        _validate_model_call(openrouter_key, model)
        if fal_key:
            _validate_fal_key(fal_key)

    # Apply the fixed site template from the questionnaire BEFORE creating the
    # profile: it's the step most likely to fail on a fresh instance
    # (unreachable, already claimed under another password, bad logo), and
    # failing here leaves no half-built profile behind. It is idempotent, so a
    # retried provision after a later failure re-converges rather than 409ing.
    site_setup_report = None
    if "site-setup" in agents:
        site_setup_report = apply_site_template(
            site_url,
            panel_password,
            questionnaire,
            openrouter_key,
            image_model=image_model,
        )

    # Resolve the FAL image model to pin per-profile: explicit flag wins,
    # otherwise snapshot the client's panel choice (image_model in the site
    # config). None → _resolve_fal_model() uses the FAL default.
    resolved_image_model = image_model or _read_panel_image_model(site_url)

    profile_dir = create_profile(canon, no_skills=True, description=f"bl-site-package rental: {client_name}")
    (profile_dir / "SOUL.md").write_text(
        SOUL_TEMPLATE.format(client_name=client_name), encoding="utf-8"
    )
    # Write config.yaml so the profile has a base/orchestrator model. Without
    # it the profile has none → orchestrator 400 "No models provided".
    config_path = _write_config(profile_dir, model, image_model=resolved_image_model)
    env_path = _write_env(
        profile_dir, site_url, panel_password, openrouter_key, old_site_url, fal_key=fal_key
    )

    created_jobs = []
    for agent_key in agents:
        source, display_name, schedule_kind = AGENT_SOURCES[agent_key]
        schedule = (
            ONBOARDING_CONTENT_DELAY
            if schedule_kind == "once"
            else pick_stagger_schedule(canon, agent_key)
        )
        job = create_job(
            prompt=Path(REPO_ROOT, source).read_text(encoding="utf-8"),
            schedule=schedule,
            name=f"{display_name} — {client_name}",
            deliver=deliver,
            profile=canon,
            prompt_source=source,
        )
        created_jobs.append({"job_id": job["id"], "name": job["name"], "schedule": schedule, "source": source})

    return {
        "profile": canon,
        "profile_dir": str(profile_dir),
        "config_path": str(config_path),
        "model": model,
        "image_model": resolved_image_model or "(FAL default)",
        "fal_key": "set" if fal_key else "not set (image generation disabled)",
        "env_path": str(env_path),
        "site_setup": site_setup_report,
        "jobs": created_jobs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--client-name", required=True)
    parser.add_argument("--site-url", required=True)
    parser.add_argument("--panel-password", required=True)
    parser.add_argument("--openrouter-key", required=True)
    parser.add_argument("--agents", required=True, help="Comma-separated: gap-hunter,seo,onboarding-content,product-articles,infographic,maintenance,site-setup")
    parser.add_argument("--deliver", default="local", help="Cron job delivery target (default: local)")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Base/orchestrator model for the profile, billed to the client's own OpenRouter key (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--fal-key",
        default=None,
        help="Client's own FAL image key (BYOK). Written to the profile .env as FAL_KEY and "
        "validated against FAL. Required for image generation (blog covers / page images); "
        "omit if the client didn't order image generation.",
    )
    parser.add_argument(
        "--image-model",
        default=None,
        help="FAL image model id to pin for this client (e.g. fal-ai/flux-2-pro). Defaults to "
        "the client's panel choice (GET /api/site/config image_model), else the FAL default.",
    )
    parser.add_argument(
        "--questionnaire",
        default=None,
        help="Path to the buyer's structured form answers (JSON). Required when "
        "--agents includes site-setup — it is what the fixed site template is "
        "applied from. Schema: scripts/bl_site_setup.py.",
    )
    parser.add_argument("--skip-key-check", action="store_true", help="Skip the live OpenRouter/FAL key + model validation calls")
    parser.add_argument(
        "--old-site-url",
        default=None,
        help="Client's existing site to migrate/read content from — required if --agents includes onboarding-content and/or product-articles",
    )
    args = parser.parse_args()

    agents = [a.strip() for a in args.agents.split(",") if a.strip()]

    questionnaire = None
    if args.questionnaire:
        try:
            with open(args.questionnaire, encoding="utf-8") as fh:
                questionnaire = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Provisioning failed: could not read --questionnaire: {exc}", file=sys.stderr)
            return 1

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
            old_site_url=args.old_site_url,
            model=args.model,
            fal_key=args.fal_key,
            image_model=args.image_model,
            questionnaire=questionnaire,
        )
    except (ValueError, FileExistsError) as exc:
        print(f"Provisioning failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    print(
        f"\nProfile '{result['profile']}' ready on model '{result['model']}' "
        f"with {len(result['jobs'])} job(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
