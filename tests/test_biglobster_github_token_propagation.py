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


def test_inject_list_carries_the_shared_not_byok_keys(boot_text: str) -> None:
    """Keys BigLobster owns for the whole fleet must ride the per-profile sync.

    A shared key that is missing here survives its own rotation only in the
    main .env; every already-provisioned profile keeps serving the revoked
    value, which is what broke grow-shop in the 2026-06-05 rotation. FAL_KEY
    is deliberately absent — it is genuinely per-client BYOK, and syncing it
    would overwrite each client's own key with BigLobster's.
    """
    inject = _parse_inject(boot_text)
    for shared in ("OPENROUTER_API_KEY", "EXA_API_KEY", "PEXELS_API_KEY"):
        assert shared in inject, f"{shared} is shared and must be synced per profile"
    assert "FAL_KEY" not in inject, "FAL_KEY is BYOK and must stay per-profile"


# --- functional check of the embedded _sync_env_file dedupe semantics --------
# The allowlist is READ from the boot script rather than copied, because a
# hand-maintained copy drifts: GSC_SERVICE_ACCOUNT_B64 was added to the script
# and never mirrored here. Only the function body below is a replica; if the
# §1 heredoc's _sync_env_file changes, update it here.
def _sync_env_file_content(content: str, environ: dict, inject: list[str]) -> str:
    for var in inject:
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


def test_sync_adds_pexels_key_to_a_profile_that_lacks_it(inject: list[str]) -> None:
    """A profile provisioned before the shorts agent existed must pick the key
    up on the next boot, appended without disturbing its existing lines."""
    profile_env = (
        "BL_SITE_URL=https://client.example\n"
        "BL_SITE_PANEL_PASSWORD=pw\n"
        "OPENROUTER_API_KEY=sk-or-client\n"
    )
    out = _sync_env_file_content(
        profile_env, {"PEXELS_API_KEY": "pexels-shared"}, inject
    )
    assert "PEXELS_API_KEY=pexels-shared\n" in out
    assert out.startswith(profile_env)


def test_sync_replaces_a_rotated_pexels_key(inject: list[str]) -> None:
    """The rotation case: an old value is overwritten, not left alongside."""
    out = _sync_env_file_content(
        "PEXELS_API_KEY=old-revoked\n", {"PEXELS_API_KEY": "new-live"}, inject
    )
    assert re.findall(r"^PEXELS_API_KEY=.*$", out, re.MULTILINE) == [
        "PEXELS_API_KEY=new-live"
    ]
