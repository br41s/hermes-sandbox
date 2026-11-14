"""Facebook Page and Instagram community inbox — bundled, auto-loaded.

Registers one tool, ``social_inbox``, into the ``social`` toolset. It replaces
the third-party service another bot used to read and answer comments: the
Graph API is spoken directly from ``plugins/shorts/social/meta.py``.

Three safety rails (see ``plugins/social/inbox.py``): replies stay in shadow
mode until ``SOCIAL_REPLY_MODE=live``, a comment is never answered twice (the
ledger remembers every id it touched), and a daily cap bounds how many live
replies a run can make.
"""

from __future__ import annotations

from plugins.social.inbox import (
    SOCIAL_INBOX_SCHEMA,
    check_social_available,
    handle_social_inbox,
)


def register(ctx) -> None:
    """Register the social inbox tool. Called once by the plugin loader."""
    ctx.register_tool(
        name="social_inbox",
        toolset="social",
        schema=SOCIAL_INBOX_SCHEMA,
        handler=handle_social_inbox,
        check_fn=check_social_available,
        emoji="💬",
    )
