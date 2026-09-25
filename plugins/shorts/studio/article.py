"""Fetch a blog post and reduce it to the text a short may cite.

Used by ``shorts_studio submit`` to run the grounding check itself, so that
check never depends on the agent's own copy of the article. Standard library
HTML parsing only; the network call goes through ``httpx`` and Hermes's SSRF
guard on the Hermes side.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import List, Optional

_SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "template"}
_BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th", "tr",
               "blockquote", "figcaption", "br", "div", "section", "article", "dd", "dt"}

MAX_ARTICLE_BYTES = 3 * 1024 * 1024
FETCH_TIMEOUT = 30.0


class _TextExtractor(HTMLParser):
    """Collect visible text, preferring the article body when there is one.

    Two buffers: everything visible, and everything inside the first
    ``<article>`` or ``class~=article-body`` element. The article buffer wins
    when it holds a real amount of text, so nav/related-post noise does not
    count as "the article said so".
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._article_depth = 0
        self._article_done = False
        self.all_parts: List[str] = []
        self.article_parts: List[str] = []
        self.title: str = ""
        self._in_title = False

    @staticmethod
    def _is_article(tag: str, attrs) -> bool:
        if tag == "article":
            return True
        classes = dict(attrs).get("class") or ""
        return "article-body" in classes.split()

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if tag == "title":
            self._in_title = True
        if self._article_depth:
            self._article_depth += 1
        elif not self._article_done and self._is_article(tag, attrs):
            self._article_depth = 1
        if tag in _BLOCK_TAGS:
            self._emit("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS:
            self._emit("\n")
        if self._article_depth:
            self._article_depth -= 1
            if not self._article_depth:
                self._article_done = True

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self._emit("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._skip:
            return
        self._emit(data)

    def _emit(self, text: str) -> None:
        self.all_parts.append(text)
        if self._article_depth:
            self.article_parts.append(text)


def _tidy(parts: List[str]) -> str:
    text = "".join(parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def html_to_text(html: str) -> tuple[str, str]:
    """Return ``(title, text)`` for an article page."""
    parser = _TextExtractor()
    parser.feed(html or "")
    parser.close()
    article = _tidy(parser.article_parts)
    text = article if len(article) >= 400 else _tidy(parser.all_parts)
    return parser.title.strip(), text


def fetch_article(url: str, *, client=None) -> tuple[str, str]:
    """Fetch ``url`` and return ``(title, text)``. Raises ``ValueError``."""
    try:
        from tools.url_safety import is_safe_url
    except Exception:  # pragma: no cover - render side never fetches articles
        is_safe_url = None
    if not url.startswith("https://"):
        raise ValueError(f"article URL must be https: {url!r}")
    if is_safe_url is not None and not is_safe_url(url):
        raise ValueError(f"refusing to fetch unsafe article URL: {url!r}")

    import httpx

    def _guard_redirect(response) -> None:
        # A public URL must not be able to 302 us onto a private address.
        if is_safe_url is not None and response.is_redirect and response.next_request:
            target = str(response.next_request.url)
            if not is_safe_url(target):
                raise ValueError(f"blocked redirect to a private address: {target}")

    own_client: Optional[httpx.Client] = None
    if client is None:
        own_client = client = httpx.Client(
            timeout=FETCH_TIMEOUT, follow_redirects=True,
            headers={"User-Agent": "BigLobster-Shorts/1.0 (+https://biglobster.top)"},
            event_hooks={"response": [_guard_redirect]},
        )
    try:
        response = client.get(url)
        response.raise_for_status()
        body = response.content[:MAX_ARTICLE_BYTES]
        encoding = response.encoding or "utf-8"
        html = body.decode(encoding, errors="replace")
    except httpx.HTTPError as exc:
        raise ValueError(f"could not fetch the article {url}: {exc}") from exc
    finally:
        if own_client is not None:
            own_client.close()

    title, text = html_to_text(html)
    if len(text) < 200:
        raise ValueError(f"the article at {url} has almost no readable text ({len(text)} chars)")
    return title, text
