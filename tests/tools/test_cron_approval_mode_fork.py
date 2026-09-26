"""Fork's own tests for approvals.cron_mode, kept out of upstream's file so upstream merges do not conflict."""

import pytest

import tools.approval as approval_module
from tools.approval import check_all_command_guards


@pytest.fixture(autouse=True)
def _clear_approval_state():
    approval_module._permanent_approved.clear()
    approval_module.clear_session("default")
    approval_module.clear_session("test-session")
    yield
    approval_module._permanent_approved.clear()
    approval_module.clear_session("default")
    approval_module.clear_session("test-session")


class TestCronInsideGatewayProcess:
    """Regression: cron jobs run INSIDE the gateway process, which sets
    HERMES_EXEC_ASK=1 process-wide (gateway/run.py). Cron approvals must be
    governed by approvals.cron_mode even when HERMES_EXEC_ASK is set — otherwise
    a flagged command falls through to the ask-approval branch, submits a
    pending approval with no listener, and the job stalls silently with no
    last_error (Langfuse trace 1d29bc22…: a benign `python3 -c` image-size check
    matched the "script execution via -e/-c flag" pattern and went
    pending_approval).

    These tests deliberately KEEP HERMES_EXEC_ASK set — the prior
    TestCronWithGatewayOrigin tests cleared it, which masked the bug.
    """

    def test_cron_deny_blocks_flagged_command_with_exec_ask_set(self, monkeypatch):
        """cron_mode=deny + HERMES_EXEC_ASK=1 → BLOCKED, never pending_approval."""
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
        monkeypatch.setenv("HERMES_EXEC_ASK", "1")  # gateway sets this process-wide
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        from unittest.mock import patch as mock_patch
        with mock_patch("tools.approval._get_cron_approval_mode", return_value="deny"):
            # The exact command shape from the incident trace: a benign
            # `python3 -c` that matches "script execution via -e/-c flag".
            result = check_all_command_guards(
                "python3 -c \"from PIL import Image; print(Image.open('x.png').size)\"",
                "local",
            )
            assert not result["approved"]
            assert "BLOCKED" in result["message"]
            # Must NOT stall waiting for an approval no one can grant.
            assert result.get("status") != "pending_approval"
            assert not result.get("approval_pending")

    def test_cron_approve_passes_through_with_exec_ask_set(self, monkeypatch):
        """cron_mode=approve + HERMES_EXEC_ASK=1 → command runs, no pending_approval."""
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
        monkeypatch.setenv("HERMES_EXEC_ASK", "1")
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        from unittest.mock import patch as mock_patch
        with mock_patch("tools.approval._get_cron_approval_mode", return_value="approve"):
            result = check_all_command_guards("rm -rf /tmp/stuff", "local")
            assert result["approved"]
            assert result.get("status") != "pending_approval"
            assert not result.get("approval_pending")

    def test_cron_deny_allows_safe_command_with_exec_ask_set(self, monkeypatch):
        """A non-dangerous command still runs under cron_mode=deny + exec_ask."""
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
        monkeypatch.setenv("HERMES_EXEC_ASK", "1")
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        from unittest.mock import patch as mock_patch
        with mock_patch("tools.approval._get_cron_approval_mode", return_value="deny"):
            result = check_all_command_guards("ls -la", "local")
            assert result["approved"]
