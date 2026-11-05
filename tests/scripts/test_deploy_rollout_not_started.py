"""deploy.sh stops Hermes before it moves the tag, and says how to go back (2026-11-05).

Two containers must never run at once: they would poll Telegram with one bot token (a
duplicate is fatal) and write the same volume and state.db. Coolify's redeploy can start
the new container before stopping the old one, so the script stops first, confirms the
container is gone, then moves the tag and starts. A new container that never reports the
commit gets the rollback steps, because a stopped app is not going to fix itself.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"
TEXT = DEPLOY_SH.read_text(encoding="utf-8")


def test_stop_then_gone_then_tag_then_start() -> None:
    pull = TEXT.index('run ssh -n "$SSH_HOST" docker pull')
    stop = TEXT.index("run coolify POST /stop")
    gone = TEXT.index('[ -n "$RUNNING_NOW" ] || break')
    move = TEXT.index("run coolify PATCH")
    start = TEXT.index("run coolify POST /start")
    assert pull < stop < gone < move < start


def test_a_container_that_will_not_stop_never_moves_the_tag() -> None:
    block = TEXT[TEXT.index("run coolify POST /stop"):TEXT.index("run coolify PATCH")]
    assert "exit 1" in block
    assert "still running" in block and "The tag was not moved" in block


def _loop_with(container_body: str) -> subprocess.CompletedProcess:
    """The real wait loop, with ``hermes_container`` replaced, then a line standing in for the tag move."""
    block = TEXT[TEXT.index("  DEADLINE=$(( $(date +%s) + 120 ))"):TEXT.index('echo "→ Pointing the app at')]
    block = block[:block.rindex("fi")]  # drop the closing fi of the DRY_RUN branch
    script = (f"set -euo pipefail\nSSH_HOST=host\nsleep() {{ :; }}\nhermes_container() {{ {container_body}; }}\n"
              f"{block}\necho TAG_MOVED\n")
    return subprocess.run(["bash", "-c", script], check=False, capture_output=True, text=True, timeout=20)


def test_a_failed_ssh_is_not_read_as_a_stopped_container() -> None:
    # The hazard: empty output from a failed ssh looking like "gone", then the tag moves
    # while the old container is still terminating.
    out = _loop_with("return 255")
    assert out.returncode == 1
    assert "TAG_MOVED" not in out.stdout
    assert "Cannot reach" in out.stderr


def test_the_tag_moves_only_once_the_container_is_really_gone() -> None:
    assert "TAG_MOVED" in _loop_with("return 0").stdout


def test_the_container_lookup_is_anchored_and_exec_wants_exactly_one() -> None:
    assert "--filter name=^/?$APP_UUID-" in TEXT
    func = TEXT[TEXT.index("pod_exec() {"):]
    assert 'c="$(hermes_container)" || return 1' in func
    assert "\"\"|*$'\\n'*) return 1" in func


def test_the_script_never_uses_coolifys_redeploy() -> None:
    for line in TEXT.splitlines():
        if line.lstrip().startswith("#"):
            continue
        assert "/deploy" not in line.replace("scripts/deploy.sh", "")
        assert "/restart" not in line


def test_the_token_never_reaches_an_argv() -> None:
    func = TEXT[TEXT.index("coolify() {"):TEXT.index("hermes_container() {")]
    assert "-H @-" in func
    assert 'printf \'Authorization: Bearer %s\\n\'' in func
    assert 'ssh "$SSH_HOST"' in func


def test_a_dry_run_touches_nothing() -> None:
    # A dry run reads no commit from the container and never waits on it.
    assert 'if [ "$DRY_RUN" -eq 0 ]; then\n  SHA_BEFORE=' in TEXT
    assert 'if [ "$DRY_RUN" -eq 0 ]; then\n  DEADLINE=' in TEXT


def test_a_container_that_never_reports_says_how_to_roll_back() -> None:
    block = TEXT[TEXT.index("Hermes did not report"):TEXT.index("# ── Verification")]
    assert "sha-${SHA_BEFORE:-" in block
    assert "stop the app" in block and "start it" in block
    assert "Never Redeploy" in block
    assert "exit 1" in block
