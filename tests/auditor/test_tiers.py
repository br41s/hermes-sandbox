"""Regression lock for auditor risk-tiering (auditor/tiers.py).

The gate's depth-of-review hinges on this. The load-bearing property is the
fail-safe: anything system OR unrecognised must classify as ``system`` — a weak
gate that rubber-stamps unknown paths is worse than an over-eager one.
"""
from auditor.tiers import classify, unknown_paths


def test_system_code_paths_are_system():
    for p in [
        "hermes/agent.py",
        "cron/scheduler.py",
        "gateway/poller.py",
        "docker/cont-init.d/03-biglobster-config",
        "scripts/git-guard/pre-commit",
        "tools/cronjob_tools.py",
        "evals/run.py",
        "tests/test_x.py",
    ]:
        assert classify([p]) == "system", p


def test_root_files_and_modules_are_system():
    assert classify(["cli.py"]) == "system"
    assert classify(["pyproject.toml"]) == "system"
    assert classify(["Dockerfile"]) == "system"
    assert classify(["docker-compose.windows.yml"]) == "system"
    assert classify(["some_top_level.py"]) == "system"


def test_pure_content_is_content():
    assert classify(["docs/guide.md"]) == "content"
    assert classify(["README.md"]) == "content"
    assert classify(["website/index.html"]) == "content"
    assert classify(["web/blog/post.html", "docs/x.md"]) == "content"


def test_mixed_system_and_content_is_system():
    assert classify(["docs/guide.md", "hermes/agent.py"]) == "system"


def test_prompt_files_are_system_at_any_depth():
    # *.prompt is autonomous-agent behaviour -> always strong reviewer, not via
    # the unknown-path fail-safe but explicitly.
    assert classify(["offsite-geo/geo-scout.prompt"]) == "system"
    assert classify(["infographic/infographic-engineer.prompt"]) == "system"
    assert classify(["auditor/auditor.prompt"]) == "system"
    assert classify(["some/random/dir/x.prompt"]) == "system"
    assert classify(["top.prompt"]) == "system"


def test_known_prompt_dirs_are_system():
    assert classify(["offsite-geo/anything.txt"]) == "system"
    assert classify(["infographic/notes.md"]) == "system"


def test_prompt_files_not_surfaced_as_unknown():
    # Now recognised, so they must NOT appear in unknown_paths.
    assert unknown_paths(["offsite-geo/geo-scout.prompt"]) == []


def test_unknown_path_fails_safe_to_system():
    # Not in either list => must be system, not content.
    assert classify(["weird/unknown_dir/file.bin"]) == "system"


def test_empty_changeset_is_content():
    assert classify([]) == "content"
    assert classify(["", "  "]) == "content"


def test_leading_dot_slash_normalised():
    assert classify(["./hermes/agent.py"]) == "system"
    assert classify(["./README.md"]) == "content"


def test_unknown_paths_surfaced():
    paths = ["hermes/agent.py", "docs/x.md", "weird/thing.bin"]
    assert unknown_paths(paths) == ["weird/thing.bin"]


# --- Phase 4: per-repo tiering ----------------------------------------------
BIG = "br41s/biglobster"
FIN = "br41s/FinView"
GS = "br41s/grow-shop-api"
ENGINE = "br41s/hermes-sandbox"


def test_engine_slug_matches_no_repo():
    # Passing the engine slug uses the original ruleset (back-compat with None).
    assert classify(["web/index.html"], ENGINE) == "content"
    assert classify(["hermes/agent.py"], ENGINE) == "system"
    assert classify(["docs/x.md"], ENGINE) == "content"


def test_profile_verified_publish_dirs_are_content():
    # Eleventy layout (biglobster#339): posts live in site/blog/.
    assert classify(["site/blog/post.html"], BIG) == "content"
    assert classify(["web/blog/images/post-cover.jpg"], BIG) == "content"
    assert classify(["web/assets/og-default.avif"], BIG) == "content"


def test_profile_core_pages_and_build_are_system():
    # The broad ``web/`` content rule must NOT leak onto profile repos.
    assert classify(["web/index.html"], BIG) == "system"
    assert classify(["web/build.mjs"], BIG) == "system"
    assert classify(["web/main.js"], BIG) == "system"
    assert classify(["src/index.js"], BIG) == "system"
    # Post-Eleventy: legacy frozen HTML, layouts, brand data and build config
    # are all system — a write there is stale workflow or a template change.
    assert classify(["web/blog/post.html"], BIG) == "system"
    assert classify(["site/_includes/layouts/base.njk"], BIG) == "system"
    assert classify(["site/index.html"], BIG) == "system"
    assert classify(["eleventy.config.mjs"], BIG) == "system"


def test_profile_prose_and_static_assets_are_content():
    assert classify(["docs/guide.md"], BIG) == "content"
    assert classify(["README.md"], FIN) == "content"
    assert classify(["assets/logo.svg"], FIN) == "content"
    assert classify(["CHANGELOG.md"], FIN) == "content"


def test_profile_behaviour_files_are_system():
    for p in ["SOUL.md", "CLAUDE.md", "MISSION.md", "CHARTER.md", "OPS.md"]:
        assert classify([p], BIG) == "system", p
    assert classify(["agents/x.prompt"], FIN) == "system"


def test_profile_extra_dir_is_repo_scoped():
    # site/blog/ is biglobster-specific; unknown on FinView -> fail-safe system.
    assert classify(["site/blog/post.html"], BIG) == "content"
    assert classify(["site/blog/post.html"], FIN) == "system"


def test_profile_mixed_content_and_code_is_system():
    assert classify(["site/blog/post.html", "web/main.js"], BIG) == "system"


def test_profile_generated_outputs_are_system():
    # blog.html, sitemap.xml and feed.xml are generated by the Eleventy build;
    # a PR touching them is stale-workflow noise or a template change. The only
    # non-system path here is the post + its cover.
    assert classify(
        ["site/blog/post.html", "web/blog/images/post-cover.png"],
        BIG,
    ) == "content"
    assert classify(["web/blog.html"], BIG) == "system"
    assert classify(["web/sitemap.xml"], BIG) == "system"
    assert classify(["web/blog.html"], FIN) == "system"
    assert classify(["web/sitemap.xml"], FIN) == "system"


def test_profile_code_repo_defaults_to_system():
    assert classify(["api.js"], GS) == "system"
    assert classify(["package.json"], GS) == "system"
    assert classify(["README.md"], GS) == "content"


def test_profile_unknown_path_fails_safe():
    assert classify(["frontend/App.tsx"], FIN) == "system"
    assert classify(["wsgi.py"], "br41s/SocialAgenda") == "system"
