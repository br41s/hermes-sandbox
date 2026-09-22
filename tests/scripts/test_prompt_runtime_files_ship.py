"""Every file a cron prompt runs BY PATH must survive .dockerignore.

Lives in tests/scripts/ and not tests/docker/ on purpose: this is a text
check over .dockerignore and the prompts, and tests/docker/conftest.py
skips its whole directory when no Docker daemon is present. A guard that
silently skips on every laptop and in every CI job without Docker is worse
than no guard — it reads as green.

Cron agents cannot use `execute_code` or `python3 -c`, so non-trivial logic
ships as a committed helper script the prompt invokes by absolute path inside
the container. That makes .dockerignore part of the contract: if the script is
excluded from the build context, the prompt is still shipped and still tells
the agent to run it, and the run fails on a missing file.

It fails quietly, too. The job reports, the scheduler records a normal run, and
the only symptom is that the agent stops producing work.

This is not hypothetical. `infographic/` is excluded wholesale (upstream keeps
README assets there) with a single `!infographic/*.prompt` re-include. The first
deploy of the unified Infographic Engineer therefore shipped the prompt and left
`validate_infographic.py` and `inter-metrics.json` behind — the validator the
prompt calls its one mandatory step.
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
CONTAINER_ROOT = "/opt/hermes/"

# Prompts that invoke a committed helper by absolute container path.
PROMPTS = sorted(REPO_ROOT.glob("*/*.prompt"))

_PATH_RE = re.compile(r"/opt/hermes/([A-Za-z0-9_./-]+\.(?:py|mjs|js|json|sh))")


def dockerignore_rules():
    rules = []
    for raw in DOCKERIGNORE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        rules.append(line)
    return rules


def is_ignored(rel_path, rules):
    """Last matching rule wins, which is Docker's own precedence."""
    import fnmatch

    ignored = False
    for rule in rules:
        negate = rule.startswith("!")
        pattern = rule[1:] if negate else rule
        pattern = pattern.rstrip("/")
        hit = (
            fnmatch.fnmatch(rel_path, pattern)
            or fnmatch.fnmatch(rel_path, pattern + "/*")
            or rel_path.startswith(pattern + "/")
        )
        if hit:
            ignored = not negate
    return ignored


def referenced_runtime_files():
    """(prompt, repo-relative path) for every /opt/hermes/... file a prompt runs."""
    found = []
    for prompt in PROMPTS:
        text = prompt.read_text(encoding="utf-8")
        for match in sorted(set(_PATH_RE.findall(text))):
            found.append((prompt.relative_to(REPO_ROOT).as_posix(), match))
    return found


def test_prompts_reference_at_least_one_helper():
    """Guard the guard: a broken regex would make every assertion below vacuous."""
    refs = referenced_runtime_files()
    assert refs, (
        "No prompt referenced a /opt/hermes/... helper. Either the convention "
        "changed or _PATH_RE stopped matching — this test proves nothing until "
        "it finds something."
    )


@pytest.mark.parametrize("prompt,rel", referenced_runtime_files())
def test_referenced_helper_is_not_dockerignored(prompt, rel):
    assert (REPO_ROOT / rel).exists(), (
        f"{prompt} runs /opt/hermes/{rel}, which does not exist in the repo."
    )
    assert not is_ignored(rel, dockerignore_rules()), (
        f"{prompt} runs /opt/hermes/{rel}, but .dockerignore excludes it from "
        f"the build context. The prompt would ship and the file would not, so "
        f"every run fails on a missing file with no error anywhere else. Add a "
        f"`!{rel}` re-include."
    )


def test_the_infographic_validator_and_its_metrics_survive():
    """The specific regression, pinned by name rather than only by the sweep."""
    rules = dockerignore_rules()
    for rel in ("infographic/validate_infographic.py", "infographic/inter-metrics.json"):
        assert (REPO_ROOT / rel).exists(), f"{rel} is missing from the repo"
        assert not is_ignored(rel, rules), f"{rel} is excluded from the Docker build context"


def test_the_infographic_prompt_still_survives():
    """The re-include that already existed, so a rewrite cannot drop it."""
    assert not is_ignored("infographic/infographic-engineer.prompt", dockerignore_rules())


def test_readme_assets_in_infographic_are_still_excluded():
    """The exclusion exists for a reason — do not let the fix widen into 'ship it all'."""
    rules = dockerignore_rules()
    assert is_ignored("infographic/fireworks-provider/infographic.png", rules), (
        "infographic/ holds upstream README screenshots; they must stay out of "
        "the image. Re-include by extension, never the whole directory."
    )
