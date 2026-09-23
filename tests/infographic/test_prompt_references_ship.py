"""Every /opt/hermes/... path a prompt names must exist AND reach the image.

The first deploy of the validator shipped the prompt telling the agent to run
`python3 /opt/hermes/infographic/validate_infographic.py` and did not ship the
file: `.dockerignore` excluded `infographic/` wholesale and re-included only
`*.prompt`. The deploy was green. The agent simply stopped being able to check
its own work.

The prompt now names five scripts instead of one, so the cost of that gap is
five times higher and the test is worth having.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = sorted(ROOT.glob("**/*.prompt"))
REF = re.compile(r"/opt/hermes/([A-Za-z0-9_./-]+\.(?:py|json|sh|mjs))")


def referenced_paths():
    for prompt in PROMPTS:
        if ".git" in prompt.parts:
            continue
        for match in REF.finditer(prompt.read_text(encoding="utf-8")):
            yield prompt.relative_to(ROOT), match.group(1)


def test_at_least_one_prompt_references_the_pipeline():
    """Guard the guard: a regex that silently matches nothing proves nothing."""
    refs = {ref for _prompt, ref in referenced_paths()}
    assert "infographic/validate_infographic.py" in refs, (
        f"expected the infographic prompt to name the validator; found {sorted(refs)}")


@pytest.mark.parametrize("prompt,ref", sorted(set(referenced_paths())))
def test_referenced_file_exists_in_the_repo(prompt, ref):
    assert (ROOT / ref).is_file(), f"{prompt} tells the agent to run /opt/hermes/{ref}, which does not exist"


@pytest.mark.parametrize("prompt,ref", sorted(set(referenced_paths())))
def test_referenced_file_is_not_excluded_from_the_image(prompt, ref):
    """Walk .dockerignore the way Docker does: last matching pattern wins."""
    rules = []
    for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rules.append((line.lstrip("!"), line.startswith("!")))

    excluded = False
    for pattern, negated in rules:
        if _matches(pattern, ref):
            excluded = not negated
    assert not excluded, (
        f"{prompt} tells the agent to run /opt/hermes/{ref}, but .dockerignore excludes it, "
        f"so it will not be in the image. Add a '!{ref}' re-include.")


def _matches(pattern, path):
    import fnmatch
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if fnmatch.fnmatch(path, pattern):
        return True
    # a bare directory name excludes everything under it
    return path.startswith(pattern.rstrip("/") + "/")
