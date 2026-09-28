"""Fork: cronjob tool additions — prompt_source / sync_prompt, and the profile routing-gap warning.

Moved out of test_cronjob_tools.py so upstream's file merges clean.
"""

import json
from pathlib import Path

import pytest

from tools.cronjob_tools import cronjob

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class TestPromptSource:
    @pytest.fixture(autouse=True)
    def _setup_cron_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
        monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
        monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")

    def test_update_prompt_source_can_set_and_clear(self):
        created = json.loads(cronjob(action="create", prompt="Check", schedule="every 1h"))
        job_id = created["job_id"]

        updated = json.loads(
            cronjob(
                action="update",
                job_id=job_id,
                prompt_source="gap-hunter/biglobster-gap-hunter.prompt",
            )
        )
        assert updated["success"] is True
        assert updated["job"]["prompt_source"] == "gap-hunter/biglobster-gap-hunter.prompt"

        cleared = json.loads(cronjob(action="update", job_id=job_id, prompt_source=""))
        assert cleared["success"] is True
        assert cleared["job"].get("prompt_source") is None

    def test_sync_prompt_pulls_repo_file_into_live_prompt(self):
        """sync_prompt closes the loop prompt_source only detects: a repo-side
        .prompt fix (merged, reviewed) previously had no way to reach the live
        job's `prompt` field short of hand-copying the file into --prompt."""
        created = json.loads(cronjob(action="create", prompt="stale text", schedule="every 1h"))
        job_id = created["job_id"]

        synced = json.loads(
            cronjob(
                action="sync_prompt",
                job_id=job_id,
                prompt_source="gap-hunter/biglobster-gap-hunter.prompt",
            )
        )
        assert synced["success"] is True
        assert synced["changed"] is True
        assert synced["job"]["prompt_source"] == "gap-hunter/biglobster-gap-hunter.prompt"

        from cron.jobs import get_job
        expected = (REPO_ROOT / "gap-hunter" / "biglobster-gap-hunter.prompt").read_text(encoding="utf-8")
        assert get_job(job_id)["prompt"].strip() == expected.strip()

    def test_sync_prompt_noop_when_already_matching(self):
        expected = (REPO_ROOT / "gap-hunter" / "biglobster-gap-hunter.prompt").read_text(encoding="utf-8")
        created = json.loads(cronjob(action="create", prompt=expected, schedule="every 1h"))
        job_id = created["job_id"]
        json.loads(
            cronjob(
                action="update",
                job_id=job_id,
                prompt_source="gap-hunter/biglobster-gap-hunter.prompt",
            )
        )

        synced = json.loads(cronjob(action="sync_prompt", job_id=job_id))
        assert synced["success"] is True
        assert synced["changed"] is False

    def test_sync_prompt_refuses_to_clobber_a_live_only_edit(self):
        """The emergency path: a job is failing in production, someone fixes its
        prompt in place, and the repo file still holds the broken text. sync
        only ever pushes repo -> live, so without this guard the next sync
        silently deletes the fix. It happened to the Gap Hunter, whose
        output-limit fix reached the live job days before the .prompt file."""
        from cron.jobs import get_job

        source = "gap-hunter/biglobster-gap-hunter.prompt"
        repo_text = (REPO_ROOT / "gap-hunter" / "biglobster-gap-hunter.prompt").read_text(encoding="utf-8")
        created = json.loads(cronjob(action="create", prompt="stale text", schedule="every 1h"))
        job_id = created["job_id"]

        # First sync establishes the baseline, and says it could not verify.
        first = json.loads(cronjob(action="sync_prompt", job_id=job_id, prompt_source=source))
        assert first["success"] is True
        assert first["changed"] is True
        assert "warning" in first, "a baseline-less sync must flag that it was unverified"

        # Someone edits the live prompt in place — the emergency fix.
        json.loads(cronjob(action="update", job_id=job_id, prompt=repo_text + "\n\nEMERGENCY FIX\n"))

        blocked = json.loads(cronjob(action="sync_prompt", job_id=job_id))
        assert blocked["success"] is False, "sync must refuse to overwrite a live-only edit"
        assert "EMERGENCY FIX" in get_job(job_id)["prompt"], "the live edit must survive a refused sync"

        # force is the deliberate override, and says what it destroyed.
        forced = json.loads(cronjob(action="sync_prompt", job_id=job_id, force=True))
        assert forced["success"] is True
        assert forced["changed"] is True
        assert "warning" in forced
        assert "EMERGENCY FIX" not in get_job(job_id)["prompt"]

    def test_sync_prompt_allows_repeat_syncs_when_live_is_untouched(self):
        """The guard must not block the normal case: nobody touched the live
        side, the repo moved on, sync again. Only a live-side edit blocks."""
        from cron.jobs import get_job

        source = "gap-hunter/biglobster-gap-hunter.prompt"
        created = json.loads(cronjob(action="create", prompt="stale text", schedule="every 1h"))
        job_id = created["job_id"]

        first = json.loads(cronjob(action="sync_prompt", job_id=job_id, prompt_source=source))
        assert first["success"] is True

        # Live drifts backwards the way a sync's own write never would, then is
        # restored to exactly what sync last wrote: still the recorded baseline.
        second = json.loads(cronjob(action="sync_prompt", job_id=job_id))
        assert second["success"] is True
        assert second["changed"] is False, "already matching is a no-op, not a refusal"
        assert get_job(job_id).get("prompt_synced_sha"), "baseline must be persisted"

    def test_sync_prompt_requires_a_source(self):
        created = json.loads(cronjob(action="create", prompt="x", schedule="every 1h"))
        job_id = created["job_id"]

        result = json.loads(cronjob(action="sync_prompt", job_id=job_id))
        assert result["success"] is False

    def test_sync_prompt_missing_file_errors(self):
        created = json.loads(cronjob(action="create", prompt="x", schedule="every 1h"))
        job_id = created["job_id"]

        result = json.loads(
            cronjob(action="sync_prompt", job_id=job_id, prompt_source="gap-hunter/does-not-exist.prompt")
        )
        assert result["success"] is False


class TestProfileRoutingGapWarning:
    """cronjob(action='create'/'update') should flag — not block — a
    profile-scoped job whose bare `deliver` platform has no per-profile
    routing override, since that job will silently ride the scheduler's
    global home channel/thread instead of the profile's own (the 2026-07-08
    BigLobster/FinView/Infographic thread-misroute pattern)."""

    @pytest.fixture(autouse=True)
    def _setup_cron_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
        monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
        monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
        # Upstream's gateway-liveness notice also writes "warning" when no
        # gateway runs (always, in tests); these tests are about the routing one.
        monkeypatch.setattr(
            "tools.cronjob_tools._gateway_liveness_notice",
            lambda plural=False: {"gateway_running": True},
        )
        root = tmp_path / "hermes-root"
        (root / "profiles" / "biglobster").mkdir(parents=True)
        # v2026.9.24: a dir is a profile only with an identity marker (SOUL.md is seeded by create).
        (root / "profiles" / "biglobster" / "SOUL.md").write_text("", encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(root))
        self.profile_root = root

    def test_create_warns_when_profile_has_no_routing_env(self):
        result = json.loads(
            cronjob(
                action="create",
                prompt="x",
                schedule="every 1h",
                deliver="telegram",
                profile="biglobster",
            )
        )
        assert result["success"] is True
        assert "warning" in result
        assert "biglobster" in result["warning"]

    def test_create_no_warning_when_profile_has_routing_env(self):
        (self.profile_root / "profiles" / "biglobster" / ".env").write_text(
            "TELEGRAM_HOME_CHANNEL=-1004224848555\nTELEGRAM_CRON_THREAD_ID=2\n",
            encoding="utf-8",
        )
        result = json.loads(
            cronjob(
                action="create",
                prompt="x",
                schedule="every 1h",
                deliver="telegram",
                profile="biglobster",
            )
        )
        assert result["success"] is True
        assert "warning" not in result

    def test_create_no_warning_without_profile(self):
        result = json.loads(
            cronjob(action="create", prompt="x", schedule="every 1h", deliver="telegram")
        )
        assert result["success"] is True
        assert "warning" not in result

    @pytest.mark.parametrize("deliver_value", ["local", "origin"])
    def test_create_no_warning_for_local_or_origin(self, deliver_value):
        result = json.loads(
            cronjob(
                action="create",
                prompt="x",
                schedule="every 1h",
                deliver=deliver_value,
                profile="biglobster",
            )
        )
        assert result["success"] is True
        assert "warning" not in result

    def test_update_warns_on_profile_routing_gap(self):
        created = json.loads(cronjob(action="create", prompt="x", schedule="every 1h"))
        result = json.loads(
            cronjob(
                action="update",
                job_id=created["job_id"],
                deliver="telegram",
                profile="biglobster",
            )
        )
        assert result["success"] is True
        assert "warning" in result
        assert "biglobster" in result["warning"]
