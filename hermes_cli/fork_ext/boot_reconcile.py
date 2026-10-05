"""Per-boot env sync and config reconcile (fork-owned).

Run by ``docker/cont-init.d/03-biglobster-config`` as the hermes user, every boot::

    python -m hermes_cli.fork_ext.boot_reconcile

It used to be a 630-line heredoc inside that shell hook, which no test could call: the
contract tests grepped its text and replayed copies of its snippets, so they tested a
copy. Moved here verbatim in behaviour, so the tests call the real code.

Section numbers match the hook's (§1 env sync, §2 config keys). Idempotent: a boot with
nothing to change rewrites no config.yaml. Never fatal: the hook catches a non-zero exit.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Mapping, Optional

# Source of truth for which Telegram thread each profile owns: the per-profile
# routing.env bundled in the image. The same file already drives outbound
# delivery (TELEGRAM_HOME_CHANNEL_THREAD_ID, injected into each profile's .env);
# we reuse it to derive the INBOUND topic->profile bindings too.
PROFILES_SRC = Path("/opt/hermes/docker/profiles")

# 1. Sync Zeabur process env → .env (override wins via load_dotenv(override=True)).
#    The same keys are ALSO propagated into every profile's .env so a key
#    rotation (which only updates the process env / main .env) doesn't leave
#    per-profile gateways running on a stale, revoked key. This is the failure
#    mode that broke the grow-shop profile after the 2026-06-05 secret rotation:
#    the main .env got the new OPENROUTER_API_KEY but profiles/grow-shop/.env
#    kept the old (revoked) one → 401 "User not found" on every grow-shop turn.
#
#    POLICY: the main .env is the single source of truth for these keys and we
#    OVERWRITE the per-profile value. If you later want per-tenant billing with
#    a DISTINCT OPENROUTER_API_KEY per profile, drop that key from `INJECT`
#    (or special-case it) before deploying.
#    GH_TOKEN is synced alongside GITHUB_TOKEN (mirrored to the same value by
#    the hook's token-resolution block) so any consumer that reads only GH_TOKEN
#    — and the gateway/delegate process env via load_dotenv — gets it too.
INJECT = [
    "OPENROUTER_API_KEY", "HERMES_CALLBACK_SECRET", "HERMES_CALLBACK_URL",
    "HERMES_MAX_ITERATIONS", "EXA_API_KEY", "HUGGINGFACE_API_KEY",
    "GITHUB_TOKEN", "GH_TOKEN", "AUXILIARY_VISION_MODEL",
    # Read-only Google Search Console service-account key (base64), consumed by
    # the gsc MCP server (config.yaml mcp_servers.gsc). See optional-mcps/gsc/.
    "GSC_SERVICE_ACCOUNT_B64",
    # Stock B-roll for the `shorts` agent (plugins/shorts/stock.py). This is
    # BigLobster's OWN key, for BigLobster's own profiles running shorts against
    # biglobster.top. Rented tenants bring their own and are excluded from it
    # (TENANT_EXCLUDE), exactly like OPENROUTER_API_KEY — being in this list is
    # only half the contract, and the half that keeps our rotation working.
    "PEXELS_API_KEY",
    # Langfuse, BigLobster's own project (stage 3 step 0c). A satellite store's
    # scope is its .env only, so without these a moved job traces nowhere, with
    # no error. The two keys are ALSO in TENANT_EXCLUDE: rentals trace into a
    # separate project, pinned in sync_envs from HERMES_RENTAL_LANGFUSE_*.
    # The base URL and labels are not secrets and reach every profile; the plugin
    # reads the HERMES_-prefixed name first, then the bare one, so both sync.
    "HERMES_LANGFUSE_PUBLIC_KEY", "HERMES_LANGFUSE_SECRET_KEY",
    "HERMES_LANGFUSE_BASE_URL", "LANGFUSE_BASE_URL",
    "HERMES_LANGFUSE_ENV", "HERMES_LANGFUSE_RELEASE", "HERMES_LANGFUSE_SAMPLE_RATE",
]

# Run-time cron tuning (HERMES_CRON_TIMEOUT, HERMES_CRON_MAX_RUNTIME, ...) is read
# from the run's scope (cron/env_settings.py), so a value set only in the Zeabur
# env is lost to a satellite store. Every HERMES_CRON_* in the service env syncs
# like an INJECT key. Tuning only, never a secret.
CRON_TUNING_PREFIX = "HERMES_CRON_"

# Never let the auditor profile's .env receive the shared token via the generic
# path — §1b is its ONLY source for these two keys.
AUDITOR_EXCLUDE = ("GITHUB_TOKEN", "GH_TOKEN")

# BYOK: a rented bl-site-package client brings their OWN OpenRouter key and their
# model usage bills to them. Injecting ours silently defeated that — every boot
# overwrote the client key written by provision_bl_client.py, so tenant agent runs
# billed to BigLobster (found on bl-shoroban 2026-07-31).
#
# PEXELS_API_KEY is the same contract for the `shorts` agent. Being free does not
# make it shareable: Pexels issues one key per account at 200 req/hour and
# 20k/month, so one key across the fleet caps every client at once and lets a heavy
# tenant throttle the rest into background-only videos. Each tenant brings their
# own, and provision_bl_client.py refuses to sell the SKU without it.
#
# Trade-off, accepted deliberately: rental profiles no longer get the rotation
# repair that §1's injection exists to provide (the 2026-06-05 grow-shop incident).
# That repair is the wrong thing for a key we do not own and must not rotate. A
# tenant whose own key dies now fails visibly on their own jobs instead of silently
# spending our credit.
#
# EXA_API_KEY / HUGGINGFACE_API_KEY are a DIFFERENT shape of leak: those two are
# ours alone and a tenant should never have EITHER — no rented prompt uses
# HuggingFace at all, and web research for tenants runs on the free ddgs backend
# (§2 forces web.search_backend: ddgs), never billed to BigLobster's Exa account.
# gap-hunter and product-articles (both daily SKUs) call web_search, so this was a
# live, recurring leak, not a theoretical one (issue #174).
TENANT_EXCLUDE = ("OPENROUTER_API_KEY", "PEXELS_API_KEY", "EXA_API_KEY", "HUGGINGFACE_API_KEY",
                  "HERMES_LANGFUSE_PUBLIC_KEY", "HERMES_LANGFUSE_SECRET_KEY")

# Rentals trace into their OWN Langfuse project (decided 2026-09-29,
# ops/multiplex-stage3-plan.md "Rental Langfuse"): a key in a client profile's reach
# can be read by that client's agent, and a Langfuse key pair reads every trace in
# its project. Pinned on every boot so a rotation in Zeabur reaches every rental.
RENTAL_LANGFUSE_SOURCE = ("HERMES_RENTAL_LANGFUSE_PUBLIC_KEY", "HERMES_RENTAL_LANGFUSE_SECRET_KEY")
# Every name the plugin resolves a key from (plugins/observability/langfuse
# _build_client: HERMES_LANGFUSE_* first, then LANGFUSE_*).
LANGFUSE_KEY_NAMES = ("HERMES_LANGFUSE_PUBLIC_KEY", "HERMES_LANGFUSE_SECRET_KEY",
                      "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
SHARED_RESEARCH_KEYS = ("EXA_API_KEY", "HUGGINGFACE_API_KEY")

# 2. Re-assert runtime-critical config keys (idempotent). docker/config.yaml
#    is the first-boot source of truth; here we only force the keys that must
#    track the intended deploy even on volumes seeded before a change.
#    Applied to BOTH the main config.yaml and every per-profile config.yaml so
#    new profiles created via `hermes profile create` get working provider
#    settings immediately without requiring a manual setup step.
OVERRIDES = {
    ("web", "backend"): "exa",
    ("image_gen", "provider"): "openrouter",
    ("video_gen", "provider"): "huggingface",
    ("memory", "memory_char_limit"): 6000,
    ("memory", "user_char_limit"): 3000,
    # Tool-iteration budget per agent run. Cron uses this as max_iterations
    # (cron/scheduler.py), and 60 was too tight for heavy jobs like the SEO/GEO
    # cron — they exhausted it mid-run, so the run ended completed=False and the
    # model's summary surfaced as a RuntimeError. Raised to 90 (run_agent's own
    # default). Enforced on main + every profile so the budget is uniform.
    ("agent", "max_turns"): 90,
    # Shutdown drain budgets must fit inside s6's stop grace (Dockerfile:
    # S6_KILL_GRACETIME=20s, the only s6 timer the gateway's dynamic slot gets), or a
    # deploy made while a chat turn or cron run is in flight is SIGKILLed mid-drain:
    # unclean exit, state.db quick_check on the next boot. The seed config asked for
    # 180s and upstream's cron default is 30s. Interrupted turns are marked
    # resume_pending before the drain, so they resume.
    ("agent", "restart_drain_timeout"): 10,
    ("agent", "cron_drain_timeout"): 10,
}

# Cost ceilings the upstream merge would otherwise raise; applied only where a
# config does not set them (see reconcile_config). Values = today's defaults.
PIN_IF_MISSING = {
    ("delegation", "max_iterations"): 50,
    ("delegation", "max_concurrent_children"): 3,
    # Upstream v2026.8.31 (c32119b12c) made the agent loop's default UNLIMITED;
    # cron now takes its cap only from agent.max_turns. 90 is the budget
    # auditor.pending's DEFAULT_LIMIT is sized against (CLAUDE.md, "One long
    # agent run starves every other agent").
    ("agent", "max_turns"): 90,
}

# Every Telegram topic bound to a profile in telegram.extra.group_topics gets a matching
# gateway.profile_routes entry (multiplex stage 2b), so the one gateway runs that topic's
# turns in-process under the profile. A bound topic no route matches is dropped
# (TelegramAdapter._drop_unrouted_bound_topic, stage 3 step 3). Routes a human added are
# never touched: only names with ROUTE_NAME_PREFIX are ours. Multiplex itself is upstream's
# default since stage 3 step 4: no pin and no fork opt-out. An explicit false is retired and
# boot rewrites it (normalize_retired_multiplex_false), since upstream does so only when no
# blocker holds.
ROUTE_NAME_PREFIX = "fork-topic:"

CURATED_OPENROUTER_IMAGE_MODEL = "x-ai/grok-imagine-image-quality"
AUDITOR_ORCHESTRATOR_DEFAULT = "deepseek/deepseek-v4-flash-0731"
FALLBACK_MODEL_DEFAULT = "deepseek/deepseek-v4-flash"
OPENROUTER_REQUEST_TIMEOUT = 600
# Tighter for the auditor: every trickled call costs it the whole value (see reconcile_cfg).
AUDITOR_OPENROUTER_REQUEST_TIMEOUT = 420
# OpenRouter providers the auditor never routes to (see reconcile_cfg). auditor/llm.py mirrors it;
# tests/test_auditor_provider_pinning.py keeps the two equal.
AUDITOR_IGNORED_PROVIDERS = ["open-inference"]
GSC_SERVER = {
    "command": "/opt/hermes/.venv/bin/python",
    "args": ["/opt/hermes/optional-mcps/gsc/server.py"],
    "env": {"GSC_SERVICE_ACCOUNT_B64": "${GSC_SERVICE_ACCOUNT_B64}"},
    "timeout": 60,
}


# ── §1: env files ──────────────────────────────────────────────────────────────


def sync_env_file(env_path: Path, environ: Mapping[str, str], exclude=()) -> None:
    """Write every set ``INJECT`` var from ``environ`` into ``env_path``, one line each."""
    content = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    cron_tuning = sorted(k for k in environ if k.startswith(CRON_TUNING_PREFIX))
    for var in [*INJECT, *cron_tuning]:
        if var in exclude:
            continue
        val = environ.get(var, "")
        if not val:
            continue
        line_re = rf"^{re.escape(var)}=.*$"
        matches = re.findall(line_re, content, flags=re.MULTILINE)
        if len(matches) == 1:
            # Single line — replace in place, preserving file position.
            content = re.sub(line_re, lambda _m: f"{var}={val}", content, flags=re.MULTILINE)
        elif len(matches) > 1:
            # DUPLICATE / divergent lines (the GITHUB_TOKEN case: a stale
            # ghp_ PAT plus the valid github_pat_ one). load_dotenv keeps the
            # last occurrence, so leaving duplicates risks loading a revoked
            # token. Strip every occurrence and append exactly one canonical
            # line. Idempotent: on the next boot the single line is stripped
            # and re-appended unchanged.
            content = re.sub(rf"^{re.escape(var)}=.*(?:\n|$)", "", content, flags=re.MULTILINE)
            if content and not content.endswith("\n"):
                content += "\n"
            content += f"{var}={val}\n"
        else:
            sep = "" if (not content or content.endswith("\n")) else "\n"
            content += f"{sep}{var}={val}\n"
    env_path.write_text(content, encoding="utf-8")


def _strip_vars(content: str, names) -> str:
    for name in names:
        content = re.sub(rf"^{name}=.*(?:\n|$)", "", content, flags=re.MULTILINE)
    return content


def _pin_vars(env_path: Path, values: Mapping[str, str]) -> None:
    """Replace every line of each var in ``values`` with exactly one line, appended."""
    content = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    content = _strip_vars(content, values)
    if content and not content.endswith("\n"):
        content += "\n"
    content += "".join(f"{k}={v}\n" for k, v in values.items())
    env_path.write_text(content, encoding="utf-8")


def _resolve(home: Path, environ: Mapping[str, str], var: str) -> str:
    """``var`` from the process env first, then the durable main .env."""
    value = environ.get(var, "")
    if not value and (home / ".env").exists():
        m = re.search(rf"^{var}=(.*)$", (home / ".env").read_text(encoding="utf-8"),
                      flags=re.MULTILINE)
        value = m.group(1).strip() if m else ""
    return value


def is_rented_tenant(env_path: Path) -> bool:
    """True for a bl-site-package rental — a profile that pays its own way.

    BL_SITE_URL is the marker rather than a slug prefix like "bl-": the slug is
    chosen by whoever runs provision_bl_client.py and a client named without
    that prefix would silently fall back to being billed to us. BL_SITE_URL is
    written by the provisioner for every rental and by nothing else.
    """
    if not env_path.exists():
        return False
    return bool(re.search(r"^BL_SITE_URL=", env_path.read_text(encoding="utf-8"),
                          flags=re.MULTILINE))


def has_own_fal_key(env_path: Path) -> bool:
    """True when this profile carries its own non-empty FAL_KEY.

    FAL_KEY is written per-tenant by provision_bl_client.py --fal-key and is
    deliberately NOT in ``INJECT``, so it survives restarts and stays the
    client's own. Its presence is what makes the image_gen override harmful
    rather than helpful: a tenant that can pay for its own images must not be
    rerouted onto our OpenRouter account. ``.+``, not ``=``: a provisioned-but-empty
    key must not count as BYOK, or the tenant is switched to a backend it cannot
    authenticate.
    """
    if not env_path.exists():
        return False
    return bool(re.search(r"^FAL_KEY=.+", env_path.read_text(encoding="utf-8"),
                          flags=re.MULTILINE))


def rental_langfuse_pin(public_key: str, secret_key: str) -> dict:
    """The four Langfuse key lines every rental .env gets, on every boot.

    The rental pair under the names the plugin reads first, and the bare
    LANGFUSE_* names pinned EMPTY. Unset (either half missing) pins all four empty.
    Empty, never absent: a fork profile job's scope is ``{**os.environ, **.env}``,
    so a missing line resolves BigLobster's key from the service env, while
    ``KEY=`` overrides it with "" and the plugin then builds no client.
    """
    if public_key and secret_key:
        return {"HERMES_LANGFUSE_PUBLIC_KEY": public_key, "HERMES_LANGFUSE_SECRET_KEY": secret_key,
                "LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": ""}
    return {name: "" for name in LANGFUSE_KEY_NAMES}


# Service-env keys BigLobster provides to every rental, pinned into its .env by
# name on every boot (and at provisioning), so a rotation in Zeabur reaches every
# rental. A rental reads them today only because a fork profile job's scope merges
# os.environ; a job in the rental's OWN store reads its .env alone (stage 3,
# cohort 3), so without this they vanish on the move:
#   BL_SITE_AUTOMATION_KEY  bl_site_* send it to log in past the site's Turnstile;
#                           without it a Turnstile-protected site refuses every write.
#   TYPESAFE_API_KEY        bl_site_health's boilerplate-description check, billed
#                           to BigLobster. Drop it from this tuple to make rentals
#                           skip that check instead.
# Not INJECT: only rentals use the bl_site tools. Unset in the service env means the
# line is stripped, never left stale.
RENTAL_PASSTHROUGH = ("BL_SITE_AUTOMATION_KEY", "TYPESAFE_API_KEY")


def pin_rental_passthrough(env_path: Path, home: Path, environ: Mapping[str, str]) -> None:
    """Write ``RENTAL_PASSTHROUGH``'s set keys into a rental .env; strip the unset ones."""
    values = {var: _resolve(home, environ, var) for var in RENTAL_PASSTHROUGH}
    _pin_vars(env_path, {var: val for var, val in values.items() if val})
    unset = [var for var, val in values.items() if not val]
    if unset and env_path.exists():
        env_path.write_text(_strip_vars(env_path.read_text(encoding="utf-8"), unset),
                            encoding="utf-8")


def real_profiles(home: Path):
    """Profile dirs, sorted. SOUL.md is the "real profile" marker (same as the s6 reconciler)."""
    root = home / "profiles"
    if not root.is_dir():
        return []
    return [p for p in sorted(p for p in root.iterdir() if p.is_dir()) if (p / "SOUL.md").exists()]


def sync_envs(home: Path, environ: Mapping[str, str]) -> None:
    """§1: shared keys into main + every profile .env, then the auditor's own identity."""
    # Main profile .env
    sync_env_file(home / ".env", environ)
    print(f"[03-biglobster] Synced env vars into {home / '.env'}")

    # 1b. Auditor profile uses its OWN GitHub identity (hermes-auditor), never the
    #     shared agent token — a reviewer must be a distinct identity from the
    #     authors it reviews. Resolved BEFORE the per-profile loop so the loop can
    #     exclude GITHUB_TOKEN/GH_TOKEN from the auditor's .env outright instead of
    #     writing the shared token and hoping a later override catches it.
    #     Invariant: the auditor profile holds ONLY the bot token, NEVER the shared
    #     one — fail closed (no credential at all) if HERMES_AUDITOR_GITHUB_TOKEN is
    #     unset. A silent fallback here previously let the auditor authenticate
    #     `gh`/`git` AS the human account that owns the shared token when the bot
    #     token was transiently missing at boot (found 2026-07-15, biglobster PR
    #     #356's first review posted under the CEO's own GitHub identity).
    auditor_token = _resolve(home, environ, "HERMES_AUDITOR_GITHUB_TOKEN")

    # Dedicated OpenRouter key for the auditor, resolved the same way. Lets the
    # auditor's LLM spend bill to — and be capped by — a SEPARATE OpenRouter key
    # from the shared content fleet, so content agents exhausting the shared key's
    # weekly limit can no longer starve the auditor (the whole auditor cron failed
    # HTTP 402 for >1 day on 2026-09-01 once the shared key hit its cap). OPTIONAL:
    # if unset, the auditor keeps the shared OPENROUTER_API_KEY the §1 loop already
    # wrote — a missing LLM key kills reviews outright, so this falls back for
    # availability rather than failing closed like the GitHub token above.
    auditor_openrouter_key = _resolve(home, environ, "HERMES_AUDITOR_OPENROUTER_API_KEY")

    # 1e. Rental Langfuse pin, resolved once. Both or neither: half a key pair
    #     traces nowhere, so it is treated as unset.
    rental_pk, rental_sk = (_resolve(home, environ, v) for v in RENTAL_LANGFUSE_SOURCE)
    rental_langfuse = rental_langfuse_pin(rental_pk, rental_sk)
    if not (rental_pk and rental_sk):
        print("[03-biglobster] WARNING: HERMES_RENTAL_LANGFUSE_PUBLIC_KEY/SECRET_KEY not "
              "both set — rentals get empty Langfuse keys (tracing off, never "
              "BigLobster's project)")

    # Per-profile .env files — keeps tenant gateways on current keys after a rotation.
    for prof in real_profiles(home):
        prof_env = prof / ".env"
        if prof.name == "auditor":
            exclude, why = AUDITOR_EXCLUDE, ""
        elif is_rented_tenant(prof_env):
            exclude = TENANT_EXCLUDE
            why = (" (BYOK tenant — kept its own OPENROUTER_API_KEY/PEXELS_API_KEY;"
                   " withheld shared EXA_API_KEY/HUGGINGFACE_API_KEY)")
        else:
            exclude, why = (), ""
        sync_env_file(prof_env, environ, exclude=exclude)
        if is_rented_tenant(prof_env):
            # The exclude only stops FUTURE overwrites — a profile that already
            # picked up EXA_API_KEY/HUGGINGFACE_API_KEY from a boot before that
            # fix shipped keeps that stale line forever otherwise (no manifest of
            # already-provisioned tenants exists to backfill by hand; this loop
            # already visits every one of them, every boot).
            content = prof_env.read_text(encoding="utf-8") if prof_env.exists() else ""
            stripped = _strip_vars(content, SHARED_RESEARCH_KEYS)
            if stripped != content:
                prof_env.write_text(stripped, encoding="utf-8")
                print(f"[03-biglobster] Stripped stale shared research keys from {prof_env}")
            _pin_vars(prof_env, rental_langfuse)
            print(f"[03-biglobster] Pinned rental Langfuse keys in {prof_env}")
            pin_rental_passthrough(prof_env, home, environ)
            print(f"[03-biglobster] Pinned rental pass-through keys "
                  f"({', '.join(RENTAL_PASSTHROUGH)}) in {prof_env}")
        print(f"[03-biglobster] Synced env vars into {prof_env}{why}")

    auditor = home / "profiles" / "auditor"
    if not (auditor / "SOUL.md").exists():
        return
    auditor_env = auditor / ".env"
    if auditor_token:
        _pin_vars(auditor_env, {"GITHUB_TOKEN": auditor_token, "GH_TOKEN": auditor_token})
        print("[03-biglobster] Auditor profile pinned to hermes-auditor GitHub identity")
    else:
        # Fail closed: strip any stale GITHUB_TOKEN/GH_TOKEN lines (defense in
        # depth — the loop above already excluded them) so the auditor's gh/git
        # simply have no credential this boot, instead of silently keeping
        # whatever was written before. Loud, because a quiet no-op here is
        # exactly what let the fallback slip by unnoticed before.
        if auditor_env.exists():
            auditor_env.write_text(
                _strip_vars(auditor_env.read_text(encoding="utf-8"), AUDITOR_EXCLUDE),
                encoding="utf-8")
        print("[03-biglobster] WARNING: HERMES_AUDITOR_GITHUB_TOKEN not set — "
              "auditor profile has NO GitHub credential this boot (fail-closed, "
              "will never fall back to the shared token)")

    # 1c. Auditor review-model knobs. Two env vars let the CEO swap the system- and
    #     content-tier review models from Zeabur without a redeploy (auditor/llm.py
    #     reads them at call time). Stamp whatever is set in the container env into
    #     the auditor .env so the cron's agent subprocess inherits them; if unset,
    #     auditor/llm.py falls back to its documented cheap defaults. Auditor-only —
    #     deliberately NOT in the shared INJECT list (other profiles don't review).
    knobs = {}
    for var in ("HERMES_AUDITOR_SYSTEM_MODEL", "HERMES_AUDITOR_CONTENT_MODEL",
                "HERMES_AUDITOR_JUDGE_MAX_TOKENS", "HERMES_AUDITOR_JUDGE_REASONING_EFFORT",
                "HERMES_AUDITOR_JUDGE_DEADLINE_SECONDS"):
        val = environ.get(var, "").strip()
        if val:
            knobs[var] = val
    content = auditor_env.read_text(encoding="utf-8") if auditor_env.exists() else ""
    for var, val in knobs.items():
        content = _strip_vars(content, (var,))
        if content and not content.endswith("\n"):
            content += "\n"
        content += f"{var}={val}\n"
    auditor_env.write_text(content, encoding="utf-8")

    # 1d. Dedicated OpenRouter key override. The loop above wrote the SHARED
    #     OPENROUTER_API_KEY into the auditor .env; a dedicated key replaces it here,
    #     after the loop, so this override always wins.
    if auditor_openrouter_key:
        _pin_vars(auditor_env, {"OPENROUTER_API_KEY": auditor_openrouter_key})
        print("[03-biglobster] Auditor profile pinned to dedicated OpenRouter key")


# ── §2: config.yaml ────────────────────────────────────────────────────────────


def _parse_env_lines(text: str) -> dict:
    vals = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            vals[k.strip()] = v.strip()
    return vals


def reconcile_group_topics(cfg: dict, profiles_src: Path = PROFILES_SRC) -> bool:
    """Ensure inbound topic->profile bindings exist in telegram.extra.group_topics.

    Derived from each bundled profile's routing.env. The live bindings were only
    ever set by hand (via the panel), so a fresh/rebuilt volume would lose all topic
    routing — every message would fall through to the default profile. Rebuilding
    them here makes routing reproducible from the image (DR) and back-fills any
    missing binding on existing volumes. Conservative: only ADDS a chat/topic entry
    or corrects a topic's `profile`; never removes user-defined topics, renames
    existing ones, or touches other telegram config (allowed_chats, reactions,
    token, ...). Returns True if cfg changed.
    """
    if not profiles_src.is_dir():
        return False
    changed = False
    for renv in sorted(profiles_src.glob("*/routing.env")):
        pname = renv.parent.name
        vals = _parse_env_lines(renv.read_text(encoding="utf-8"))
        chat = vals.get("TELEGRAM_HOME_CHANNEL")
        thread = vals.get("TELEGRAM_HOME_CHANNEL_THREAD_ID")
        if not chat or not thread:
            continue
        try:
            thread = int(thread)
        except ValueError:
            continue
        tg = cfg.setdefault("telegram", {})
        if not isinstance(tg, dict):
            return changed
        extra = tg.setdefault("extra", {})
        if not isinstance(extra, dict):
            return changed
        gts = extra.get("group_topics")
        if not isinstance(gts, list):
            gts = []
            extra["group_topics"] = gts
        chat_entry = next(
            (e for e in gts if isinstance(e, dict) and str(e.get("chat_id")) == str(chat)),
            None,
        )
        if chat_entry is None:
            chat_entry = {"chat_id": chat, "topics": []}
            gts.append(chat_entry)
            changed = True
        topics = chat_entry.setdefault("topics", [])
        if not isinstance(topics, list):
            continue
        topic = next(
            (t for t in topics if isinstance(t, dict) and t.get("thread_id") == thread),
            None,
        )
        if topic is None:
            topics.append({"name": pname, "profile": pname, "thread_id": thread})
            changed = True
        elif topic.get("profile") != pname:
            topic["profile"] = pname
            changed = True
    return changed


def topic_routes(cfg: dict, served: set) -> list:
    """One ``profile_routes`` entry per group_topics topic bound to a served profile.

    Keyed on chat_id + thread_id (the topic), sorted so the list is stable across boots.
    A profile upstream does not serve is left out: a route to it makes the gateway drop
    the message (``ProfileRouteRejected``), where the subprocess path still answers.
    """
    tg = cfg.get("telegram")
    extra = tg.get("extra") if isinstance(tg, dict) else None
    chats = extra.get("group_topics") if isinstance(extra, dict) else None
    routes = {}
    for chat in chats if isinstance(chats, list) else []:
        if not isinstance(chat, dict) or chat.get("chat_id") in (None, ""):
            continue
        for topic in chat.get("topics") if isinstance(chat.get("topics"), list) else []:
            if not isinstance(topic, dict):
                continue
            profile, thread = topic.get("profile"), topic.get("thread_id")
            if not profile or thread in (None, "") or profile not in served:
                continue
            chat_id, thread_id = str(chat["chat_id"]), str(thread)
            routes[(chat_id, thread_id)] = {
                "name": f"{ROUTE_NAME_PREFIX}{profile}:{thread_id}",
                "platform": "telegram",
                "chat_id": chat_id,
                "thread_id": thread_id,
                "profile": profile,
            }
    return [routes[k] for k in sorted(routes)]


def _routes_slot(cfg: dict) -> tuple:
    """``(container, key)`` holding the profile_routes upstream actually reads.

    gateway/config_loader.py bridges a top-level ``profile_routes`` whose value is not None
    ahead of ``gateway.profile_routes`` (mode "none", an empty list included), so writing
    the nested key while a top-level one exists would be silently ignored.
    """
    if cfg.get("profile_routes") is not None:
        return cfg, "profile_routes"
    return _section(cfg, "gateway"), "profile_routes"


def normalize_retired_multiplex_false(cfg: dict) -> bool:
    """Rewrite an explicit ``multiplex_profiles: false`` to ``true``; leave every other value.

    Upstream retired ``false``, but rewrites it (``persist_resolved_default``) only when no
    blocker holds; with one, config.yaml keeps ``false`` and the gateway comes up standalone.
    The OVERRIDES pin used to close that on every boot. This does the same for ``false`` only:
    an unset key stays upstream's to decide. Reads both spellings upstream's loader accepts.
    """
    changed = False
    gateway = cfg.get("gateway")
    for container in (cfg, gateway if isinstance(gateway, dict) else None):
        if isinstance(container, dict) and container.get("multiplex_profiles") is False:
            container["multiplex_profiles"] = True
            changed = True
    return changed


def reconcile_profile_routes(cfg: dict, served: Optional[set]) -> bool:
    """Replace our generated routes with the current bound topics; keep every other route.

    ``served`` None (the served set could not be read) leaves the routes untouched rather
    than guessing. Returns True if changed.
    """
    if served is None:
        return False
    container, key = _routes_slot(cfg)
    current = container.get(key)
    current = current if isinstance(current, list) else []
    kept = [r for r in current
            if not (isinstance(r, dict) and str(r.get("name", "")).startswith(ROUTE_NAME_PREFIX))]
    wanted = kept + topic_routes(cfg, served)
    if wanted == current:
        return False
    container[key] = wanted
    return True


def served_profiles() -> Optional[set]:
    """Upstream's served-profile set (``profiles_to_serve``, a pure directory read), or None."""
    try:
        from hermes_cli.profiles import profiles_to_serve

        return {name for name, _home in profiles_to_serve(multiplex=True)}
    except Exception as e:
        print(f"[03-biglobster] Warning: served profiles unreadable ({e}); profile_routes left as is")
        return None


def _section(cfg: dict, name: str) -> dict:
    """``cfg[name]`` as a dict, replacing anything else (None, a scalar) with ``{}``."""
    if not isinstance(cfg.get(name), dict):
        cfg[name] = {}
    return cfg[name]


def reconcile_cfg(
    cfg: dict,
    label: str,
    environ: Mapping[str, str],
    *,
    is_rented: bool = False,
    byok_images: bool = False,
    profiles_src: Path = PROFILES_SRC,
    served: Optional[set] = None,
) -> bool:
    """Apply §2 to one parsed config in place. Returns True when anything changed.

    ``served`` (main only) is the profile set upstream serves; None skips the route step.
    """
    changed = False
    for (section, key), val in OVERRIDES.items():
        # BYOK images: a tenant with its own FAL_KEY keeps the in-tree FAL
        # path. Forcing image_gen.provider on it both broke covers and moved
        # the bill to us — see the block below for the full account.
        if byok_images and (section, key) == ("image_gen", "provider"):
            continue
        sec = _section(cfg, section)
        if sec.get(key) != val:
            sec[key] = val
            changed = True
    # Written only where ABSENT, never forced: pins today's value so the
    # next upstream merge cannot raise it silently, while a profile that
    # chose its own limit keeps it. Upstream v2026.9.x raises the
    # delegation defaults 50 -> 250 iterations and 3 -> 10 parallel
    # children — a 5x cost ceiling on every profile that never set them —
    # and v2026.8.31 already made agent.max_turns unlimited by default.
    for (section, key), val in PIN_IF_MISSING.items():
        sec = _section(cfg, section)
        if key not in sec:
            sec[key] = val
            changed = True
    # Undo what earlier boots imposed. Skipping the override above is not
    # enough on its own: a profile reconciled before this change still has
    # `provider: openrouter` (and the curated openrouter.model) written to
    # disk, and nothing would ever remove it. Clearing the keys returns the
    # tenant to tools/image_generation_tool.py's in-tree FAL path, whose
    # DEFAULT_MODEL (fal-ai/flux-2/klein/9b) is what these agents used
    # before the override reached them.
    if byok_images and isinstance(cfg.get("image_gen"), dict):
        for stale in ("provider", "openrouter"):
            if stale in cfg["image_gen"]:
                del cfg["image_gen"][stale]
                changed = True
        if not cfg["image_gen"]:
            del cfg["image_gen"]
    # Rented tenants: force web research onto the free ddgs backend, never the
    # "web.backend": "exa" override above. Deliberately a MORE SPECIFIC key
    # (search_backend, not backend) — the resolver (agent/web_search_registry.py
    # get_active_search_provider) reads search_backend first and only falls back
    # to backend when it's unset, so this wins without having to touch/remove the
    # generic override. Forces the provider even before the ddgs package is
    # physically installed on a fresh tenant (its own lazy-install fires on first
    # real search — plugins/web/ddgs/provider.py). See issue #174: EXA_API_KEY
    # billed every tenant's web_search calls to BigLobster's own Exa account; §1
    # stops handing tenants the key at all, this is the other half — without it, a
    # tenant with a stale key already on disk (or none at all) would still either
    # bill us or hard-fail with "no provider configured".
    if is_rented:
        web = _section(cfg, "web")
        if web.get("search_backend") != "ddgs":
            web["search_backend"] = "ddgs"
            changed = True
    # Preserve the curated image_gen.openrouter.model only if unset. Never for a
    # BYOK-images tenant: it is an OpenRouter model id, and on that path it would
    # only ever be reached by rerouting the tenant off its own FAL key.
    ig = None if byok_images else cfg.get("image_gen")
    if isinstance(ig, dict):
        ig_or = ig.get("openrouter")
        if not isinstance(ig_or, dict):
            ig_or = {}
        if ig_or.get("model") in (None, ""):
            ig_or["model"] = CURATED_OPENROUTER_IMAGE_MODEL
            ig["openrouter"] = ig_or
            changed = True
    # model.default from HERMES_DEFAULT_MODEL when provided.
    # AUDITOR EXEMPTION: the auditor profile's base model drives a tool-calling
    # ORCHESTRATOR loop (gh / python -m auditor.*), not prose. The free,
    # rate-limited owl-alpha default makes weak tool decisions and derails into
    # long flailing runs (blocked rm/script/auth.json retries) that take ~25min
    # and starve every other cron (agent runs serialize). Pin the auditor to a
    # reliable cheap orchestrator instead — tunable from Zeabur via
    # HERMES_AUDITOR_ORCHESTRATOR_MODEL, mirroring the §1c review-model knobs.
    # The reviewer models (auditor/llm.py, §1c) are separate and unaffected.
    # The default names the DATED slug on purpose: the undated alias
    # `deepseek/deepseek-v4-flash` resolves to the 0423 snapshot on
    # OpenRouter ($0.087/M input) while `-0731` is $0.065/M — 25% cheaper
    # on the input tokens that are ~99% of this orchestrator's bill
    # (tasks/token-optimization.md). Never let the auditor fall back to the
    # undated alias.
    eff_model = environ.get("HERMES_DEFAULT_MODEL", "")
    if label == "auditor":
        eff_model = environ.get(
            "HERMES_AUDITOR_ORCHESTRATOR_MODEL", AUDITOR_ORCHESTRATOR_DEFAULT).strip()
    if eff_model:
        mv = cfg.get("model")
        if isinstance(mv, dict):
            cur = mv.get("default")
        elif isinstance(mv, str):
            cur = mv.strip()
        else:
            cur = None
        if cur != eff_model:
            if isinstance(mv, dict):
                mv["default"] = eff_model
                cfg["model"] = mv
            else:
                cfg["model"] = {"default": eff_model, "provider": "openrouter", "base_url": ""}
            changed = True
    # fallback_model must stay a DIFFERENT model than model.default — a
    # fallback that matches the main model is a no-op retry against the
    # same dead/rate-limited upstream. Incident 2026-07-02: swapping the
    # main model to tencent/hy3-preview also clobbered fallback_model to
    # the same value on 4 profiles (main, biglobster, finview, grow-shop,
    # socialagenda), silently disabling fallback account-wide. Reconciled
    # here like model.default so this can't drift again. Auditor is
    # exempt — OpenRouter's own provider fallback serves it, not this key.
    if label != "auditor":
        fb_model = environ.get("HERMES_FALLBACK_MODEL", FALLBACK_MODEL_DEFAULT).strip()
        if fb_model:
            fbv = cfg.get("fallback_model")
            if not isinstance(fbv, dict):
                fbv = {}
            if fbv.get("model") != fb_model:
                fbv["model"] = fb_model
                fbv["provider"] = "openrouter"
                cfg["fallback_model"] = fbv
                changed = True
    # AUDITOR: route like the rest of the fleet, minus OpenInference.
    # The old `order: ["deepseek"]` pin (2026-07-02, for a warm DeepSeek
    # prompt cache) never took effect: the OpenRouter account refuses
    # providers that train on paid prompts, and DeepSeek's own API does, so
    # routing drops it ("Paid model training violation (account settings)",
    # probed 2026-10-01). DeepSeek served 0 of 3,993 logged calls. Worse, the
    # dead pin put the auditor in a different fallback pool led by
    # OpenInference's fp4 endpoint, which trickles responses for 10-50 min.
    # All 7 OpenInference calls in the fleet were the auditor's, and the
    # unpinned profiles (~3,400 calls, mostly Together/AtlasCloud/Parasail)
    # had no slow calls. So: drop the pin (only the exact value we wrote,
    # never a hand-set order) and ignore OpenInference outright.
    if label == "auditor":
        pr = _section(cfg, "provider_routing")
        if pr.get("order") == ["deepseek"]:
            del pr["order"]
            changed = True
        if pr.get("ignore") != AUDITOR_IGNORED_PROVIDERS:
            pr["ignore"] = list(AUDITOR_IGNORED_PROVIDERS)
            changed = True
    # Bound every OpenRouter request to 600s. Without a per-provider
    # request_timeout_seconds the httpx timeout falls back to
    # HERMES_API_TIMEOUT (1800s), which is LOOSER than the cron inactivity
    # watchdog (1200s) — so a hung non-streaming response was never a
    # retryable SDK timeout, it was always the watchdog killing the whole
    # run ("idle for 1204s ... waiting for non-streaming API response",
    # ~14 times since 2026-08 across auditor-review, gap hunters and the
    # Shoroban jobs). At 600s the request fails first and the agent's own
    # retry gets a second attempt inside the same run. Main + every
    # profile, because a profile job reads ITS config.yaml, not main's.
    # First set by hand on 2026-09-21; reconciled here so a newly
    # provisioned profile gets it too.
    # Since #376 it is also each streaming call's overall deadline, and
    # OpenRouter trickles the auditor's calls often (the first run after
    # that deploy did). The auditor gets 420s: its healthy calls are capped
    # at max_tokens 16000, so even at 40 tok/s they finish inside it, and
    # 8 of its 10 calls ever logged at 420-600s ran under 13 tok/s.
    # Three trickled attempts then end at ~21 min with a real API timeout
    # instead of the 30-min run ceiling.
    want_timeout = (AUDITOR_OPENROUTER_REQUEST_TIMEOUT if label == "auditor"
                    else OPENROUTER_REQUEST_TIMEOUT)
    orc = _section(_section(cfg, "providers"), "openrouter")
    if orc.get("request_timeout_seconds") != want_timeout:
        orc["request_timeout_seconds"] = want_timeout
        changed = True
    # Enable the Langfuse observability plugin when its keys are present.
    # The recorder is OPT-IN via plugins.enabled — the env keys alone do
    # NOT activate it (the #1 source of "I set the keys but see no traces").
    # Append the plugin's registry key (loader also accepts the bare
    # "langfuse") WITHOUT clobbering any other enabled plugins. Idempotent:
    # once present, later boots make no change. Gated on the keys so we
    # never enable a recorder that has nowhere to send traces.
    if environ.get("HERMES_LANGFUSE_PUBLIC_KEY") and environ.get("HERMES_LANGFUSE_SECRET_KEY"):
        plugins_cfg = _section(cfg, "plugins")
        enabled = plugins_cfg.get("enabled")
        if not isinstance(enabled, list):
            enabled = []
        if "observability/langfuse" not in enabled and "langfuse" not in enabled:
            enabled.append("observability/langfuse")
            plugins_cfg["enabled"] = enabled
            changed = True
    # Ensure the read-only GSC MCP server is registered — biglobster
    # profile only. docker/config.yaml ships this block, but that is only
    # the first-boot seed: an existing volume keeps its own config.yaml
    # (which starts as `mcp_servers: {}`), so the image change never reaches
    # the running agent and the SEO/GEO cron silently runs without Search
    # Console data. Scoped to the `biglobster` profile (the biglobster.top
    # website lane): the tool reads biglobster.top data and must NOT leak into
    # the owner (`main`) profile or other tenant profiles. The credential is
    # interpolated from the env at load time (tools/mcp_tool.py), so only the
    # ${...} placeholder is written here, never the key. See optional-mcps/gsc/.
    if label == "biglobster":
        servers = _section(cfg, "mcp_servers")
        if servers.get("gsc") != GSC_SERVER:
            servers["gsc"] = {**GSC_SERVER, "args": list(GSC_SERVER["args"]),
                              "env": dict(GSC_SERVER["env"])}
            changed = True
    # The owner (`main`) profile must NOT carry the GSC server. Volumes
    # provisioned before GSC moved to the biglobster profile still have it
    # written onto main's config.yaml (the old `label == "main"` gate);
    # actively shed it so the owner drops the biglobster.top credential and
    # the tool can't run in the wrong lane. Idempotent on clean volumes.
    if label == "main":
        servers = cfg.get("mcp_servers")
        if isinstance(servers, dict) and "gsc" in servers:
            del servers["gsc"]
            changed = True
        # Inbound topic->profile routing lives only on the main profile's
        # config. Rebuild it from the profiles' routing.env so it survives a
        # volume rebuild (DR) and back-fills missing bindings.
        if reconcile_group_topics(cfg, profiles_src):
            changed = True
        # Stage 3 step 4: no multiplex pin, but a retired explicit `false` is still corrected.
        if normalize_retired_multiplex_false(cfg):
            print("[03-biglobster] Rewrote retired gateway.multiplex_profiles: false to true")
            changed = True
        # Stage 2b: route every bound topic in-process (after group_topics, so a binding
        # rebuilt above is routed the same boot).
        if reconcile_profile_routes(cfg, served):
            changed = True
    return changed


def reconcile_config(
    config_path: Path,
    label: str,
    environ: Mapping[str, str],
    *,
    is_rented: bool = False,
    byok_images: bool = False,
    profiles_src: Path = PROFILES_SRC,
    served: Optional[set] = None,
) -> None:
    """§2 for one config.yaml on disk: rewritten only when something changed, never raises.

    The write goes through ``atomic_config_write``, the one config.yaml writer upstream
    allows (scripts/check_config_yaml_writers.py): atomic, so a pod killed mid-boot
    cannot leave a truncated config, and comment-preserving. The heredoc this replaced
    used a plain ``yaml.dump``; the resulting mapping is the same, keys this function
    deletes included. Importing it lays out the HERMES_HOME skeleton (and seeds a missing
    root SOUL.md) and the writer keeps bounded snapshots in backups/config/ — both exactly
    what the gateway does when it starts a moment later.
    """
    import yaml

    from hermes_cli.config import atomic_config_write

    if not config_path.exists():
        print(f"[03-biglobster] {label}: config.yaml not present yet — skipping")
        return
    try:
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if reconcile_cfg(cfg, label, environ, is_rented=is_rented,
                         byok_images=byok_images, profiles_src=profiles_src, served=served):
            atomic_config_write(config_path, cfg)
            print(f"[03-biglobster] {label}: reconciled config.yaml keys")
        else:
            print(f"[03-biglobster] {label}: config.yaml keys already current")
    except Exception as e:
        print(f"[03-biglobster] Warning: {label} config.yaml reconcile failed: {e}")


def reconcile_configs(home: Path, environ: Mapping[str, str],
                      profiles_src: Path = PROFILES_SRC, served: Optional[set] = None) -> None:
    """§2 for main, then every real profile (after §1, so tenant markers are current).

    ``served`` defaults to upstream's served-profile set, read once for main's routes.
    """
    if served is None:
        served = served_profiles()
    reconcile_config(home / "config.yaml", "main", environ, profiles_src=profiles_src,
                     served=served)
    for prof in real_profiles(home):
        rented = is_rented_tenant(prof / ".env")
        reconcile_config(
            prof / "config.yaml", prof.name, environ,
            is_rented=rented,
            # Both conditions on purpose: a rental WITHOUT its own FAL_KEY has
            # no image backend of its own, so it keeps the OpenRouter override
            # rather than losing image generation entirely.
            byok_images=rented and has_own_fal_key(prof / ".env"),
            profiles_src=profiles_src,
        )


def main(argv: Optional[list] = None) -> int:
    home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    sync_envs(home, os.environ)
    reconcile_configs(home, os.environ)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
