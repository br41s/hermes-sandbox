"""Fetch a blog post and reduce it to the text a short may cite.

Used by ``shorts_studio submit`` to run the grounding check itself, so that
check never depends on the agent's own copy of the article, and by the
render farm (``build.gather_article_images``) for the article's pictures.
Standard library HTML parsing only. Every fetch goes through an SSRF guard:
Hermes's ``tools.url_safety`` where it is importable, and ``public_url``
below on the render farm (the Actions checkout has no ``tools/``), with the
same check on every redirect hop, so a public URL cannot 302 to a private
address.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from html import unescape
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

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


_OG_IMAGE = re.compile(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', re.I)
_COVER_IMG = re.compile(r'<div class="article-cover">.*?<img[^>]+src=["\']([^"\']+)["\']', re.I | re.S)
_INFOGRAPHIC = re.compile(r'<figure class="article-infographic">(.*?)</figure>', re.I | re.S)
_IMG_SRC = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
_IMG_ALT = re.compile(r'<img[^>]+alt=["\']([^"\']*)["\']', re.I)
_FIGCAPTION = re.compile(r'<figcaption>(.*?)</figcaption>', re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")


def article_images(html: str, base_url: str) -> Dict[str, Any]:
    """The article's own pictures: its cover, and its infographic when it has a raster one.

    The brand look (shorts/STUDIO.md §3.8, owner 2026-10-14: the picture is
    the article's, as in VideoDesk's shorts) puts the cover behind the cards
    and can show the infographic as a card. An inline SVG infographic is not
    returned: the renderer shows a picture file, never markup from a page.
    """
    html = html or ""
    cover: Optional[str] = None
    found = _OG_IMAGE.search(html) or _COVER_IMG.search(html)
    if found:
        cover = urljoin(base_url, unescape(found.group(1)).strip())
    infographic: Optional[Dict[str, str]] = None
    figure = _INFOGRAPHIC.search(html)
    if figure:
        src = _IMG_SRC.search(figure.group(1))
        if src:
            alt = _IMG_ALT.search(figure.group(1))
            caption = _FIGCAPTION.search(figure.group(1))
            infographic = {
                "url": urljoin(base_url, unescape(src.group(1)).strip()),
                "alt": unescape(alt.group(1)).strip() if alt else "",
                "caption": " ".join(_TAGS.sub("", unescape(caption.group(1))).split()) if caption else "",
            }
    return {"cover": cover, "infographic": infographic}


def public_url(url: str) -> bool:
    """The render farm's SSRF guard: https, and a host that resolves only to public addresses."""
    parts = urlsplit(url)
    host = (parts.hostname or "").strip().lower()
    if parts.scheme != "https" or not host or parts.username or parts.password:
        return False
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return _public_ip(literal)
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError):
        return False
    return bool(infos) and all(_public_ip(ipaddress.ip_address(info[4][0])) for info in infos)


def _public_ip(ip) -> bool:
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified)


def url_is_safe(url: str) -> bool:
    """Hermes's guard when it is importable (the tool side), ours otherwise (the render farm)."""
    try:
        from tools.url_safety import is_safe_url
    except Exception:  # the Actions checkout carries plugins/shorts only
        return public_url(url)
    return bool(is_safe_url(url))


def guard_redirect(response) -> None:
    """Re-check every redirect hop: a safe URL must not 302 us onto a private address."""
    if response.is_redirect and response.next_request:
        target = str(response.next_request.url)
        if not url_is_safe(target):
            raise ValueError(f"blocked redirect to a private address: {target}")


def fetch_html(url: str, *, client=None) -> str:
    """Fetch ``url`` (https, SSRF-guarded on both sides) and return its HTML. Raises ``ValueError``."""
    if not url.startswith("https://"):
        raise ValueError(f"article URL must be https: {url!r}")
    if not url_is_safe(url):
        raise ValueError(f"refusing to fetch unsafe article URL: {url!r}")

    import httpx

    own_client: Optional[httpx.Client] = None
    if client is None:
        own_client = client = httpx.Client(
            timeout=FETCH_TIMEOUT, follow_redirects=True,
            headers={"User-Agent": "BigLobster-Shorts/1.0 (+https://biglobster.top)"},
            event_hooks={"response": [guard_redirect]},
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
    return html


def fetch_article(url: str, *, client=None) -> tuple[str, str]:
    """Fetch ``url`` and return ``(title, text)``. Raises ``ValueError``."""
    title, text = html_to_text(fetch_html(url, client=client))
    if len(text) < 200:
        raise ValueError(f"the article at {url} has almost no readable text ({len(text)} chars)")
    return title, text
