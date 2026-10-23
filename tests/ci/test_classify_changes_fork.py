"""Fork lanes of scripts/ci/classify_changes.py.

The fork keeps its own ``ci.yml`` orchestrator, which reads three lanes that
upstream's ``ci.yaml`` does not: ``desktop`` (the ~20-minute apps/desktop leg,
run only when a PR touches it), ``mcp_catalog`` and ``ci_review`` (the
``ci-reviewed`` label gate on CI-sensitive files). The WhatsApp lead bot is
also a frontend path here, so a bot-only PR runs its JS job.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "classify_changes.py"
_spec = importlib.util.spec_from_file_location("classify_changes_fork", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
classify = _mod.classify
ci_review_files = _mod.ci_review_files


def _fork(files: list[str]) -> dict[str, bool]:
    lanes = classify(files)
    return {k: lanes[k] for k in ("desktop", "mcp_catalog", "ci_review")}


@pytest.mark.parametrize("files,desktop,mcp_catalog,ci_review", [
    (["apps/desktop/src/app.tsx"], True, False, False),
    (["apps/shared/src/x.ts"], True, False, False),
    (["package-lock.json"], True, False, False),
    (["web/src/pages/CronPage.tsx"], False, False, False),
    (["ui-tui/src/entry.ts"], False, False, False),
    (["gateway/run.py"], False, False, False),
    (["optional-mcps/foo/manifest.yaml"], False, True, False),
    (["hermes_cli/mcp_catalog.py"], False, True, False),
    (["apps/desktop/eslint.config.mjs"], True, False, True),
    (["eslint.config.shared.mjs"], False, False, True),
    (["web/eslint.config.js"], False, False, True),
    ([".prettierrc"], False, False, True),
    ([".github/supply-chain-reviewed.txt"], True, False, True),
    ([".github/workflows/tests.yml"], True, False, True),
    ([], True, False, True),
])
def test_fork_lanes(files, desktop, mcp_catalog, ci_review):
    assert _fork(files) == {"desktop": desktop, "mcp_catalog": mcp_catalog, "ci_review": ci_review}


@pytest.mark.parametrize("path", [
    "scripts/whatsapp-lead-bot/src/assistant.js",
    "scripts/whatsapp-lead-bot/package-lock.json",
])
def test_whatsapp_lead_bot_runs_the_frontend_lane(path):
    assert classify([path])["frontend"]


def test_ci_review_files_returns_only_sensitive_paths_sorted_and_unique():
    assert ci_review_files([
        "apps/desktop/src/app.tsx",
        ".github/workflows/ci.yml",
        "apps/desktop/eslint.config.mjs",
        ".github/workflows/ci.yml",
    ]) == [
        ".github/workflows/ci.yml",
        "apps/desktop/eslint.config.mjs",
    ]


def test_main_emits_the_ci_review_files_output(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(_mod.sys, "stdin", __import__("io").StringIO("web/eslint.config.js\n"))
    monkeypatch.setattr(_mod, "pull_request_labels", list)
    assert _mod.main() == 0
    out = capsys.readouterr().out.splitlines()
    assert "ci_review=true" in out
    assert f"ci_review_files={json.dumps(['web/eslint.config.js'])}" in out


def test_fork_lanes_reach_the_composite_action():
    action = (Path(__file__).resolve().parents[2] / ".github/actions/detect-changes/action.yml").read_text()
    for lane in ("desktop", "mcp_catalog", "ci_review", "ci_review_files"):
        assert f"steps.classify.outputs.{lane} }}}}" in action, lane
