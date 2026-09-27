"""Fork: the supply-chain scan honours .github/supply-chain-reviewed.txt.

Runs the real "Scan diff for critical patterns" step, extracted from
.github/workflows/supply-chain-audit.yml, against a throwaway git repo, so the
test covers the shell the workflow actually executes.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO / ".github" / "workflows" / "supply-chain-audit.yml"
_REVIEWED = ".github/supply-chain-reviewed.txt"


def _scan_script() -> str:
    wf = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    for step in wf["jobs"]["scan"]["steps"]:
        if step.get("name") == "Scan diff for critical patterns":
            return (
                step["run"]
                .replace("${{ github.event.pull_request.base.sha }}", "$T_BASE")
                .replace("${{ github.event.pull_request.head.sha }}", "$T_HEAD")
            )
    raise AssertionError("scan step not found")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    ).stdout.strip()


def _run_scan(tmp_path: Path, setup_py: str, reviewed: str | None) -> tuple[str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "setup.py").write_text("from setuptools import setup\nsetup()\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")

    (repo / "setup.py").write_text(setup_py, encoding="utf-8")
    if reviewed is not None:
        (repo / ".github").mkdir()
        (repo / _REVIEWED).write_text(reviewed, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "head")
    head = _git(repo, "rev-parse", "HEAD")

    out = tmp_path / "github_output"
    proc = subprocess.run(
        ["bash", "-c", _scan_script()], cwd=repo, capture_output=True, text=True,
        env={**os.environ, "T_BASE": base, "T_HEAD": head, "GITHUB_OUTPUT": str(out)},
    )
    assert proc.returncode == 0, proc.stderr
    return out.read_text(encoding="utf-8"), proc.stdout


_NEW = "from setuptools import setup\nsetup(cmdclass={})\n"
_NEW_SUM = hashlib.sha256(_NEW.encode()).hexdigest()


def test_reviewed_hash_accepts_the_exact_content(tmp_path):
    output, stdout = _run_scan(tmp_path, _NEW, f"# note\nsetup.py {_NEW_SUM} reviewed\n")
    assert "found=false" in output
    assert "matches its reviewed sha256" in stdout


def test_any_other_content_still_fires(tmp_path):
    output, _ = _run_scan(tmp_path, _NEW + "import os\n", f"setup.py {_NEW_SUM}\n")
    assert "found=true" in output


def test_no_allowlist_still_fires(tmp_path):
    output, _ = _run_scan(tmp_path, _NEW, None)
    assert "found=true" in output


def test_commented_entry_is_not_honoured(tmp_path):
    output, _ = _run_scan(tmp_path, _NEW, f"# setup.py {_NEW_SUM}\n")
    assert "found=true" in output


def test_allowlist_is_ci_sensitive():
    spec = importlib.util.spec_from_file_location(
        "classify_changes_fork", _REPO / "scripts" / "ci" / "classify_changes.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.ci_review_files([_REVIEWED]) == [_REVIEWED]


@pytest.mark.parametrize("line", [
    ln for ln in (_REPO / _REVIEWED).read_text(encoding="utf-8").splitlines()
    if ln.strip() and not ln.startswith("#")
])
def test_committed_entries_are_well_formed(line):
    path, digest, *_ = line.split()
    assert (_REPO / path).is_file(), path
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
