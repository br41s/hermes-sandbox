"""Gap Hunter prompts work in the scheduler's per-run checkout, never their own clone.

A job whose workdir is a git tree runs in an isolated checkout that the scheduler
deletes after the run (cron/fork_ext/isolated_checkout.py). The FinView prompt
used to `git clone` into /tmp instead; `rm -rf` is refused in cron mode, so each
run picked a new directory name and nothing removed them: ~10 GB of clones and
loose drafts on the production volume by 2026-10-21.
"""
from pathlib import Path

import pytest

PROMPTS = sorted((Path(__file__).resolve().parents[2] / "gap-hunter").glob("*.prompt"))


def test_prompts_exist() -> None:
    assert PROMPTS


@pytest.mark.parametrize("prompt", PROMPTS, ids=lambda p: p.name)
def test_prompt_does_not_clone_its_own_workspace(prompt: Path) -> None:
    text = prompt.read_text()
    assert "git clone" not in text
    assert "/tmp/finview-gap-hunter" not in text
