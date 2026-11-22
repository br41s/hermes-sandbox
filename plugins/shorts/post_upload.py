"""The text check the social inbox uses for a comment or reply.

``_text`` refuses what a bot must never post — an e-mail address, a link that is not an
https ``biglobster.top`` one, control characters — and normalises the whitespace. The
SEODesk executor that applied ``#actualizar-video`` requests (translations, captions,
playlists, comments, replies, the quota budget) was removed on 2026-11-22 with the Buzz
fleet.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

BARE_DOMAIN = re.compile(r"(?i)^[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:[/:?#].*)?$")
SCHEME = re.compile(r"(?i)^(?:[a-z][a-z0-9+.-]*://|www\.|(?:mailto|javascript|data|tel|ftp|file):)")
TRIM = ".,;:!?¡¿()[]{}\"'«»“”‘’*"


def _link_problem(text: str) -> str:
    """Why ``text`` may not be posted: it links anywhere but biglobster.top, or carries an e-mail address."""
    for raw in text.split():
        token = raw.strip(TRIM)
        if re.search(r"(?i)@[^@\s]+\.[a-z]{2,}", token):
            return f"{token}: no e-mail addresses"
        if not (SCHEME.match(token) or BARE_DOMAIN.match(token)):
            continue
        try:
            parts = urlparse(token if "://" in token else f"https://{token}")
            host = (parts.hostname or "").lower()
            port = parts.port
        except ValueError:
            return f"{token}: only https links on biglobster.top"
        if (parts.scheme != "https" or parts.username or port or
                not (host == "biglobster.top" or host.endswith(".biglobster.top"))):
            return f"{token}: only https links on biglobster.top"
    return ""


def _text(value: Any, what: str, limit: int, problems: list[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{what}: a text")
        return ""
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").strip().split("\n")))
    if len(text) > limit:
        problems.append(f"{what}: up to {limit} characters")
    elif any(ord(c) < 32 and c != "\n" for c in text):
        problems.append(f"{what}: control characters")
    elif problem := _link_problem(text):
        problems.append(f"{what}: {problem}")
    return text
