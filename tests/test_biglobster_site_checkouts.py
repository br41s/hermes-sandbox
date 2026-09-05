"""Contract test: docker/cont-init.d/03-biglobster-config section 6b provisions
ISOLATED BigLobster site checkouts — one per consuming cron job — so the SEO/GEO
and Content Gap Hunter jobs never share a git working tree.

The two jobs used to commit to one shared clone, which collided on branch AND
git identity: Gap Hunter left the tree on a blog/<slug> feature branch authored
as "BigLobster <biglobster@biglobster.top>", so the SEO agent's commits landed
on the wrong branch as the wrong author — never reached main and later tripped
the "human edit" ledger detector (author != hermes@agent.local). Section 6b
gives each job its own clone under checkouts/, each with a locally pinned
hermes@agent.local identity.

Like tests/test_biglobster_git_credentials.py, executing the real cont-init
script needs root + s6-setuidgid + git (none available in CI), so we assert the
section's invariants on the script text instead.
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


def _section6b(boot_text: str) -> str:
    """Return just the section-6b block so assertions can't accidentally match
    the section-6 per-profile repo-sync plumbing."""
    start = boot_text.index("--- 6b:")
    end = boot_text.index("--- 7:")
    return boot_text[start:end]


def test_section_6b_present(boot_text: str) -> None:
    assert "--- 6b:" in boot_text


def test_clones_the_biglobster_site_repo(boot_text: str) -> None:
    s = _section6b(boot_text)
    assert 'BIGLOBSTER_SITE_REPO="br41s/biglobster"' in s


def test_isolated_checkout_per_consuming_job(boot_text: str) -> None:
    """One checkout per consuming cron job, all under checkouts/.

    Asserted per-name against the loop line rather than as one exact string:
    the previous version pinned the whole `for ckdir in ...; do` line, so
    adding the Infographic Engineer checkout (db554111b, #61) silently rotted
    this test instead of failing the invariant it actually guards.
    """
    s = _section6b(boot_text)
    assert 'checkouts_root="$HERMES_HOME/checkouts"' in s
    loop = next(
        (ln for ln in s.splitlines() if ln.lstrip().startswith("for ckdir in")), ""
    )
    assert loop, "section 6b has no `for ckdir in ...` checkout loop"
    for job in ("biglobster-seo", "biglobster-gaphunter", "biglobster-infographic"):
        assert job in loop, f"{job} has no isolated checkout in section 6b"


def test_identity_pinned_locally_per_checkout(boot_text: str) -> None:
    """Identity is pinned with `git -C <target> config` (LOCAL, no --global) so
    it can never be clobbered by a sibling job or the global .gitconfig."""
    s = _section6b(boot_text)
    assert 'git -C "$target" config user.email "hermes@agent.local"' in s
    assert 'git -C "$target" config user.name "Hermes Agent"' in s
    # Must be local config, not a --global write that would be shared.
    assert "config --global user.email" not in s


def test_remote_normalized_tokenless(boot_text: str) -> None:
    """Tokenless remote so the agent never sees a redacted token in its URL and
    detours to SSH (the recurring profile-lane failure)."""
    s = _section6b(boot_text)
    assert 'remote set-url origin' in s
    assert '"https://github.com/$BIGLOBSTER_SITE_REPO.git"' in s


def test_ensures_main_without_hard_reset(boot_text: str) -> None:
    """Each boot returns the tree to main and fast-forwards, but never hard-
    resets — untracked artifacts (a mid-run cover image) must survive."""
    s = _section6b(boot_text)
    assert 'checkout --quiet main' in s
    assert "pull --ff-only" in s
    assert "reset --hard" not in s


def test_git_calls_bounded_by_timeout(boot_text: str) -> None:
    """Boot is never blocked by a stalled clone/fetch/pull (cont-init runs
    before s6 starts the dashboard)."""
    s = _section6b(boot_text)
    assert "timeout 300 git clone" in s
    assert "timeout 120 git -C" in s


def test_never_fatal(boot_text: str) -> None:
    """Every git operation degrades to a warning so a non-zero exit can never
    abort container boot."""
    s = _section6b(boot_text)
    assert "(non-fatal)" in s


def test_writes_run_as_hermes(boot_text: str) -> None:
    """Checkouts must land hermes-owned, like the rest of the script."""
    s = _section6b(boot_text)
    assert "as_hermes timeout 300 git clone" in s


def test_dirty_tracked_file_is_self_healed_only_when_not_ahead(boot_text: str) -> None:
    """Regression lock for the 2026-09-05 incident: all four checkouts sat
    dirty (SOUL.md diverged from a real upstream commit synced in without
    advancing the branch pointer) and silently blocked `pull --ff-only` for a
    month. A dirty tracked file with no local commits at risk must now be
    auto-discarded via `checkout -- .` (never `reset --hard`, which would also
    swallow real unmerged commits) before the fast-forward is attempted."""
    s = _section6b(boot_text)
    assert 'git -C "$target" status --porcelain' in s
    assert 'rev-list --count HEAD "^origin/main"' in s
    assert 'checkout --quiet -- .' in s
    assert "reset --hard" not in s


def test_diverged_checkout_is_reported_not_guessed(boot_text: str) -> None:
    """A checkout that is BOTH dirty AND ahead of origin/main might hold real
    unmerged work (confirmed: biglobster-seo carried 4 real commits during the
    2026-09-05 incident). That case must never be auto-discarded — only
    reported, via the same jsonl-signal convention the git-guard pre-commit
    hook uses, so the hourly incident watcher (incidents/sweep.py) surfaces it
    instead of it going silent for weeks."""
    s = _section6b(boot_text)
    assert "checkout-drift.jsonl" in s
    assert '"checkout":"%s"' in s
    assert '"ahead":%s' in s
    assert 'as_hermes mkdir -p "$checkouts_root"' in s
