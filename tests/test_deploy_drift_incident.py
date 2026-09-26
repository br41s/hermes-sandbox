"""Regression lock for the deploy-drift signal.

Production runs whatever the Zeabur service tag points at; `main` moves on every
merge and nothing reconciles the two. On 2026-09-22 the gap had reached 11
commits — one of them a real fix — and surfaced only because someone looked.

Hermetic: the compare payload and the running sha are injected, so no network
and no container. The founding-case fixture is the REAL
`compare/777a10031...2cd61760e` response shape: 11 commits, 13 aggregate files,
of which 10 are workflow-only, 1 is a test, and 2 are runtime.

See tasks/deploy-automation-plan.md, "Half 1 (revised)".
"""
from datetime import timedelta

import pytest

from incidents.sweep import (DEPLOY_DRIFT_GRACE_HOURS, _now, deploy_drift_incidents,
                             sweep)

RUNNING = "777a10031"

_FOUNDING_WORKFLOW_FILES = [
    ".github/workflows/ci.yml", ".github/workflows/docker-lint.yml",
    ".github/workflows/docker.yml", ".github/workflows/ghcr-publish.yml",
    ".github/workflows/js-autofix.yml", ".github/workflows/lint.yml",
    ".github/workflows/osv-scanner.yml", ".github/workflows/tests.yml",
    ".github/workflows/upload_to_pypi.yml", ".github/workflows/uv-lockfile-check.yml",
]
_FOUNDING_RUNTIME_FILES = ["incidents/sweep.py", "remediation/registry.py"]


def _compare(*, ahead_by=11, status="ahead", files=None, age_hours=30,
             n_commits=11, omit_files=False):
    """A compare payload shaped like the real API response."""
    oldest = (_now() - timedelta(hours=age_hours)).isoformat()
    payload = {
        "status": status,
        "ahead_by": ahead_by,
        "total_commits": n_commits,
        "commits": [{"commit": {"committer": {"date": oldest}}}
                    for _ in range(n_commits)],
    }
    if not omit_files:
        listed = (_FOUNDING_WORKFLOW_FILES + ["tests/test_remediation_registry.py"]
                  + _FOUNDING_RUNTIME_FILES) if files is None else files
        payload["files"] = [{"filename": f} for f in listed]
    return payload


def _incidents(**kw):
    kw.setdefault("running_sha", RUNNING)
    return deploy_drift_incidents(compare=_compare(**kw.pop("compare_kw", {})), **kw)


class TestTheFoundingCase:
    def test_real_drift_fires_and_names_both_numbers(self):
        out = _incidents()
        assert len(out) == 1
        inc = out[0]
        assert inc.id == f"deploy-drift:{RUNNING}"
        assert "11 commit(s) behind main" in inc.title
        # Two, not three: the test file is inert, the workflows are inert.
        assert "runtime files changed: 2" in inc.detail
        assert "behind by: 11 commit(s)" in inc.detail

    def test_handoff_points_at_the_actual_deploy_command(self):
        assert "scripts/deploy.sh" in _incidents()[0].handoff


class TestCryingWolf:
    def test_workflow_only_drift_is_silent_however_many_commits(self):
        assert _incidents(compare_kw={"files": _FOUNDING_WORKFLOW_FILES,
                                      "ahead_by": 40, "n_commits": 40}) == []

    def test_tasks_and_tests_alone_are_inert(self):
        assert _incidents(compare_kw={
            "files": ["tasks/deploy-automation-plan.md",
                      "tests/test_deploy_drift_incident.py"]}) == []

    def test_a_markdown_change_is_NOT_inert(self):
        # Skill SKILL.md and AGENTS.md are read by the runtime.
        assert len(_incidents(compare_kw={"files": ["skills/foo/SKILL.md"]})) == 1

    def test_a_prompt_change_is_NOT_inert(self):
        assert len(_incidents(compare_kw={"files": ["cron/gap-hunter.prompt"]})) == 1

    def test_inside_the_grace_window_is_silent(self):
        assert _incidents(compare_kw={
            "age_hours": DEPLOY_DRIFT_GRACE_HOURS - 1}) == []

    def test_just_past_the_grace_window_fires(self):
        assert len(_incidents(compare_kw={
            "age_hours": DEPLOY_DRIFT_GRACE_HOURS + 1})) == 1

    def test_identical_is_silent(self):
        assert _incidents(compare_kw={"status": "identical", "ahead_by": 0}) == []


class TestFailsLoudNotSilent:
    """A producer that returns [] on every failure looks exactly like a healthy
    one. That is how the auditor judge went twelve weeks without running."""

    @pytest.mark.parametrize("status", ["behind", "diverged"])
    def test_production_running_code_not_on_main_is_loud(self, status):
        out = _incidents(compare_kw={"status": status})
        assert len(out) == 1
        assert out[0].id.startswith("deploy-drift-blind:not-an-ancestor:")
        assert "NOT on main" in out[0].detail

    def test_no_token_still_reads_the_public_repo(self, monkeypatch):
        """Regression: the watcher is a no-agent cron script, and the runner
        strips GITHUB_TOKEN / GH_TOKEN from every script's env. Requiring a
        token made this signal report BLIND (no-token) on every sweep, though
        the repo is public and compare needs no credential."""
        from incidents import sweep as sweep_mod

        for var in ("HERMES_DEPLOY_DRIFT_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        seen = {}

        def fake_fetch(repo, base, token):
            seen["token"] = token
            return {"status": "identical", "ahead_by": 0}

        monkeypatch.setattr(sweep_mod, "_fetch_deploy_compare", fake_fetch)
        assert deploy_drift_incidents(running_sha=RUNNING) == []
        assert seen == {"token": ""}

    def test_unauthenticated_request_sends_no_authorization_header(self, monkeypatch):
        import io
        import urllib.request

        from incidents import sweep as sweep_mod

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["auth"] = req.get_header("Authorization")
            return io.BytesIO(b'{"status": "identical", "ahead_by": 0}')

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        assert sweep_mod._fetch_deploy_compare("o/r", RUNNING, "") == {
            "status": "identical", "ahead_by": 0}
        assert captured == {"auth": None}

    def test_spent_unauthenticated_rate_limit_is_blind_and_says_so(self, monkeypatch):
        import urllib.error
        import urllib.request
        from email.message import Message

        from incidents import sweep as sweep_mod

        def fake_urlopen(req, timeout=None):
            hdrs = Message()
            hdrs["X-RateLimit-Remaining"] = "0"
            raise urllib.error.HTTPError(req.full_url, 403, "rate limited", hdrs, None)

        for var in ("HERMES_DEPLOY_DRIFT_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        out = sweep_mod.deploy_drift_incidents(running_sha=RUNNING)
        assert len(out) == 1 and out[0].id.startswith("deploy-drift-blind:api-403:")
        assert "rate limit" in out[0].detail and "HERMES_DEPLOY_DRIFT_GITHUB_TOKEN" in out[0].detail

    def test_missing_build_sha_inside_the_image_is_blind(self, tmp_path):
        # tmp_path exists, so this looks like a deployment with no stamp.
        out = deploy_drift_incidents(build_sha_path=tmp_path / ".hermes_build_sha")
        assert len(out) == 1
        assert out[0].id.startswith("deploy-drift-blind:no-build-sha:")

    def test_missing_build_sha_outside_the_image_is_not_applicable(self, tmp_path):
        # No such directory => a laptop or CI, not a deployment. Silence is
        # correct here; a blind incident would fire on every developer's run.
        missing = tmp_path / "not-a-deployment" / ".hermes_build_sha"
        assert deploy_drift_incidents(build_sha_path=missing) == []

    def test_an_omitted_files_list_is_treated_as_runtime_relevant(self):
        # GitHub drops `files` past ~300; assuming "inert" would be the
        # dangerous direction.
        out = _incidents(compare_kw={"omit_files": True})
        assert len(out) == 1
        assert "runtime files changed: unknown" in out[0].detail


class TestDedup:
    """One stale deployment is one alert, not a fresh alert per merge."""

    @staticmethod
    def _sweep(incidents, state_path):
        return sweep(jobs=[], langfuse=[], blocked=[], checkout_drift=[],
                     judge_liveness=[], dependency_alerts=[],
                     deploy_drift=incidents, state_path=state_path)

    def test_same_running_sha_reports_once(self, tmp_path):
        state = tmp_path / "s.json"
        first = self._sweep(_incidents(), state)
        assert "commit(s) behind main" in first
        # Drift grew by another merge — same running sha, so still one alert.
        assert self._sweep(_incidents(compare_kw={"ahead_by": 12}), state) == ""

    def test_a_new_running_sha_reports_again(self, tmp_path):
        state = tmp_path / "s.json"
        self._sweep(_incidents(), state)
        after = deploy_drift_incidents(running_sha="2cd61760e", compare=_compare())
        assert "commit(s) behind main" in self._sweep(after, state)

    def test_id_is_keyed_on_the_running_sha_not_the_count(self):
        a = _incidents()[0].id
        b = _incidents(compare_kw={"ahead_by": 99})[0].id
        assert a == b == f"deploy-drift:{RUNNING}"
