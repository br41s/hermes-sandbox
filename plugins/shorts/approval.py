"""Approvals for irreversible steps are read from the gateway's own message
store, never from the agent's word: an agent that would publish without
approval would also claim it had approval. Only a user message counts; the
Telegram adapter only admits allowlisted senders (TELEGRAM_ALLOWED_USERS /
group allowlists), which is what makes a user message the owner's.

Built for the Flow shorts; meant for the Prospector send gate too.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def find(pattern: str, *, chat_id: str, after: float, db_path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """The first owner message matching ``pattern`` in the chat, after ``after``; None when unheard.

    Reads the profile's state.db read-only; a missing or unreadable database is
    "no approval", never an exception.
    """
    if db_path is None:
        from hermes_constants import get_hermes_home

        db_path = get_hermes_home() / "state.db"
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT m.content, m.timestamp, s.id FROM messages m JOIN sessions s ON s.id = m.session_id"
                " WHERE s.source = 'telegram' AND s.chat_id = ? AND m.role = 'user' AND m.timestamp > ?"
                " ORDER BY m.timestamp",
                (str(chat_id), after),
            )
            rx = re.compile(pattern, re.IGNORECASE)
            for content, timestamp, session_id in rows:
                if content and rx.search(content):
                    return {"text": content[:300], "at": timestamp, "session_id": session_id}
            return None
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("approval check on %s failed: %s", db_path, exc)
        return None


def phrase(verb: str, target: str) -> str:
    """The regex for "<verb> <target>" as a whole phrase.

    Neither side may continue into another word or hyphenated piece, so
    "publica 2026-11-14_es-seo" does NOT match target "2026-11-14_es-seo-local".
    """
    return rf"(?<![\w-]){re.escape(verb)}\s+{re.escape(target)}(?![\w-])"
