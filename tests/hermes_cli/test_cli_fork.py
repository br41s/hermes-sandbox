"""Fork's own tests for cli.py, kept out of upstream's test files so upstream merges do not conflict.

Regression: an old upstream merge left copies of the /curator, /kanban and
/skills handlers inside HermesCLI after upstream had moved them into
CLICommandsMixin. HermesCLI lists itself first in the MRO, so the stale copies
silently won — and the /skills copy predated write approval, so
``/skills pending|approve|reject|diff|mode`` fell through to the skills hub
instead of the approval queue. The handlers must come from the mixin.
"""

from unittest.mock import patch

import pytest

from cli import HermesCLI
from hermes_cli.cli_commands_mixin import CLICommandsMixin


@pytest.mark.parametrize("name", [
    "_handle_curator_command",
    "_handle_kanban_command",
    "_handle_skills_command",
])
def test_slash_handlers_come_from_the_mixin(name):
    assert getattr(HermesCLI, name) is getattr(CLICommandsMixin, name), (
        f"HermesCLI overrides {name}; upstream's CLICommandsMixin version must win"
    )


def test_skills_pending_reaches_the_write_approval_queue():
    cli = HermesCLI.__new__(HermesCLI)  # handler needs no instance state
    with (
        patch("hermes_cli.write_approval_commands.handle_pending_subcommand",
              return_value="queue") as pending,
        patch("hermes_cli.skills_hub.handle_skills_slash") as hub,
    ):
        cli._handle_skills_command("/skills pending")
    pending.assert_called_once()
    hub.assert_not_called()
