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

These are content assertions on the script text (matching
``test_biglobster_git_credentials.py``): executing the real cont-init script
needs root + s6-setuidgid, neither available in CI. The dedupe semantics of the
embedded ``_sync_env_file`` are additionally exercised functionally below by
replicating the function in-process.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

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


def _parse_inject(boot_text: str) -> list[str]:
    """Read §1's ``inject`` allowlist out of the boot script."""
    match = re.search(r"inject = \[(.*?)\n\]", boot_text, re.DOTALL)
    assert match, "inject list not found"
    body = "\n".join(
        line for line in match.group(1).splitlines()
        if not line.strip().startswith("#")
    )
    return re.findall(r'"([^"]+)"', body)


def test_inject_list_includes_both_token_names(boot_text: str) -> None:
    """§1 syncs GITHUB_TOKEN and GH_TOKEN into the main and per-profile .env."""
    inject = _parse_inject(boot_text)
    assert "GITHUB_TOKEN" in inject
    assert "GH_TOKEN" in inject


def _parse_tenant_exclude(boot_text: str) -> list[str]:
    """Read the keys held back from a rented tenant's per-profile sync."""
    match = re.search(
        r'_is_rented_tenant\(_prof_env\):.*?_exclude = \(([^)]*)\)',
        boot_text,
        re.DOTALL,
    )
    assert match, "rented-tenant _exclude tuple not found"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_byok_keys_are_withheld_from_rented_tenants(boot_text: str) -> None:
    """The bl-shoroban contract: a rented client's own keys are never overwritten.

    A BYOK key must be in `inject` (so BigLobster's OWN profiles keep the
    rotation repair §1 exists to provide) AND in the rented-tenant exclude (so
    a boot never overwrites the client value provision_bl_client.py wrote).
    Being in `inject` alone is the 2026-07-31 bug: every boot silently billed
    tenant runs to BigLobster.
    """
    inject = _parse_inject(boot_text)
    exclude = _parse_tenant_exclude(boot_text)
    for byok in ("OPENROUTER_API_KEY", "PEXELS_API_KEY"):
        assert byok in inject, f"{byok} must sync to BigLobster's own profiles"
        assert byok in exclude, f"{byok} is BYOK and must be withheld from tenants"
    # FAL_KEY is per-client too, but has never been in `inject` at all, so it
    # needs no exclusion. Adding it to `inject` without the exclude would
    # reintroduce the bug.
    assert "FAL_KEY" not in inject, "FAL_KEY is BYOK and must stay out of inject"


# --- functional check of the embedded _sync_env_file dedupe semantics --------
# The allowlist is READ from the boot script rather than copied, because a
# hand-maintained copy drifts: GSC_SERVICE_ACCOUNT_B64 was added to the script
# and never mirrored here. Only the function body below is a replica; if the
# §1 heredoc's _sync_env_file changes, update it here.
def _sync_env_file_content(
    content: str, environ: dict, inject: list[str], exclude: tuple[str, ...] = ()
) -> str:
    for var in inject:
        if var in exclude:
            continue
        val = environ.get(var, "")
        if not val:
            continue
        line_re = rf"^{re.escape(var)}=.*$"
        matches = re.findall(line_re, content, flags=re.MULTILINE)
        if len(matches) == 1:
            content = re.sub(line_re, lambda _m: f"{var}={val}", content, flags=re.MULTILINE)
        elif len(matches) > 1:
            content = re.sub(rf"^{re.escape(var)}=.*(?:\n|$)", "", content, flags=re.MULTILINE)
            if content and not content.endswith("\n"):
                content += "\n"
            content += f"{var}={val}\n"
        else:
            sep = "" if (not content or content.endswith("\n")) else "\n"
            content += f"{sep}{var}={val}\n"
    return content


@pytest.fixture(scope="module")
def inject(boot_text: str) -> list[str]:
    return _parse_inject(boot_text)


def test_sync_collapses_divergent_duplicates(inject: list[str]) -> None:
    """The prod failure mode: two divergent GITHUB_TOKEN lines collapse to one
    canonical line (the valid, last one) and the stale one is removed."""
    prod = (
        "OPENROUTER_API_KEY=sk-or-old\n"
        "GITHUB_TOKEN=ghp_STALE\n"
        "EXA_API_KEY=exa\n"
        "GITHUB_TOKEN=github_pat_VALID\n"
    )
    env = {"GITHUB_TOKEN": "github_pat_VALID", "GH_TOKEN": "github_pat_VALID"}
    out = _sync_env_file_content(prod, env, inject)
    assert re.findall(r"^GITHUB_TOKEN=.*$", out, re.MULTILINE) == ["GITHUB_TOKEN=github_pat_VALID"]
    assert re.findall(r"^GH_TOKEN=.*$", out, re.MULTILINE) == ["GH_TOKEN=github_pat_VALID"]
    assert "ghp_STALE" not in out


def test_sync_is_idempotent(inject: list[str]) -> None:
    env = {"GITHUB_TOKEN": "github_pat_VALID", "GH_TOKEN": "github_pat_VALID"}
    once = _sync_env_file_content("GITHUB_TOKEN=ghp_a\nGITHUB_TOKEN=ghp_b\n", env, inject)
    twice = _sync_env_file_content(once, env, inject)
    assert once == twice


def test_sync_single_line_preserves_position(inject: list[str]) -> None:
    """A single existing line is replaced in place — no reordering churn."""
    single = "A=1\nGITHUB_TOKEN=ghp_x\nB=2\n"
    out = _sync_env_file_content(single, {"GITHUB_TOKEN": "ghp_x"}, inject)
    assert out == single


@pytest.fixture(scope="module")
def tenant_exclude(boot_text: str) -> tuple[str, ...]:
    return tuple(_parse_tenant_exclude(boot_text))


def test_tenant_byok_keys_survive_a_boot_sync(
    inject: list[str], tenant_exclude: tuple[str, ...]
) -> None:
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
    out = _sync_env_file_content(tenant_env, biglobster_env, inject, tenant_exclude)

    assert "sk-or-CLIENT" in out and "sk-or-BIGLOBSTER" not in out
    assert "pexels-CLIENT" in out and "pexels-BIGLOBSTER" not in out
    # Non-BYOK infrastructure values still sync, or tenants drift on the ones
    # BigLobster does own.
    assert "HERMES_CALLBACK_URL=https://biglobster.top/api/hermes-callback" in out


def test_biglobster_own_profile_still_gets_the_pexels_rotation(inject: list[str]) -> None:
    """A profile that is NOT a rented tenant (no BL_SITE_URL) gets our key
    refreshed, which is why PEXELS_API_KEY stays in `inject` at all."""
    out = _sync_env_file_content(
        "PEXELS_API_KEY=old-revoked\n", {"PEXELS_API_KEY": "new-live"}, inject
    )
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

def test_shared_research_keys_are_withheld_from_rented_tenants(
    inject: list[str], tenant_exclude: tuple[str, ...]
) -> None:
    """Unlike the BYOK keys above, EXA_API_KEY/HUGGINGFACE_API_KEY must be
    withheld, full stop — there's no client-supplied replacement to fall
    back to (that's the whole point: tenants use the free ddgs backend, not
    their own Exa key)."""
    for shared in ("EXA_API_KEY", "HUGGINGFACE_API_KEY"):
        assert shared in inject, f"{shared} must still sync to BigLobster's own profiles"
        assert shared in tenant_exclude, f"{shared} must be withheld from tenants"


def test_excluded_keys_are_never_injected_into_a_tenant_env(
    inject: list[str], tenant_exclude: tuple[str, ...]
) -> None:
    tenant_env = "BL_SITE_URL=https://client.example\n"
    biglobster_env = {"EXA_API_KEY": "exa-BIGLOBSTER", "HUGGINGFACE_API_KEY": "hf-BIGLOBSTER"}
    out = _sync_env_file_content(tenant_env, biglobster_env, inject, tenant_exclude)
    assert "EXA_API_KEY" not in out
    assert "HUGGINGFACE_API_KEY" not in out


def _strip_stale_research_keys(content: str) -> str:
    """Replicates the defensive stripping added alongside _exclude — a
    profile that already picked up EXA_API_KEY/HUGGINGFACE_API_KEY from a
    boot before this fix shipped keeps the stale line forever otherwise,
    since exclude only stops future overwrites."""
    stripped = content
    for var in ("EXA_API_KEY", "HUGGINGFACE_API_KEY"):
        stripped = re.sub(rf"^{var}=.*(?:\n|$)", "", stripped, flags=re.MULTILINE)
    return stripped


def test_stale_research_keys_are_stripped_from_an_already_contaminated_tenant() -> None:
    """The bl-shoroban-class regression, backfilled: a rented tenant .env
    written by a PRE-fix boot already carries BigLobster's keys. There is no
    manifest of already-provisioned tenants to hand-fix (provision_bl_client.py
    never writes these two — only the boot injector ever did), so this must
    self-heal from the SAME per-profile loop that already visits every
    tenant, every boot."""
    contaminated = (
        "BL_SITE_URL=https://client.example\n"
        "OPENROUTER_API_KEY=sk-or-CLIENT\n"
        "EXA_API_KEY=exa-BIGLOBSTER\n"
        "HUGGINGFACE_API_KEY=hf-BIGLOBSTER\n"
    )
    out = _strip_stale_research_keys(contaminated)
    assert "EXA_API_KEY" not in out
    assert "HUGGINGFACE_API_KEY" not in out
    # Untouched: the tenant's own key and the site marker survive.
    assert "sk-or-CLIENT" in out
    assert "BL_SITE_URL=https://client.example" in out


def test_stale_key_stripping_is_idempotent() -> None:
    once = _strip_stale_research_keys("EXA_API_KEY=a\nOTHER=1\n")
    twice = _strip_stale_research_keys(once)
    assert once == twice == "OTHER=1\n"


# --- Rented tenants get web.search_backend: ddgs forced (#174) --------------
#
# A SECOND, more direct leak than the .env injection above: §2's generic
# overrides force web.backend: "exa" onto EVERY profile's config.yaml,
# tenants included. search_backend (more specific) must win over backend in
# the resolver's read order (agent/web_search_registry.py), so forcing it
# per-tenant here is what actually stops a tenant's web_search from landing
# on Exa, not just removing the key from their .env.

def test_reconcile_config_accepts_is_rented_flag(boot_text: str) -> None:
    assert "def _reconcile_config(config_path, label, is_rented=False):" in boot_text


def test_is_rented_forces_ddgs_search_backend_not_generic_web_backend(boot_text: str) -> None:
    idx = boot_text.index('cfg["web"]["search_backend"] = "ddgs"')
    gate = boot_text.rindex("if is_rented:", 0, idx)
    between = boot_text[gate:idx]
    # Must sit inside the is_rented branch, and must be search_backend, never
    # touching/overriding the generic ("web", "backend"): "exa" override.
    assert 'cfg["web"].get("search_backend") != "ddgs"' in between
    assert '"backend"' not in between


def test_profile_loop_passes_is_rented_from_the_env_marker(boot_text: str) -> None:
    assert "is_rented=_is_rented_tenant(prof / \".env\")" in boot_text


def test_ddgs_reconcile_logic_on_sample_configs() -> None:
    """Functionally replay the is_rented branch of _reconcile_config."""
    def reconcile_web_backend(cfg: dict, is_rented: bool) -> bool:
        changed = False
        if is_rented:
            if not isinstance(cfg.get("web"), dict):
                cfg["web"] = {}
            if cfg["web"].get("search_backend") != "ddgs":
                cfg["web"]["search_backend"] = "ddgs"
                changed = True
        return changed

    # Fresh tenant config: section created, ddgs forced.
    cfg: dict = {"model": {"default": "x"}}
    assert reconcile_web_backend(cfg, is_rented=True) is True
    assert cfg["web"]["search_backend"] == "ddgs"

    # Second boot: no change (idempotent).
    assert reconcile_web_backend(cfg, is_rented=True) is False

    # BigLobster's own profile: never touched.
    own_cfg: dict = {"web": {"backend": "exa"}}
    assert reconcile_web_backend(own_cfg, is_rented=False) is False
    assert "search_backend" not in own_cfg["web"]

    # A tenant who had manually set something else gets corrected back.
    drifted_cfg: dict = {"web": {"search_backend": "exa", "extract_backend": "exa"}}
    assert reconcile_web_backend(drifted_cfg, is_rented=True) is True
    assert drifted_cfg["web"] == {"search_backend": "ddgs", "extract_backend": "exa"}
