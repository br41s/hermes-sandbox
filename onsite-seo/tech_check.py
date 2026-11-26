#!/usr/bin/env python3
"""Weekly technical SEO check for biglobster.top.

Runs with no agent and no model: Hermes cron executes this file by path and
delivers its stdout to a Telegram topic. Empty stdout means nothing is sent, so
the script reports only the CHANGE since last week — new findings in full, plus
counts of the ones that persist and the ones that got resolved.

It checks, against the live site:
  * robots.txt: status, blocked bots and the Sitemap line;
  * the sitemap (following a sitemapindex, at most two levels) against the live
    pages: status, redirects, title/description/canonical/robots, soft 404s and
    duplicate titles;
  * title and meta description length outside the recommended window;
  * JSON-LD blocks that do not parse;
  * target=_blank links without rel=noopener/noreferrer;
  * pages whose <main> carries too little text;
  * orphan pages no other sitemap page links to, ignoring language twins;
  * hreflang: alternates that do not link back and broken targets;
  * llms.txt: status and links outside the sitemap that do not answer 200;
  * PageSpeed (mobile) for a few key pages, field data first, lab as fallback.
  * PageSpeed's LCP image audit: loaded late or without priority.

WHY THIS EXISTS: the cron sandbox forbids `python -c`, and the folder name has
a hyphen (not importable as a module), so the job invokes this committed script
by path: `.venv/bin/python onsite-seo/tech_check.py`.

State lives in `$HERMES_HOME/workspace/seo-tech/estado.json` and is written
atomically. A missing or corrupt state file starts from empty. The sitemap
(and robots.txt) being unreachable is fatal: the reason goes to stderr and the
exit code is 1 so Hermes delivers it as an alert.
"""
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

SITE = "https://biglobster.top"
# The site's CDN answers 403 to custom bot user-agents (seen 2026-10-27), so
# present as a current desktop Chrome.
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)
TIMEOUT = 20
WORKERS = 4
PSI_PATHS = ["/", "/es/", "/agents-for-rent.html", "/es/agentes-en-alquiler.html", "/big.html"]
BOTS = [
    "Googlebot",
    "Bingbot",
    "Applebot",
    "GPTBot",
    "OAI-SearchBot",
    "ChatGPT-User",
    "ClaudeBot",
    "Claude-SearchBot",
    "PerplexityBot",
]
MAX_LISTED = 30
PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

TITLE_MIN = 30
TITLE_MAX = 65
DESC_MIN = 70
DESC_MAX = 160
THIN_MAIN_WORDS = 150
HOME_PATHS = ("/", "/es/")  # entry points, never reported as orphans
# Tags whose text never counts as <main> copy.
_MAIN_SKIP_TAGS = ("script", "style", "noscript", "template", "svg")

_MD_LINK_RE = re.compile(r"\]\(\s*(https?://[^)\s]+)")
_BARE_LINK_RE = re.compile(re.escape(SITE) + r"/[^\s)\]<>\"']+")
# The site carries prices such as "1.404 €" in titles, so "404" only counts as
# a soft-404 marker when it is not part of a number.
_SOFT404_TITLE_RE = re.compile(r"(?<![\d.,])404(?![\d.,])|no encontrad[oa]|not found", re.IGNORECASE)


class FatalCheckError(Exception):
    """A check that cannot proceed: the sitemap is unreadable/unparseable."""


def _state_path():
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "workspace" / "seo-tech" / "estado.json"


def load_state(path):
    """Missing file = empty state. Corrupt file = empty state plus a warning."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {"hallazgos": {}}
    except (OSError, ValueError) as exc:
        print(f"AVISO: estado ilegible en {path}: {type(exc).__name__}", file=sys.stderr)
        return {"hallazgos": {}}
    if not isinstance(data, dict) or not isinstance(data.get("hallazgos", {}), dict):
        print(f"AVISO: estado ilegible en {path}: forma inesperada", file=sys.stderr)
        return {"hallazgos": {}}
    return data


def save_state(path, state):
    """Atomic write: tmp file then os.replace."""
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def fetch(client, url):
    """One GET (no redirect following). Retry once after 3 s on transport errors.

    Returns ``(status, headers, text)``. A failed retry is status 0 with the
    error class in the text, so callers never have to catch httpx errors.
    Single choke point: only https URLs on this site are fetched. Every URL here comes from the
    site's own robots/sitemap/HTML, but the pod shares a network with other services.
    """
    if urlsplit(url).scheme != "https" or not _same_site(url):
        return 0, {}, "fuera del sitio"
    error = "TransportError"
    for attempt in range(2):
        try:
            resp = client.get(url)
            return resp.status_code, resp.headers, resp.text
        except httpx.TransportError as exc:
            error = type(exc).__name__
            if attempt == 0:
                time.sleep(3)
    return 0, {}, error


def _fetch_pages(client, urls):
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(lambda u: (u, *fetch(client, u)), urls))


def _header(headers, name):
    if not headers:
        return None
    value = headers.get(name)
    if value is not None:
        return value
    for key, val in headers.items():
        if key.lower() == name.lower():
            return val
    return None


class _PageParser(HTMLParser):
    """Collect the HTML basics the checks need from one page."""

    def __init__(self):
        super().__init__()
        self.title = ""
        self.description = None
        self.canonical = None
        self.robots = ""
        self.alternates = []
        self.links = []
        self.jsonld = []
        self._in_title = False
        self._title_done = False
        self._svg_depth = 0
        self._in_jsonld = False
        self._jsonld_parts = []
        self._has_main = False
        self._main_depth = 0
        self._main_skip = 0
        self._main_parts = []

    def handle_starttag(self, tag, attrs):
        values = {key.lower(): (val or "") for key, val in attrs}
        if tag == "svg":
            self._svg_depth += 1
        elif tag == "title":
            if self._svg_depth == 0 and not self._title_done:
                self._in_title = True
        elif tag == "meta":
            name = values.get("name", "").lower()
            if name == "description" and self.description is None:
                self.description = values.get("content", "")
            elif name == "robots":
                self.robots = values.get("content", "")
        elif tag == "link":
            rels = values.get("rel", "").lower().split()
            if "canonical" in rels and self.canonical is None:
                self.canonical = values.get("href", "")
            elif "alternate" in rels:
                lang = values.get("hreflang")
                href = values.get("href")
                if lang and href:
                    self.alternates.append((lang, href))
        elif tag == "a":
            href = values.get("href", "")
            if href:
                self.links.append(
                    (href, values.get("target", ""), values.get("rel", ""))
                )
        elif tag == "script":
            if values.get("type", "").strip().lower() == "application/ld+json":
                self._in_jsonld = True
                self._jsonld_parts = []
        elif tag == "main":
            self._has_main = True
            self._main_depth += 1
        if tag in _MAIN_SKIP_TAGS and self._main_depth:
            self._main_skip += 1

    def handle_endtag(self, tag):
        if tag == "svg":
            if self._svg_depth:
                self._svg_depth -= 1
        elif tag == "title" and self._in_title:
            self._in_title = False
            self._title_done = True
        elif tag == "script" and self._in_jsonld:
            self._in_jsonld = False
            self.jsonld.append("".join(self._jsonld_parts))
            self._jsonld_parts = []
        elif tag == "main":
            if self._main_depth:
                self._main_depth -= 1
        if tag in _MAIN_SKIP_TAGS and self._main_skip:
            self._main_skip -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_jsonld:
            self._jsonld_parts.append(data)
        if self._main_depth and not self._main_skip:
            self._main_parts.append(data)


def parse_page(html):
    parser = _PageParser()
    parser.feed(html)
    main_words = None
    if parser._has_main:
        main_words = len(re.findall(r"\w+", "".join(parser._main_parts)))
    return {
        "title": parser.title.strip(),
        "description": parser.description,
        "canonical": parser.canonical,
        "robots": parser.robots,
        "alternates": parser.alternates,
        "body_len": len(html.encode("utf-8")),
        "links": parser.links,
        "jsonld": parser.jsonld,
        "main_words": main_words,
    }


def normalize_url(url):
    """Lower-case host, drop a trailing index.html and treat /es as /es/."""
    parts = urlsplit(url.strip())
    path = parts.path or "/"
    if path.endswith("index.html"):
        path = path[: -len("index.html")]
        if not path.startswith("/"):
            path = "/" + path
    if path == "/es":
        path = "/es/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def check_page(url, status, headers, page):
    """Findings for one sitemap URL, from already-fetched data."""
    findings = []
    if 300 <= status < 400:
        location = _header(headers, "location") or "(sin Location)"
        findings.append(
            (f"pagina:redirige:{url}", f"Redirige {status} a {_limpio(location)}: {url}")
        )
        return findings
    if status != 200:
        shown = status if status else "error de red"
        findings.append((f"pagina:status:{url}", f"Responde {shown}: {url}"))
        return findings

    title = page.get("title", "")
    description = page.get("description")
    canonical = page.get("canonical")
    if not title:
        findings.append((f"pagina:sin-title:{url}", f"Sin <title>: {url}"))
    if not description:
        findings.append((f"pagina:sin-description:{url}", f"Sin meta description: {url}"))
    if not canonical or normalize_url(canonical) != normalize_url(url):
        findings.append((f"pagina:canonical:{url}", f"Canonical ausente o distinta: {url}"))
    if "noindex" in (page.get("robots") or "").lower():
        findings.append((f"pagina:noindex:{url}", f"Noindex en el sitemap: {url}"))

    body_len = page.get("body_len", 0)
    soft_title = bool(_SOFT404_TITLE_RE.search(title))
    if body_len < 3000:
        findings.append(
            (f"pagina:soft404:{url}", f"Soft 404 (cuerpo de {body_len} bytes): {url}")
        )
    elif soft_title:
        findings.append((f"pagina:soft404:{url}", f"Soft 404 (título \"{_limpio(title)}\"): {url}"))
    return findings


def check_duplicate_titles(pages):
    """One finding per repeated non-empty title, naming up to three URLs."""
    groups = {}
    order = []
    for url, title in pages:
        title = (title or "").strip()
        if not title:
            continue
        if title not in groups:
            groups[title] = []
            order.append(title)
        groups[title].append(url)
    findings = []
    for title in order:
        urls = groups[title]
        if len(urls) < 2:
            continue
        shown = ", ".join(urls[:3])
        findings.append(
            (
                f"titulo-duplicado:{_limpio(title, 80)}",
                f"Título duplicado en {len(urls)} páginas ({shown})",
            )
        )
    return findings


def check_lengths(url, page):
    """Title and meta description outside the recommended length window."""
    findings = []
    title = page.get("title") or ""
    if title:
        n = len(title)
        if n < TITLE_MIN:
            findings.append(
                (
                    f"pagina:titulo-corto:{url}",
                    f"Título corto ({n} caracteres, mín. {TITLE_MIN}): {url}",
                )
            )
        elif n > TITLE_MAX:
            findings.append(
                (
                    f"pagina:titulo-largo:{url}",
                    f"Título largo ({n} caracteres, máx. {TITLE_MAX}): {url}",
                )
            )
    description = (page.get("description") or "").strip()
    if description:
        n = len(description)
        if n < DESC_MIN:
            findings.append(
                (
                    f"pagina:description-corta:{url}",
                    f"Meta description corta ({n} caracteres, mín. {DESC_MIN}): {url}",
                )
            )
        elif n > DESC_MAX:
            findings.append(
                (
                    f"pagina:description-larga:{url}",
                    f"Meta description larga ({n} caracteres, máx. {DESC_MAX}): {url}",
                )
            )
    return findings


def check_jsonld(url, page):
    """First JSON-LD block that does not parse. Content rules are not checked."""
    for i, block in enumerate(page.get("jsonld") or [], 1):
        try:
            json.loads(block)
        except ValueError as exc:
            return [
                (
                    f"jsonld:invalido:{url}",
                    f"JSON-LD ilegible (bloque {i}, {type(exc).__name__}): {url}",
                )
            ]
    return []


def check_blank_links(url, page):
    """target=_blank links that carry neither noopener nor noreferrer in rel."""
    bare = []
    for href, target, rel in page.get("links") or []:
        if target.strip().lower() != "_blank":
            continue
        tokens = rel.lower().split()
        if "noopener" in tokens or "noreferrer" in tokens:
            continue
        bare.append(href)
    if not bare:
        return []
    return [
        (
            f"enlace:sin-noopener:{url}",
            f"{len(bare)} enlace(s) target=_blank sin rel=noopener en {url} "
            f"(p. ej. {_limpio(bare[0], 80)})",
        )
    ]


def check_thin(url, page):
    """Pages with a <main> but too little copy inside it. No <main>, no finding."""
    words = page.get("main_words")
    if words is None or words >= THIN_MAIN_WORDS:
        return []
    return [
        (
            f"pagina:poco-texto:{url}",
            f"Poco texto en <main> ({words} palabras, mín. {THIN_MAIN_WORDS}): {url}",
        )
    ]


def check_orphans(parsed_pages):
    """Sitemap pages no other sitemap page links to, ignoring language twins.

    A link from S to T does not count when T is one of S's hreflang alternates or
    S is one of T's: every page reaches its translation through the language
    switcher, which otherwise hides real orphans.
    """
    by_normalized = {}
    for url in parsed_pages:
        by_normalized.setdefault(normalize_url(url), url)
    alternates = {
        url: {normalize_url(href) for _lang, href in page.get("alternates") or []}
        for url, page in parsed_pages.items()
    }
    inbound = {url: set() for url in parsed_pages}
    for source, page in parsed_pages.items():
        for href, _target, _rel in page.get("links") or []:
            resolved = urlsplit(urljoin(source, href))
            clean = urlunsplit((resolved.scheme, resolved.netloc, resolved.path, "", ""))
            if resolved.scheme != "https" or not _same_site(clean):
                continue
            target = by_normalized.get(normalize_url(clean))
            if target is None or target == source:
                continue
            if (normalize_url(target) in alternates[source]
                    or normalize_url(source) in alternates[target]):
                continue
            inbound[target].add(source)
    findings = []
    for url in parsed_pages:
        if urlsplit(normalize_url(url)).path in HOME_PATHS:
            continue
        if inbound[url]:
            continue
        findings.append(
            (
                f"pagina:huerfana:{url}",
                f"Huérfana: ninguna página del sitemap la enlaza "
                f"(sin contar su par de idioma): {url}",
            )
        )
    return findings


def parse_robots(text):
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    sitemaps = []
    for line in text.splitlines():
        if line.strip().lower().startswith("sitemap:"):
            sitemaps.append(line.split(":", 1)[1].strip())
    return parser, sitemaps


def check_robots(status, text):
    """Return ``(findings, sitemap_urls)`` for robots.txt."""
    findings = []
    if status != 200:
        findings.append(
            ("robots:status", f"robots.txt no responde 200 ({status}): {SITE}/robots.txt")
        )
    parser, sitemaps = parse_robots(text)
    for bot in BOTS:
        if not parser.can_fetch(bot, SITE + "/"):
            findings.append(
                (f"robots:bloquea:{bot}", f"robots.txt bloquea a {bot}: {SITE}/robots.txt")
            )
    if not sitemaps:
        findings.append(
            ("robots:sin-sitemap", f"robots.txt no declara Sitemap: {SITE}/robots.txt")
        )
    return findings, sitemaps


def _local_name(tag):
    return tag.rsplit("}", 1)[-1]


def parse_sitemap_xml(text):
    """Return ``(kind, locs)`` where kind is ``urlset`` or ``sitemapindex``.

    Namespaced and un-namespaced documents parse the same way.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise FatalCheckError(f"XML inválido: {exc}") from exc
    kind = _local_name(root.tag)
    if kind not in ("sitemapindex", "urlset"):
        raise FatalCheckError(f"raíz de sitemap desconocida: {root.tag}")
    locs = []
    for child in root:
        if _local_name(child.tag) not in ("sitemap", "url"):
            continue
        for loc in child:
            if _local_name(loc.tag) == "loc" and loc.text and loc.text.strip():
                locs.append(loc.text.strip())
    return kind, locs


def collect_sitemap_urls(client, root_url):
    """Flatten a sitemap (or sitemapindex) into page URLs, deduped in order."""
    urls = []
    seen = set()

    def add(url):
        if url not in seen:
            seen.add(url)
            urls.append(url)

    def walk(url, depth):
        status, _headers, text = fetch(client, url)
        if status != 200:
            raise FatalCheckError(f"no se pudo leer {url} (status {status})")
        kind, locs = parse_sitemap_xml(text)
        if kind == "urlset":
            for loc in locs:
                add(loc)
            return
        if depth >= 2:
            raise FatalCheckError(f"demasiados niveles de sitemapindex en {url}")
        for loc in locs:
            walk(loc, depth + 1)

    walk(root_url, 0)
    return urls


def hreflang_without_return(parsed_alternates):
    """Alternate targets that do not link back to the source page.

    Google's hreflang rules require reciprocity: if A points at B, B must point
    back at A. ``parsed_alternates`` maps each 200 page URL to its hreflang hrefs.
    """
    by_normalized = {}
    for url in parsed_alternates:
        by_normalized.setdefault(normalize_url(url), url)
    findings = []
    seen = set()
    for source, targets in parsed_alternates.items():
        for target in targets:
            page = by_normalized.get(normalize_url(target))
            if page is None or normalize_url(target) == normalize_url(source):
                continue
            pair = (page, source)
            if pair in seen:
                continue
            seen.add(pair)
            back_links = {
                normalize_url(href) for href in parsed_alternates.get(page, [])
            }
            if normalize_url(source) in back_links:
                continue
            findings.append(
                (
                    f"hreflang:sin-retorno:{page}:{source}",
                    f"hreflang sin retorno: {page} no enlaza de vuelta a {source}",
                )
            )
    return findings


def unlisted_hreflang_targets(alternates, sitemap_urls):
    """Unique ``(source, target)`` pairs whose target is not in the sitemap."""
    sitemap_set = set(sitemap_urls)
    seen = set()
    pairs = []
    for source, targets in alternates.items():
        for target in targets:
            pair = (source, target)
            if target in sitemap_set or pair in seen:
                continue
            seen.add(pair)
            pairs.append(pair)
    return pairs


def broken_hreflang(pairs, statuses):
    return [
        (
            f"hreflang:roto:{source}:{target}",
            f"hreflang roto ({statuses.get(target, 0)}): {source} → {target}",
        )
        for source, target in pairs
        if statuses.get(target) != 200
    ]


def _limpio(texto, tope=120):
    """Remote text (a title, a Location header) as one short line for the Telegram report."""
    return " ".join(str(texto).split())[:tope]


def _same_site(url):
    parts = urlsplit(url)
    return parts.netloc.lower() == urlsplit(SITE).netloc.lower()


def extract_llms_links(text):
    """Absolute same-site URLs from markdown links and bare URLs, deduped."""
    urls = []
    seen = set()

    def add(url):
        url = url.rstrip(".,;")
        if url not in seen and _same_site(url):
            seen.add(url)
            urls.append(url)

    for match in _MD_LINK_RE.finditer(text):
        add(match.group(1))
    for match in _BARE_LINK_RE.finditer(text):
        add(match.group(0))
    return urls


def check_llms(status, links, sitemap_urls, statuses):
    if status != 200:
        return [("llms:status", f"llms.txt no responde 200 ({status}): {SITE}/llms.txt")]
    sitemap_set = set(sitemap_urls)
    findings = []
    for url in links:
        if url in sitemap_set:
            continue
        if statuses.get(url) != 200:
            findings.append(
                (f"llms:enlace:{url}", f"Enlace roto en llms.txt ({statuses.get(url, 0)}): {url}")
            )
    return findings


def _metric_percentile(metrics, name):
    entry = metrics.get(name)
    if not isinstance(entry, dict):
        return None
    value = entry.get("percentile")
    return value if isinstance(value, (int, float)) else None


def _audit_value(audits, name):
    entry = audits.get(name)
    if not isinstance(entry, dict):
        return None
    value = entry.get("numericValue")
    return value if isinstance(value, (int, float)) else None


def check_psi(url, data):
    """Google's "poor" thresholds: field data first, lab data as fallback."""
    data = data or {}
    metrics = (data.get("loadingExperience") or {}).get("metrics") or {}
    findings = []
    audits = (data.get("lighthouseResult") or {}).get("audits") or {}
    # lighthouseResult is present with or without field data. The first id is the
    # legacy Lighthouse audit, the second its replacement: whichever exists.
    for audit_id in ("lcp-lazy-loaded", "lcp-discovery-insight"):
        audit = audits.get(audit_id)
        if not isinstance(audit, dict):
            continue
        score = audit.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            continue
        if score < 0.9:
            findings.append(
                (
                    f"psi:lcp-descubrimiento:{url}",
                    f"Imagen LCP tardía o sin prioridad en {url} (PageSpeed: {audit_id})",
                )
            )
            break
    if metrics:
        lcp = _metric_percentile(metrics, "LARGEST_CONTENTFUL_PAINT_MS")
        if lcp is not None and lcp > 4000:
            findings.append((f"psi:lcp:{url}", f"LCP {lcp:.0f} ms en {url} (campo)"))
        cls = _metric_percentile(metrics, "CUMULATIVE_LAYOUT_SHIFT_SCORE")
        if cls is not None and cls / 100 > 0.25:
            findings.append((f"psi:cls:{url}", f"CLS {cls / 100:.2f} en {url} (campo)"))
        inp = _metric_percentile(metrics, "INTERACTION_TO_NEXT_PAINT")
        if inp is not None and inp > 500:
            findings.append((f"psi:inp:{url}", f"INP {inp:.0f} ms en {url} (campo)"))
        return findings

    lcp = _audit_value(audits, "largest-contentful-paint")
    if lcp is not None and lcp > 4000:
        findings.append(
            (f"psi:lcp:{url}", f"LCP {lcp:.0f} ms en {url} (laboratorio: varía entre pasadas)")
        )
    cls = _audit_value(audits, "cumulative-layout-shift")
    if cls is not None and cls > 0.25:
        findings.append(
            (f"psi:cls:{url}", f"CLS {cls:.2f} en {url} (laboratorio: varía entre pasadas)")
        )
    tbt = _audit_value(audits, "total-blocking-time")
    if tbt is not None and tbt > 600:
        findings.append(
            (f"psi:tbt:{url}", f"TBT {tbt:.0f} ms en {url} (laboratorio: varía entre pasadas)")
        )
    return findings


def fetch_psi(url):
    """Return ``(data, None)`` or ``(None, note_error)``.

    Separate client with a 120 s timeout and redirects. Retry once after 10 s on
    a transport error or an HTTP 5xx. Never echo the request URL (it may carry
    the API key) — only the site URL and a status/error class reach the output.
    """
    params = {"url": url, "strategy": "mobile", "category": "performance"}
    key = os.environ.get("PSI_API_KEY")
    if key:
        params["key"] = key
    error = "error"
    for attempt in range(2):
        try:
            with httpx.Client(timeout=120, follow_redirects=True) as client:
                resp = client.get(PSI_ENDPOINT, params=params)
        except httpx.TransportError as exc:
            error = type(exc).__name__
            if attempt == 0:
                time.sleep(10)
                continue
            return None, error
        if resp.status_code == 429:
            return None, "429"
        if resp.status_code >= 500:
            error = str(resp.status_code)
            if attempt == 0:
                time.sleep(10)
                continue
            return None, error
        if resp.status_code != 200:
            return None, str(resp.status_code)
        try:
            return resp.json(), None
        except ValueError:
            return None, "JSONDecodeError"
    return None, error


def _check_robots_and_sitemap(client):
    robots_status, _headers, robots_text = fetch(client, SITE + "/robots.txt")
    if robots_status == 0:
        raise FatalCheckError(f"no se pudo leer robots.txt: {robots_text}")
    findings = {}
    robots_findings, sitemaps = check_robots(robots_status, robots_text)
    findings.update(robots_findings)
    sitemap_url = sitemaps[0] if sitemaps else SITE + "/sitemap.xml"
    return findings, collect_sitemap_urls(client, sitemap_url)


def _check_pages(client, sitemap_urls, findings):
    """Per-page, duplicate-title and hreflang checks; returns parsed 200 pages."""
    parsed_pages = {}
    titles = []
    for url, status, headers, text in _fetch_pages(client, sitemap_urls):
        page = parse_page(text)
        findings.update(check_page(url, status, headers, page))
        if status == 200:
            parsed_pages[url] = page
            titles.append((url, page["title"]))
            findings.update(check_lengths(url, page))
            findings.update(check_jsonld(url, page))
            findings.update(check_blank_links(url, page))
            findings.update(check_thin(url, page))
    findings.update(check_duplicate_titles(titles))
    findings.update(check_orphans(parsed_pages))

    alternates = {
        url: [href for _lang, href in page["alternates"]]
        for url, page in parsed_pages.items()
    }
    findings.update(hreflang_without_return(alternates))
    pairs = unlisted_hreflang_targets(alternates, sitemap_urls)
    statuses = {}
    for _source, target in pairs:
        if target not in statuses:
            status, _headers, _text = fetch(client, target)
            statuses[target] = status
    findings.update(broken_hreflang(pairs, statuses))
    return parsed_pages


def _check_llms(client, sitemap_urls, findings):
    status, _headers, text = fetch(client, SITE + "/llms.txt")
    links = extract_llms_links(text) if status == 200 else []
    sitemap_set = set(sitemap_urls)
    statuses = {}
    for url in links:
        if url in sitemap_set or url in statuses:
            continue
        link_status, _h, _t = fetch(client, url)
        statuses[url] = link_status
    findings.update(check_llms(status, links, sitemap_urls, statuses))


def _check_pagespeed(parsed_pages, findings):
    psi_urls = [SITE + path for path in PSI_PATHS]
    if parsed_pages:
        largest = max(parsed_pages, key=lambda url: parsed_pages[url]["body_len"])
        if largest not in psi_urls:
            psi_urls.append(largest)
    psi_urls = psi_urls[:6]
    notes = []
    unavailable = set()
    ok = 0
    for url in psi_urls:
        data, error = fetch_psi(url)
        if data is None:
            unavailable.add(url)
            notes.append(f"PageSpeed no disponible para {url}: {error}")
            continue
        ok += 1
        findings.update(check_psi(url, data))
    return notes, ok, len(psi_urls), unavailable


def _collect_findings(client):
    findings, sitemap_urls = _check_robots_and_sitemap(client)
    parsed_pages = _check_pages(client, sitemap_urls, findings)
    _check_llms(client, sitemap_urls, findings)
    notes, psi_ok, psi_total, unavailable = _check_pagespeed(parsed_pages, findings)
    return findings, sitemap_urls, notes, psi_ok, psi_total, unavailable


def _psi_url_of(fingerprint):
    parts = fingerprint.split(":", 2)
    if len(parts) == 3 and parts[0] == "psi":
        return parts[2]
    return None


def _oldest_since(previous, fingerprints, fallback):
    dates = []
    for fingerprint in fingerprints:
        entry = previous.get(fingerprint)
        if isinstance(entry, dict) and entry.get("desde"):
            dates.append(entry["desde"])
    return min(dates) if dates else fallback


def _render(findings, previous, new_fps, persisting_fps, carried_fps, resolved_fps,
            notes, sitemap_urls, psi_ok, psi_total, today):
    n = len(new_fps)
    r = len(resolved_fps)
    p = len(persisting_fps) + len(carried_fps)
    lines = [f"🔧 SEO técnico — biglobster.top: {n} nuevos, {r} resueltos, {p} siguen"]
    if n:
        lines.append("Nuevos:")
        lines.extend(f"- {findings[fp]}" for fp in new_fps[:MAX_LISTED])
        if n > MAX_LISTED:
            lines.append(f"… y {n - MAX_LISTED} más")
    if r:
        lines.append(f"Resueltos: {r}")
        for fp in resolved_fps[:10]:
            entry = previous.get(fp) or {}
            lines.append(f"- {entry.get('texto', fp)}")
    if p:
        oldest = _oldest_since(previous, list(persisting_fps) + list(carried_fps), today)
        lines.append(f"Siguen {p} de semanas anteriores (el más antiguo desde {oldest}).")
    lines.extend(notes)
    lines.append(
        f"Revisado: {len(sitemap_urls)} URLs del sitemap, robots, llms.txt, "
        f"PageSpeed {psi_ok}/{psi_total}."
    )
    return "\n".join(lines)


def main(argv=None):
    del argv  # invoked by path, never as a module with arguments
    state_path = _state_path()
    state = load_state(state_path)

    with httpx.Client(
        headers={"User-Agent": UA}, timeout=TIMEOUT, follow_redirects=False
    ) as client:
        try:
            findings, sitemap_urls, notes, psi_ok, psi_total, unavailable = _collect_findings(client)
        except FatalCheckError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    previous = state.get("hallazgos") or {}
    today = date.today().isoformat()
    new_fps = [fp for fp in findings if fp not in previous]
    persisting_fps = [fp for fp in findings if fp in previous]
    # A PageSpeed outage does not resolve last week's psi:* findings for that URL.
    carried_fps = [
        fp
        for fp in previous
        if fp not in findings and _psi_url_of(fp) in unavailable
    ]
    carried = set(carried_fps)
    resolved_fps = [
        fp for fp in previous if fp not in findings and fp not in carried
    ]

    new_state = {"hallazgos": {}}
    for fp, line in findings.items():
        entry = previous.get(fp) or {}
        new_state["hallazgos"][fp] = {
            "desde": entry.get("desde") or today,
            "texto": line,
        }
    for fp in carried_fps:
        entry = previous[fp]
        new_state["hallazgos"][fp] = entry if isinstance(entry, dict) else {
            "desde": today,
            "texto": fp,
        }

    save_state(state_path, new_state)

    if not new_fps and not resolved_fps:
        return 0

    print(
        _render(
            findings,
            previous,
            new_fps,
            persisting_fps,
            carried_fps,
            resolved_fps,
            notes,
            sitemap_urls,
            psi_ok,
            psi_total,
            today,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
