"""Contract test: docker/cont-init.d/03-biglobster-config reliably resolves a
GitHub token and propagates it as BOTH ``GITHUB_TOKEN`` and ``GH_TOKEN``.

Root cause this guards against: in the Zeabur deployment GITHUB_TOKEN is not a
platform-injected process env var — it lives only in ``$HERMES_HOME/.env``,
historically as duplicate, divergent lines (a stale classic ``ghp_…`` PAT plus
the valid fine-grained ``github_pat_…`` one). Because the old boot hook gated
its env sync (§1) and git-credential write (§4) on a non-empty process-env
value, both silently skipped GITHUB_TOKEN every boot: the divergent .env lines
were never deduped and the gateway's load_dotenv (last-occurrence-wins) could
load a revoked token, while no GH_TOKEN was ever produced at all.

The token-resolution preamble is shell, so its tests below are content
assertions on the script text (matching ``test_biglobster_git_credentials.py``):
executing the real cont-init script needs root + s6-setuidgid, neither available
in CI. §1's env sync lives in ``hermes_cli/fork_ext/boot_reconcile.py`` and is
called directly.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from hermes_cli.fork_ext import boot_reconcile as br

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOT_SCRIPT = REPO_ROOT / "docker" / "cont-init.d" / "03-biglobster-config"


@pytest.fixture(scope="module")
def boot_text() -> str:
    if not BOOT_SCRIPT.exists():
        pytest.skip("docker/cont-init.d/03-biglobster-config not present")
    return BOOT_SCRIPT.read_text(encoding="utf-8")


def test_token_resolution_reads_from_env_file(boot_text: str) -> None:
    """When the process env carries no token, the hook sources it from the last
    GITHUB_TOKEN (then GH_TOKEN) line in $HERMES_HOME/.env."""
    assert 'grep -E \'^GITHUB_TOKEN=\' "$HERMES_HOME/.env"' in boot_text
    assert 'grep -E \'^GH_TOKEN=\' "$HERMES_HOME/.env"' in boot_text
    assert "tail -n1" in boot_text


def test_token_resolution_prefers_process_env(boot_text: str) -> None:
    """An explicit process-env GITHUB_TOKEN/GH_TOKEN is authoritative and used
    before falling back to the .env file."""
    # The .env read is guarded on GITHUB_TOKEN already being empty.
    assert 'if [ -z "${GITHUB_TOKEN:-}" ] && [ -f "$HERMES_HOME/.env" ]; then' in boot_text
    # GH_TOKEN in the process env can stand in for a missing GITHUB_TOKEN.
    assert 'GITHUB_TOKEN="${GH_TOKEN:-}"' in boot_text


def test_token_is_exported_under_both_names(boot_text: str) -> None:
    """Both names are exported so §1's python, §4's git config, and the
    gateway/delegate process env (via load_dotenv on the synced .env) agree."""
    assert 'GH_TOKEN="$GITHUB_TOKEN"' in boot_text
    assert "export GITHUB_TOKEN GH_TOKEN" in boot_text


def test_inject_list_includes_both_token_names() -> None:
    """§1 syncs GITHUB_TOKEN and GH_TOKEN into the main and per-profile .env."""
    assert "GITHUB_TOKEN" in br.INJECT
    assert "GH_TOKEN" in br.INJECT


def test_byok_keys_are_withheld_from_rented_tenants() -> None:
    """The bl-shoroban contract: a rented client's own keys are never overwritten.

    A BYOK key must be in `INJECT` (so BigLobster's OWN profiles keep the
    rotation repair §1 exists to provide) AND in the rented-tenant exclude (so
    a boot never overwrites the client value provision_bl_client.py wrote).
    Being in `INJECT` alone is the 2026-07-31 bug: every boot silently billed
    tenant runs to BigLobster.
    """
    for byok in ("OPENROUTER_API_KEY", "PEXELS_API_KEY"):
        assert byok in br.INJECT, f"{byok} must sync to BigLobster's own profiles"
        assert byok in br.TENANT_EXCLUDE, f"{byok} is BYOK and must be withheld from tenants"
    # FAL_KEY is per-client too, but has never been in `INJECT` at all, so it
    # needs no exclusion. Adding it to `INJECT` without the exclude would
    # reintroduce the bug.
    assert "FAL_KEY" not in br.INJECT, "FAL_KEY is BYOK and must stay out of INJECT"


# --- §1's sync_env_file, on real files ----------------------------------------


@pytest.fixture
def sync(tmp_path):
    def _sync(content: str, environ: dict, exclude: tuple[str, ...] = ()) -> str:
        env_file = tmp_path / ".env"
        env_file.write_text(content, encoding="utf-8")
        br.sync_env_file(env_file, environ, exclude=exclude)
        return env_file.read_text(encoding="utf-8")
    return _sync


def test_sync_collapses_divergent_duplicates(sync) -> None:
    """The prod failure mode: two divergent GITHUB_TOKEN lines collapse to one
    canonical line (the valid, last one) and the stale one is removed."""
    prod = (
        "OPENROUTER_API_KEY=sk-or-old\n"
        "GITHUB_TOKEN=ghp_STALE\n"
        "EXA_API_KEY=exa\n"
        "GITHUB_TOKEN=github_pat_VALID\n"
    )
    env = {"GITHUB_TOKEN": "github_pat_VALID", "GH_TOKEN": "github_pat_VALID"}
    out = sync(prod, env)
    assert re.findall(r"^GITHUB_TOKEN=.*$", out, re.MULTILINE) == ["GITHUB_TOKEN=github_pat_VALID"]
    assert re.findall(r"^GH_TOKEN=.*$", out, re.MULTILINE) == ["GH_TOKEN=github_pat_VALID"]
    assert "ghp_STALE" not in out


def test_sync_is_idempotent(sync) -> None:
    env = {"GITHUB_TOKEN": "github_pat_VALID", "GH_TOKEN": "github_pat_VALID"}
    once = sync("GITHUB_TOKEN=ghp_a\nGITHUB_TOKEN=ghp_b\n", env)
    twice = sync(once, env)
    assert once == twice


def test_sync_single_line_preserves_position(sync) -> None:
    """A single existing line is replaced in place — no reordering churn."""
    single = "A=1\nGITHUB_TOKEN=ghp_x\nB=2\n"
    assert sync(single, {"GITHUB_TOKEN": "ghp_x"}) == single


def test_tenant_byok_keys_survive_a_boot_sync(sync) -> None:
    """The bl-shoroban regression, for both BYOK keys at once.

    A rented client's .env carries their own OpenRouter and Pexels keys. A boot
    where BigLobster's process env holds different values must leave both
    untouched — otherwise the client's stock searches and model calls bill to
    us, silently, on every run.
    """
    tenant_env = (
        "BL_SITE_URL=https://client.example\n"
        "BL_SITE_PANEL_PASSWORD=pw\n"
        "OPENROUTER_API_KEY=sk-or-CLIENT\n"
        "PEXELS_API_KEY=pexels-CLIENT\n"
    )
    biglobster_env = {
        "OPENROUTER_API_KEY": "sk-or-BIGLOBSTER",
        "PEXELS_API_KEY": "pexels-BIGLOBSTER",
        "HERMES_CALLBACK_URL": "https://biglobster.top/api/hermes-callback",
    }
    out = sync(tenant_env, biglobster_env, br.TENANT_EXCLUDE)

    assert "sk-or-CLIENT" in out and "sk-or-BIGLOBSTER" not in out
    assert "pexels-CLIENT" in out and "pexels-BIGLOBSTER" not in out
    # Non-BYOK infrastructure values still sync, or tenants drift on the ones
    # BigLobster does own.
    assert "HERMES_CALLBACK_URL=https://biglobster.top/api/hermes-callback" in out


def test_biglobster_own_profile_still_gets_the_pexels_rotation(sync) -> None:
    """A profile that is NOT a rented tenant (no BL_SITE_URL) gets our key
    refreshed, which is why PEXELS_API_KEY stays in `INJECT` at all."""
    out = sync("PEXELS_API_KEY=old-revoked\n", {"PEXELS_API_KEY": "new-live"})
    assert re.findall(r"^PEXELS_API_KEY=.*$", out, re.MULTILINE) == [
        "PEXELS_API_KEY=new-live"
    ]


# --- Rented tenants never see EXA_API_KEY / HUGGINGFACE_API_KEY (#174) -------
#
# Different shape of leak than OPENROUTER_API_KEY/PEXELS_API_KEY above: those
# are BYOK (a tenant supplies their own, must not be clobbered). These two are
# BigLobster's alone — a tenant should never receive either at all. gap-hunter
# and product-articles (both daily SKUs) call web_search, so EXA_API_KEY
# billed every rented client's research to BigLobster's own Exa account until
# this fix. Tenants use the free ddgs backend instead (see the config-
# reconcile tests below); HUGGINGFACE_API_KEY gates video_gen, which no
# rented prompt uses at all.

def test_shared_research_keys_are_withheld_from_rented_tenants() -> None:
    """Unlike the BYOK keys above, EXA_API_KEY/HUGGINGFACE_API_KEY must be
    withheld, full stop — there's no client-supplied replacement to fall
    back to (that's the whole point: tenants use the free ddgs backend, not
    their own Exa key)."""
    for shared in ("EXA_API_KEY", "HUGGINGFACE_API_KEY"):
        assert shared in br.INJECT, f"{shared} must still sync to BigLobster's own profiles"
        assert shared in br.TENANT_EXCLUDE, f"{shared} must be withheld from tenants"


def test_excluded_keys_are_never_injected_into_a_tenant_env(sync) -> None:
    biglobster_env = {"EXA_API_KEY": "exa-BIGLOBSTER", "HUGGINGFACE_API_KEY": "hf-BIGLOBSTER"}
    out = sync("BL_SITE_URL=https://client.example\n", biglobster_env, br.TENANT_EXCLUDE)
    assert "EXA_API_KEY" not in out
    assert "HUGGINGFACE_API_KEY" not in out


def _home_with(tmp_path: Path, profiles: dict[str, str]) -> Path:
    home = tmp_path / "data"
    for name, env in profiles.items():
        prof = home / "profiles" / name
        prof.mkdir(parents=True)
        (prof / "SOUL.md").write_text("soul", encoding="utf-8")
        (prof / ".env").write_text(env, encoding="utf-8")
    return home


def test_stale_research_keys_are_stripped_from_an_already_contaminated_tenant(tmp_path) -> None:
    """The bl-shoroban-class regression, backfilled: a rented tenant .env
    written by a PRE-fix boot already carries BigLobster's keys. There is no
    manifest of already-provisioned tenants to hand-fix (provision_bl_client.py
    never writes these two — only the boot injector ever did), so this must
    self-heal from the SAME per-profile loop that already visits every
    tenant, every boot."""
    home = _home_with(tmp_path, {"bl-client": (
        "BL_SITE_URL=https://client.example\n"
        "OPENROUTER_API_KEY=sk-or-CLIENT\n"
        "EXA_API_KEY=exa-BIGLOBSTER\n"
        "HUGGINGFACE_API_KEY=hf-BIGLOBSTER\n"
    )})
    br.sync_envs(home, {"OPENROUTER_API_KEY": "sk-or-BIGLOBSTER"})
    out = (home / "profiles" / "bl-client" / ".env").read_text(encoding="utf-8")
    assert "EXA_API_KEY" not in out
    assert "HUGGINGFACE_API_KEY" not in out
    # Untouched: the tenant's own key and the site marker survive.
    assert "sk-or-CLIENT" in out and "sk-or-BIGLOBSTER" not in out
    assert "BL_SITE_URL=https://client.example" in out


def test_stale_key_stripping_leaves_our_own_profiles_alone(tmp_path) -> None:
    home = _home_with(tmp_path, {"biglobster": "EXA_API_KEY=a\nOTHER=1\n"})
    br.sync_envs(home, {"EXA_API_KEY": "exa-new"})
    br.sync_envs(home, {"EXA_API_KEY": "exa-new"})
    assert (home / "profiles" / "biglobster" / ".env").read_text(encoding="utf-8") == (
        "EXA_API_KEY=exa-new\nOTHER=1\n")


# --- Rented tenants get web.search_backend: ddgs forced (#174) --------------
#
# A SECOND, more direct leak than the .env injection above: §2's generic
# overrides force web.backend: "exa" onto EVERY profile's config.yaml,
# tenants included. search_backend (more specific) must win over backend in
# the resolver's read order (agent/web_search_registry.py), so forcing it
# per-tenant here is what actually stops a tenant's web_search from landing
# on Exa, not just removing the key from their .env.

def _web(cfg: dict, is_rented: bool) -> bool:
    return br.reconcile_cfg(cfg, "some-profile", {}, is_rented=is_rented)


def test_ddgs_reconcile_on_sample_configs() -> None:
    # Fresh tenant config: section created, ddgs forced, generic override kept.
    cfg: dict = {"model": {"default": "x"}}
    _web(cfg, is_rented=True)
    assert cfg["web"] == {"backend": "exa", "search_backend": "ddgs"}

    # Second boot: no change (idempotent).
    assert _web(cfg, is_rented=True) is False

    # BigLobster's own profile: never gets search_backend.
    own_cfg: dict = {"web": {"backend": "exa"}}
    _web(own_cfg, is_rented=False)
    assert "search_backend" not in own_cfg["web"]

    # A tenant who had manually set something else gets corrected back.
    drifted_cfg: dict = {"web": {"search_backend": "exa", "extract_backend": "exa"}}
    _web(drifted_cfg, is_rented=True)
    assert drifted_cfg["web"]["search_backend"] == "ddgs"
    assert drifted_cfg["web"]["extract_backend"] == "exa"


def test_profile_loop_marks_rentals_by_their_env_marker(tmp_path) -> None:
    """is_rented comes from BL_SITE_URL in the profile's .env, not from its name."""
    import yaml

    home = _home_with(tmp_path, {"client-without-prefix": "BL_SITE_URL=https://c\n",
                                 "biglobster": "A=1\n"})
    for name in ("client-without-prefix", "biglobster"):
        (home / "profiles" / name / "config.yaml").write_text("{}\n", encoding="utf-8")
    br.reconcile_configs(home, {}, profiles_src=tmp_path / "none")

    def web(name):
        return yaml.safe_load((home / "profiles" / name / "config.yaml").read_text())["web"]

    assert web("client-without-prefix").get("search_backend") == "ddgs"
    assert "search_backend" not in web("biglobster")


# --- stage 3 step 0c: Langfuse split, cron tuning, auditor judge knobs --------
# ops/multiplex-stage3-plan.md, section 3 ("Rental Langfuse"). Sentinels only, and
# every assertion compares booleans or sentinels, so a failure prints no real value.

BL_PK, BL_SK = "pk-lf-BIGLOBSTER", "sk-lf-BIGLOBSTER"
RENTAL_PK, RENTAL_SK = "pk-lf-RENTAL", "sk-lf-RENTAL"
BL_VALUES = {BL_PK, BL_SK}


def test_inject_never_carries_a_forbidden_key() -> None:
    """INJECT reaches every profile's .env, so these must never be in it."""
    for name in br.INJECT:
        assert name != "TELEGRAM_BOT_TOKEN", "a satellite holding the bot token is a duplicate credential"
        assert not (name.startswith("TELEGRAM_") and "ALLOWED" in name), name
        assert name != "SHORTS_STUDIO_GITHUB_TOKEN", name
        assert not name.startswith(("YOUTUBE_", "META_")), name
        assert not name.startswith("HERMES_RENTAL_LANGFUSE_"), name
        assert not name.startswith(br.CRON_TUNING_PREFIX), "cron tuning syncs by prefix, not by list"


def test_biglobster_langfuse_keys_sync_to_our_profiles_but_never_to_rentals() -> None:
    for key in ("HERMES_LANGFUSE_PUBLIC_KEY", "HERMES_LANGFUSE_SECRET_KEY"):
        assert key in br.INJECT, f"{key} must reach BigLobster's own profiles"
        assert key in br.TENANT_EXCLUDE, f"{key} is BigLobster's project and must never reach a rental"


def _boot_home(tmp_path: Path, rental_env: str = "BL_SITE_URL=https://client.example\n") -> Path:
    return _home_with(tmp_path, {"rental": rental_env, "biglobster": "A=1\n"})


def _service_env(*, rental: bool) -> dict:
    env = {"HERMES_LANGFUSE_PUBLIC_KEY": BL_PK, "HERMES_LANGFUSE_SECRET_KEY": BL_SK,
           "LANGFUSE_PUBLIC_KEY": BL_PK, "LANGFUSE_SECRET_KEY": BL_SK,
           "HERMES_LANGFUSE_BASE_URL": "https://lf.example"}
    if rental:
        env.update(HERMES_RENTAL_LANGFUSE_PUBLIC_KEY=RENTAL_PK, HERMES_RENTAL_LANGFUSE_SECRET_KEY=RENTAL_SK)
    return env


def _resolved_in_rental_run(home: Path, service_env: dict, monkeypatch) -> dict:
    """What get_secret returns inside the rental's cron profile-run scope (fork store)."""
    from agent.secret_scope import get_secret
    from cron.fork_ext.profile_scope import _job_profile_context

    monkeypatch.setenv("HERMES_HOME", str(home))
    for name, value in service_env.items():  # the scope carries the process env
        monkeypatch.setenv(name, value)
    with _job_profile_context("job-0c", "rental"):
        return {name: get_secret(name) for name in br.LANGFUSE_KEY_NAMES}


def _resolved_in_satellite_scope(home: Path) -> dict:
    """The same names under the rental's own store scope (.env only), after its move."""
    from agent.secret_scope import build_profile_secret_scope

    scope = build_profile_secret_scope(home / "profiles" / "rental")
    return {name: scope.get(name) for name in br.LANGFUSE_KEY_NAMES}


@pytest.mark.parametrize("case", ["both_set", "rental_unset", "already_contaminated"])
def test_a_rental_never_resolves_biglobsters_langfuse_keys(tmp_path, monkeypatch, case) -> None:
    rental_env = "BL_SITE_URL=https://client.example\n"
    if case == "already_contaminated":  # a .env written by a boot before 0c
        rental_env += f"HERMES_LANGFUSE_PUBLIC_KEY={BL_PK}\nHERMES_LANGFUSE_SECRET_KEY={BL_SK}\n"
    home = _boot_home(tmp_path, rental_env)
    service_env = _service_env(rental=(case != "rental_unset"))
    br.sync_envs(home, service_env)

    for resolved in (_resolved_in_rental_run(home, service_env, monkeypatch),
                     _resolved_in_satellite_scope(home)):
        assert not (set(resolved.values()) & BL_VALUES), f"{case}: a BigLobster key reached the rental"
        assert resolved["LANGFUSE_PUBLIC_KEY"] == "" and resolved["LANGFUSE_SECRET_KEY"] == ""
        if case == "rental_unset":
            assert resolved["HERMES_LANGFUSE_PUBLIC_KEY"] == "" and resolved["HERMES_LANGFUSE_SECRET_KEY"] == ""
        else:
            assert resolved["HERMES_LANGFUSE_PUBLIC_KEY"] == RENTAL_PK
            assert resolved["HERMES_LANGFUSE_SECRET_KEY"] == RENTAL_SK


def test_a_rotated_rental_key_replaces_the_old_one(tmp_path) -> None:
    home = _boot_home(tmp_path)
    br.sync_envs(home, _service_env(rental=True))
    rotated = {**_service_env(rental=True), "HERMES_RENTAL_LANGFUSE_SECRET_KEY": "sk-lf-ROTATED"}
    br.sync_envs(home, rotated)

    text = (home / "profiles" / "rental" / ".env").read_text(encoding="utf-8")
    secret_lines = re.findall(r"^HERMES_LANGFUSE_SECRET_KEY=.*$", text, re.MULTILINE)
    assert secret_lines == ["HERMES_LANGFUSE_SECRET_KEY=sk-lf-ROTATED"]
    assert len(re.findall(r"^HERMES_LANGFUSE_PUBLIC_KEY=", text, re.MULTILINE)) == 1


def test_our_own_profiles_get_biglobsters_langfuse_and_base_url(tmp_path) -> None:
    home = _boot_home(tmp_path)
    br.sync_envs(home, _service_env(rental=True))
    text = (home / "profiles" / "biglobster" / ".env").read_text(encoding="utf-8")
    assert f"HERMES_LANGFUSE_PUBLIC_KEY={BL_PK}" in text
    assert "HERMES_LANGFUSE_BASE_URL=https://lf.example" in text
    assert "RENTAL" not in text
    # The base URL is not a secret: rentals need it too, or they trace to the cloud default.
    assert "HERMES_LANGFUSE_BASE_URL=https://lf.example" in (
        home / "profiles" / "rental" / ".env").read_text(encoding="utf-8")


def test_cron_tuning_in_the_service_env_reaches_every_profile(tmp_path) -> None:
    home = _boot_home(tmp_path)
    br.sync_envs(home, {"HERMES_CRON_TIMEOUT": "900", "HERMES_CRON_MAX_RUNTIME": "1500", "OTHER": "x"})
    for name in ("rental", "biglobster"):
        text = (home / "profiles" / name / ".env").read_text(encoding="utf-8")
        assert "HERMES_CRON_TIMEOUT=900" in text and "HERMES_CRON_MAX_RUNTIME=1500" in text
        assert "OTHER=" not in text


def test_auditor_judge_knobs_are_stamped_into_the_auditor_env(tmp_path) -> None:
    home = _home_with(tmp_path, {"auditor": "A=1\n", "biglobster": "A=1\n"})
    br.sync_envs(home, {"HERMES_AUDITOR_JUDGE_MAX_TOKENS": "4000",
                        "HERMES_AUDITOR_JUDGE_DEADLINE_SECONDS": "300"})
    auditor = (home / "profiles" / "auditor" / ".env").read_text(encoding="utf-8")
    assert "HERMES_AUDITOR_JUDGE_MAX_TOKENS=4000" in auditor
    assert "HERMES_AUDITOR_JUDGE_DEADLINE_SECONDS=300" in auditor
    assert "HERMES_AUDITOR_JUDGE" not in (home / "profiles" / "biglobster" / ".env").read_text(encoding="utf-8")
