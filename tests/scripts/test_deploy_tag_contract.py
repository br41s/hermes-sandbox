"""The image-tag contract between the builder, the deployer and the image.

Deploying is *moving a tag*. ``scripts/deploy.sh`` derives
``sha-$(git rev-parse --short=9 HEAD)`` and points the Zeabur service at it;
whatever built the image had to publish that exact string. Nothing at runtime
reconciles the two — if they disagree the service is pointed at a tag nobody
pushed and the rollout dies as "Service Image Pull Failed" (PR #197/#198),
with the pod not coming back on its own.

The abbreviation length is the fragile part, and it cannot be left to git:
git picks one from the repository's object count, so a full clone gives 9
while the shallow clone ``actions/checkout`` makes by default gives 7. Same
commit, different tag. ``--short=9`` pins it, and every party must say so
explicitly.

Three parties, because there are two build paths:
  * ``.github/workflows/ghcr-publish.yml`` — the default; builds every push
    to main.
  * ``cloudbuild.yaml`` — the ``--build`` fallback, reached only when Actions
    is unavailable. Rarely exercised, so drift here surfaces at the worst
    possible moment.
  * ``scripts/deploy.sh`` — the consumer, on both paths.

These are prose-documented in all three files and in ``CLAUDE.md``. This
asserts them mechanically, because the failure mode is a broken production
deploy rather than a broken test.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ghcr-publish.yml"
CLOUDBUILD = REPO_ROOT / "cloudbuild.yaml"
DOCKERFILE = REPO_ROOT / "Dockerfile"

# The contract, in one place. Both builders tag with it; deploy.sh derives it.
SHORT_SHA_CMD = "git rev-parse --short=9 HEAD"
IMAGE = "ghcr.io/br41s/hermes-sandbox"


def _uncommented(path: Path) -> list[str]:
    """Instruction lines only — comments and blanks dropped.

    All three files explain the contract in comments that *quote* the forms
    they warn against (a bare ``--short``, upstream's full ``github.sha``), so
    matching raw text would find the prose rather than a real instruction.
    """
    return [
        line
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_deploy_derives_the_tag_with_a_pinned_abbreviation() -> None:
    """A bare ``--short`` here is the 7-vs-9 bug, silently."""
    lines = _uncommented(DEPLOY_SH)

    assert any(
        SHORT_SHA_CMD in line and line.strip().startswith("SHA=") for line in lines
    ), f"deploy.sh must derive SHA with `{SHORT_SHA_CMD}`"

    # Guard the mistake directly: any *other* HEAD abbreviation feeding the
    # deployed tag reintroduces git's object-count-dependent length.
    offenders = [
        line.strip()
        for line in lines
        if line.strip().startswith("SHA=") and "--short=9" not in line
    ]
    assert not offenders, f"tag SHA must be pinned to --short=9: {offenders}"

    assert any(
        line.strip() == 'TAG="sha-$SHA"' for line in lines
    ), "deploy.sh must build the tag as sha-<short sha>"


def test_actions_workflow_publishes_the_tag_deploy_will_look_for() -> None:
    """Upstream's ``docker.yml`` stamps the FULL ``github.sha``. Not here."""
    lines = _uncommented(WORKFLOW)
    text = "\n".join(lines)

    assert SHORT_SHA_CMD in text, (
        f"the workflow must derive its short SHA with `{SHORT_SHA_CMD}` — "
        "actions/checkout's shallow clone otherwise abbreviates to 7"
    )
    # `github.sha` is the full 40-char form; it must not reach a tag or the
    # build-arg deploy.sh polls for.
    offenders = [
        line.strip()
        for line in lines
        if re.search(r"github\.sha", line)
        and ("sha-" in line or "HERMES_GIT_SHA" in line)
    ]
    assert not offenders, (
        f"the full github.sha cannot match deploy.sh's short tag: {offenders}"
    )

    # The workflow templates the image name (`ghcr.io/${{ github.repository }}`)
    # rather than hardcoding it, and is pinned to this fork by the job's `if:`.
    assert "ghcr.io/${{ github.repository }}:latest" in text, (
        "the workflow must push :latest — it is what a fresh service pulls"
    )
    assert (
        "ghcr.io/${{ github.repository }}:sha-${{ steps.sha.outputs.short }}" in text
    ), (
        "the workflow must push the immutable sha- tag deploy.sh moves the "
        "service to, derived from the SAME pinned short SHA"
    )
    assert "github.repository == 'br41s/hermes-sandbox'" in text, (
        f"the templated image must resolve to {IMAGE} on this fork"
    )


def test_both_build_paths_stamp_the_sha_the_image_reports() -> None:
    """``deploy.sh --status`` is the only thing that can say what is live.

    It reads ``/opt/hermes/.hermes_build_sha``, which exists only because the
    build passed ``HERMES_GIT_SHA``. Without it the status flag reports
    "production : UNKNOWN" and the checkout-vs-image drift it exists to catch
    goes back to being invisible.
    """
    for path in (WORKFLOW, CLOUDBUILD):
        text = "\n".join(_uncommented(path))
        assert "HERMES_GIT_SHA" in text, (
            f"{path.name} must pass the HERMES_GIT_SHA build-arg"
        )

    dockerfile = "\n".join(_uncommented(DOCKERFILE))
    assert "ARG HERMES_GIT_SHA" in dockerfile
    assert "/opt/hermes/.hermes_build_sha" in dockerfile, (
        "the Dockerfile must write the build SHA where --status reads it"
    )

    status_reader = "\n".join(_uncommented(DEPLOY_SH))
    assert "/opt/hermes/.hermes_build_sha" in status_reader, (
        "deploy.sh --status must read the file the Dockerfile writes"
    )


def test_cloudbuild_fallback_tags_the_same_two_tags() -> None:
    """The ``--build`` path is only reached when Actions is broken.

    It must land the same pair of tags on one image, or a fallback deploy
    points at nothing.
    """
    text = "\n".join(_uncommented(CLOUDBUILD))

    assert f"{IMAGE}:sha-$_COMMIT_SHA" in text, (
        "cloudbuild must tag the immutable sha- tag deploy.sh moves to"
    )
    assert f"{IMAGE}:latest" in text, "cloudbuild must also tag :latest"
    assert "HERMES_GIT_SHA=$_COMMIT_SHA" in text, (
        "the stamped SHA must be the same value as the tag, not a second source"
    )


def test_deploy_never_prints_cloud_build_substitutions() -> None:
    """``_GITHUB_TOKEN`` is stored in clear in every build's substitutions.

    Filtering on a substitution does not print it; formatting does. A
    ``--format`` that includes them leaks the token into the terminal and into
    any pasted log.
    """
    offenders = [
        line.strip()
        for line in _uncommented(DEPLOY_SH)
        if "--format" in line and "substitutions" in line
    ]
    assert not offenders, (
        f"formatting substitutions leaks _GITHUB_TOKEN in clear: {offenders}"
    )


def test_the_dirty_tree_guard_is_unconditional() -> None:
    """Both deploy paths refuse a dirty tree, not just ``--build``.

    The *reason* in the comment is ``--build``-specific (``gcloud builds
    submit`` uploads the working directory), which reads as though the guard
    were gated on it — ``CLAUDE.md`` claimed exactly that until this test was
    written. It is not: the check runs before the paths diverge, so a plain
    ``scripts/deploy.sh`` also wants a clean checkout.

    Locked because the two readings differ in what an operator expects at the
    moment they are trying to ship, and nothing else would notice a change.
    """
    lines = _uncommented(DEPLOY_SH)

    guard = [
        i
        for i, line in enumerate(lines)
        if 'git status --porcelain' in line and line.lstrip().startswith("if ")
    ]
    assert len(guard) == 1, "expected exactly one dirty-tree guard in deploy.sh"

    assert "DO_BUILD" not in lines[guard[0]], (
        "the dirty-tree guard must stay unconditional, or CLAUDE.md's "
        "description of which paths refuse a dirty tree goes stale again"
    )
