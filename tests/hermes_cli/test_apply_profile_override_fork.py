"""Fork: the global -p/--profile selector stops at the subcommand.

Moved out of test_apply_profile_override.py so upstream's file merges clean.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


class TestApplyProfileOverrideSubcommandBoundary:
    """A subcommand's own --profile must not be hijacked by the global selector.

    The global -p/--profile selector applies only BEFORE the subcommand. A
    --profile that appears after the subcommand belongs to that subcommand
    (e.g. `hermes cron edit <id> --profile <name>` sets the cron job's run
    profile) and must NOT switch the CLI's HERMES_HOME — that previously made
    the job lookup read the wrong (empty) store and fail with "Job not found".
    """

    def test_profile_after_subcommand_is_not_hijacked(self, tmp_path, monkeypatch):
        """`hermes cron edit <id> --profile coder` must leave HERMES_HOME and
        argv untouched, so the flag reaches the cron subparser."""
        # A real coder profile exists — proving we still don't switch to it.
        (tmp_path / ".hermes" / "profiles" / "coder").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".hermes" / "profiles" / "coder" / "config.yaml").write_text("{}\n")  # identity marker
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        argv = ["hermes", "cron", "edit", "abc123", "--profile", "coder"]
        monkeypatch.setattr(sys, "argv", list(argv))

        from hermes_cli.main import _apply_profile_override
        _apply_profile_override()

        assert os.environ.get("HERMES_HOME") is None, (
            "post-subcommand --profile must not switch HERMES_HOME"
        )
        assert sys.argv == argv, (
            "post-subcommand --profile must remain in argv for the subparser"
        )

    def test_global_profile_before_subcommand_still_applies(self, tmp_path, monkeypatch):
        """`hermes -p coder cron list` must still switch HERMES_HOME to coder
        and strip the flag from argv."""
        (tmp_path / ".hermes" / "profiles" / "coder").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".hermes" / "profiles" / "coder" / "config.yaml").write_text("{}\n")  # identity marker
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(sys, "argv", ["hermes", "-p", "coder", "cron", "list"])

        from hermes_cli.main import _apply_profile_override
        _apply_profile_override()

        result = os.environ.get("HERMES_HOME")
        assert result is not None and result.endswith("coder"), (
            f"global -p before subcommand must switch HERMES_HOME, got {result!r}"
        )
        assert sys.argv == ["hermes", "cron", "list"], (
            f"global -p/value must be stripped from argv, got {sys.argv!r}"
        )

    def test_profile_subcommand_word_is_not_a_flag(self, tmp_path, monkeypatch):
        """`hermes profile use coder` — the bare 'profile' subcommand must not
        be confused with the --profile flag, and must not switch HERMES_HOME."""
        (tmp_path / ".hermes" / "profiles" / "coder").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".hermes" / "profiles" / "coder" / "config.yaml").write_text("{}\n")  # identity marker
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        argv = ["hermes", "profile", "use", "coder"]
        monkeypatch.setattr(sys, "argv", list(argv))

        from hermes_cli.main import _apply_profile_override
        _apply_profile_override()

        assert os.environ.get("HERMES_HOME") is None
        assert sys.argv == argv

    def test_global_profile_after_top_level_value_flag_still_applies(
        self, tmp_path, monkeypatch
    ):
        """`hermes -m <model> -p coder chat` — a top-level flag's VALUE is not
        the subcommand, so the scan must skip it and still find -p."""
        (tmp_path / ".hermes" / "profiles" / "coder").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".hermes" / "profiles" / "coder" / "config.yaml").write_text("{}\n")  # identity marker
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(sys, "argv", ["hermes", "-m", "some/model", "-p", "coder", "chat"])

        from hermes_cli.main import _apply_profile_override
        _apply_profile_override()

        result = os.environ.get("HERMES_HOME")
        assert result is not None and result.endswith("coder")
        assert sys.argv == ["hermes", "-m", "some/model", "chat"]
