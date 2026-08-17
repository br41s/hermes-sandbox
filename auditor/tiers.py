"""Risk-tiering for the auditor: classify a PR's changed files.

Two tiers drive how hard the auditor reviews and which model it spends:

  * ``system``  — code/config that can break Hermes itself or a profile's runtime.
                  Deep review with the strong paid model; hard gate.
  * ``content`` — docs, website copy, marketing/blog assets. Light review with the
                  cheap model; the existing asset guards already cover the
                  catastrophic case (the cover-wipe).

Policy is **fail-safe toward more review**: a PR is ``system`` if ANY changed file
matches a system pattern, OR if any file matches neither list (unknown => treat as
system). It is ``content`` only when EVERY changed file is recognisably content.
A weak gate that rubber-stamps unknown paths is worse than an over-eager one.

The lists are deliberately path-prefix / suffix based (no real globbing) so the
classification is obvious and cheap to reason about. Tune them in one place here.

SCOPE (Phase 4): the original globs below are tuned for the ``hermes-sandbox``
engine repo and are used when ``classify`` is called with no repo (or with the
engine slug). Profile repos (biglobster, FinView, grow-shop-*, SocialAgenda) are
LIVE production — including the biglobster.top revenue site — so they get a
SEPARATE, deliberately NARROW content allowlist (``_is_content_profile``): prose,
static assets, and a few verified publish dirs are content; everything else is
``system``. A misclassification on a profile repo can therefore only over-review,
never wrong-auto-merge a live site. Add a repo's safe publish dirs to
``_REPO_EXTRA_CONTENT_DIRS`` only after confirming they hold articles/assets, not
templates/build/source.
"""
from __future__ import annotations

from typing import Iterable, List, Optional

# --- system: anything that can change runtime behaviour ---------------------
# Directory prefixes (matched against the repo-relative POSIX path).
_SYSTEM_DIR_PREFIXES = (
    "hermes/",
    "hermes_cli/",
    "cron/",
    "gateway/",
    "tui_gateway/",
    "acp_adapter/",
    "acp_registry/",
    "providers/",
    "plugins/",
    "skills/",
    "tools/",
    "toolsets/",
    "evals/",
    "incidents/",
    "auditor/",
    "agent/",
    "apps/",
    "docker/",
    "scripts/",
    "nix/",
    "packaging/",
    "tests/",
    # Agent-prompt dirs: a cron/agent prompt IS autonomous behaviour, so it
    # warrants the strong reviewer (a bad prompt is the cover-wipe class of
    # risk). The .prompt suffix rule below also catches prompts in any dir.
    "offsite-geo/",
    "infographic/",
)
# Exact root-level files that are system config.
_SYSTEM_ROOT_FILES = (
    "Dockerfile",
    "cloudbuild.yaml",
    "pyproject.toml",
    "setup.py",
    "package.json",
    "package-lock.json",
    "uv.lock",
    "flake.nix",
    "flake.lock",
    "zbpack.json",
    "cli.py",
    "run_agent.py",
    "mcp_serve.py",
    "batch_runner.py",
)
# Root-level filename suffixes / prefixes that are system config.
_SYSTEM_ROOT_PREFIXES = ("docker-compose", "constraints-")
_SYSTEM_ROOT_SUFFIXES = (".py",)  # any top-level *.py is module code
# Suffixes that mark a file system-tier at ANY path depth. A *.prompt file is an
# autonomous agent's instructions — behaviour, not prose — so it always gets the
# strong reviewer regardless of which dir it lives in (offsite-geo, infographic,
# auditor, or a future one). This makes the prior unknown-path fail-safe explicit.
_SYSTEM_ANY_SUFFIXES = (".prompt",)

# --- content: docs, site, copy, assets --------------------------------------
_CONTENT_DIR_PREFIXES = (
    "web/",
    "website/",
    "docs/",
    "assets/",
    "locales/",
    "datagen-config-examples/",
)
_CONTENT_SUFFIXES = (".md", ".mdx", ".txt", ".rst")


def _is_system(path: str) -> bool:
    p = path.strip().lstrip("./")
    if not p:
        return False
    if any(p.startswith(prefix) for prefix in _SYSTEM_DIR_PREFIXES):
        return True
    if any(p.endswith(suf) for suf in _SYSTEM_ANY_SUFFIXES):
        return True
    # root-level file (no slash) checks
    if "/" not in p:
        if p in _SYSTEM_ROOT_FILES:
            return True
        if any(p.startswith(pre) for pre in _SYSTEM_ROOT_PREFIXES):
            return True
        if any(p.endswith(suf) for suf in _SYSTEM_ROOT_SUFFIXES):
            return True
    return False


def _is_content(path: str) -> bool:
    p = path.strip().lstrip("./")
    if not p:
        return False
    if any(p.startswith(prefix) for prefix in _CONTENT_DIR_PREFIXES):
        return True
    if any(p.endswith(suf) for suf in _CONTENT_SUFFIXES):
        return True
    return False


# --- profile-repo tiering (Phase 4) -----------------------------------------
# Profile repos have their own layouts and are live production. Their content
# rule is a NARROW allowlist — prose, static media, and a few explicitly-safe
# publish dirs — and EVERYTHING ELSE is system (advisory, no auto-merge). This is
# intentionally tighter than the hermes ruleset: on a site repo, ``web/`` holds
# core pages + the build, so only the real publish dirs (not all of ``web/``)
# count as content.
_HERMES_REPO = "br41s/hermes-sandbox"

_PROFILE_CONTENT_DIR_PREFIXES = (
    "docs/",
    "assets/",
    "locales/",
)
_PROFILE_CONTENT_SUFFIXES = (
    ".md", ".mdx", ".rst", ".txt",
    # static media — safe to publish without a code review
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif", ".ico",
    ".woff", ".woff2", ".ttf", ".otf",
)
# Per-repo extra publish dirs that are safe content, VERIFIED against the repo's
# layout. biglobster (Eleventy since biglobster#339, 2026-07-06): site/blog/
# holds the Spanish article source files (JSON frontmatter + body HTML, no
# chrome; permalinks under /es/blog/ since the Aug 2026 i18n migration),
# site/en/blog/ is the same shape for English translations (permalinks at
# root /blog/, populated by the Translation Engineer cron), web/blog/images/
# the covers/infographics and web/assets/ the static media — all safe to
# auto-publish. Everything else stays system, INCLUDING the legacy frozen
# web/*.html copies (a write there means the agent is following the
# pre-migration workflow and must be flagged, not merged) and site/_includes/ /
# site/_data/ (layouts and brand data are template/build changes).
_REPO_EXTRA_CONTENT_DIRS = {
    "br41s/biglobster": (
        "site/blog/",
        "site/en/blog/",
        "web/blog/images/",
        "web/assets/",
    ),
}
# Per-repo exact files that are safe content despite living outside the publish
# dirs above. biglobster no longer needs any: blog.html, sitemap.xml and
# feed.xml are GENERATED by the Eleventy build from the posts collection, so a
# PR touching them is either stale-workflow noise or a template change — both
# belong in system tier.
_REPO_EXTRA_CONTENT_FILES: dict = {}
# Files that define autonomous-agent or operational BEHAVIOUR — never content, at
# any depth, on any repo. A bad SOUL/prompt is the cover-wipe class of risk, so it
# always gets the strong reviewer + a human merge.
_SYSTEM_ANY_BASENAMES = frozenset({
    "SOUL.md", "CLAUDE.md", "AGENTS.md", "AGENT_TEMPLATE.md",
    "MISSION.md", "CHARTER.md", "OPS.md", "LIMITS.md", "USER.md", "WORKFLOW.md",
})


def _is_behaviour_file(p: str) -> bool:
    base = p.rsplit("/", 1)[-1]
    return base in _SYSTEM_ANY_BASENAMES or p.endswith(".prompt")


def _is_content_profile(path: str, repo: str) -> bool:
    p = path.strip().lstrip("./")
    if not p:
        return False
    if _is_behaviour_file(p):
        return False
    if any(p.startswith(pre) for pre in _PROFILE_CONTENT_DIR_PREFIXES):
        return True
    if any(p.startswith(pre) for pre in _REPO_EXTRA_CONTENT_DIRS.get(repo, ())):
        return True
    if p in _REPO_EXTRA_CONTENT_FILES.get(repo, ()):
        return True
    if any(p.endswith(suf) for suf in _PROFILE_CONTENT_SUFFIXES):
        return True
    return False


def classify(paths: Iterable[str], repo: Optional[str] = None) -> str:
    """Return ``"system"`` or ``"content"`` for a set of changed file paths.

    Fail-safe: ``system`` wins if any path is system OR unrecognised. ``content``
    only when EVERY changed file is recognisably content. An empty changeset is
    treated as ``content`` (nothing to break).

    ``repo`` (``owner/name``) selects the ruleset: the engine repo (or ``None``)
    uses the hermes globs; any other repo uses the narrow profile allowlist.
    """
    paths = [p for p in (paths or []) if p and p.strip()]
    if not paths:
        return "content"
    if repo and repo != _HERMES_REPO:
        # Profile repo: narrow allowlist, fail-safe to system.
        return "content" if all(_is_content_profile(p, repo) for p in paths) else "system"
    # hermes-sandbox engine repo (or unspecified): the original ruleset.
    for p in paths:
        if _is_system(p):
            return "system"
    # No system path matched — only content if EVERY remaining path is content.
    if all(_is_content(p) for p in paths):
        return "content"
    return "system"  # unknown path(s) => fail safe


def unknown_paths(paths: Iterable[str]) -> List[str]:
    """Paths that match neither list — surfaced so the tiers can be tuned."""
    out = []
    for p in paths or []:
        if not p or not p.strip():
            continue
        if not _is_system(p) and not _is_content(p):
            out.append(p.strip().lstrip("./"))
    return out
