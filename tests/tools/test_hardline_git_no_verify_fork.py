"""Fork: `git commit --no-verify` / `-n` is hardline-blocked (scripts/git-guard/).

The pre-commit guard is the whole enforcement path on Hermes volumes (GitHub Free +
private: no Actions, branch protection or server hooks), so bypassing it must hit the
floor. Moved out of test_hardline_blocklist.py so upstream's file merges clean.
"""

import pytest

from tools.approval import detect_hardline_command

_BLOCK = [
    "git commit --no-verify -m 'x'",
    'git commit -m "x" --no-verify',
    "git commit -n -m 'fix'",
    "git commit -nm 'fix'",
    "git commit -anm 'fix'",
    "git -C /repo commit --no-verify",
    "git add -A && git commit --no-verify -m 'sync'",
]

# Ordinary commits, and -n that is not git commit's flag, must NOT be blocked.
_ALLOW = [
    "git commit -m 'normal commit'",
    "git commit -am 'wip'",
    "git add -A && git commit -m 'ok'",
    "git commit -m 'added -n flag handling'",
    "git commit -a -m 'msg mentioning -n inline'",
    "echo -n hello",
    "npm run build -- -n",
]


@pytest.mark.parametrize("command", _BLOCK)
def test_commit_hook_bypass_is_hardline(command):
    is_hl, desc = detect_hardline_command(command)
    assert is_hl, f"expected hardline to match {command!r}"
    assert desc


@pytest.mark.parametrize("command", _ALLOW)
def test_ordinary_commit_is_not_hardline(command):
    is_hl, desc = detect_hardline_command(command)
    assert not is_hl, f"expected hardline NOT to match {command!r} (got: {desc})"
