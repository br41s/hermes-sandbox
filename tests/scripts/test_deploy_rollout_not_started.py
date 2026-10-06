"""deploy.sh tells a rollout Zeabur never started from one that is merely slow (2026-10-06).

The deploy of sha-a0b79d9d0 moved the service tag, and Zeabur saved it, but no new
pod was ever created: the pod that predated the move kept serving the old commit
until a manual Restart. deploy.sh timed out with "the rollout may still be in
progress", which sent the owner to wait for something that was never coming. It
now reads the serving pod's name and build SHA before moving the tag, and again at
the deadline; the same pod on the same commit means "never started".
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"
TEXT = DEPLOY_SH.read_text(encoding="utf-8")

POD = "service-6a5ea5074d439e41ee4cd38c-7d9dc45d7b-qjhrs"


def _diagnose(*args: str) -> str:
    func = re.search(r"^rollout_diagnosis\(\) \{\n.*?^\}\n", TEXT, re.S | re.M).group(0)
    script = func + 'rollout_diagnosis "$@"\n'
    out = subprocess.run(["bash", "-c", script, "deploy.sh", *args],
                         capture_output=True, text=True, timeout=10, check=True)
    return out.stdout.strip()


def test_the_same_pod_on_the_same_commit_is_a_rollout_that_never_started() -> None:
    # The 2026-10-06 case, values as production reported them.
    assert _diagnose(POD, "ae7173854", POD, "ae7173854") == "not_started"


@pytest.mark.parametrize("pod_before, sha_before, pod_after, sha_after", [
    (POD, "ae7173854", "service-6a5ea5074d439e41ee4cd38c-5f6b8c9d4-x2k7p", "ae7173854"),  # replaced
    (POD, "ae7173854", "", ""),            # pod unreachable at the deadline (mid-swap)
    ("", "", POD, "ae7173854"),            # could not read it before the move
    (POD, "", POD, ""),                    # image without .hermes_build_sha
])
def test_anything_else_stays_in_progress(pod_before, sha_before, pod_after, sha_after) -> None:
    # "Unknown" must never become a Restart instruction.
    assert _diagnose(pod_before, sha_before, pod_after, sha_after) == "in_progress"


def test_the_pod_is_read_before_the_tag_moves_and_again_at_the_deadline() -> None:
    before = TEXT.index("read -r POD_BEFORE SHA_BEFORE")
    move = TEXT.index("run zeabur service update tag")
    deadline = TEXT.index("read -r POD_AFTER SHA_AFTER")
    assert before < move < deadline
    # A dry run moves nothing, so it must not exec into the pod either.
    assert 'if [ "$DRY_RUN" -eq 0 ]; then\n  read -r POD_BEFORE' in TEXT


def test_never_started_says_restart_and_how_to_roll_back() -> None:
    block = TEXT[TEXT.index('= not_started ]; then'):TEXT.index("may still be in progress")]
    assert "never rolled it out" in block
    assert "Click Restart" in block
    assert "re-run scripts/deploy.sh and answer N" in block  # the in-flight check, before the restart
    assert '-t sha-$SHA_BEFORE -y -i=false' in block          # rollback to what was serving
