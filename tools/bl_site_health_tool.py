"""Deterministic health check for a bl-site-package client site.

The Website Maintenance rental is a recurring, fixed-price product, so the
*checking* half of it may not be "the model pokes around the site and reports
what it feels like reporting" — two runs a week apart have to check the same
things, in the same order, with the same thresholds. This module is that half:
every measurement below is code, and the model that consumes the result only
decides which of a fixed list of mechanical fixes to apply and how to word the
monthly report.

It is read-only with respect to the client's *content*: everything it finds is
written back through ``bl_site_publish`` (page fields, post bodies) by the
agent, so there is exactly one write path to a client's site and it is already
audited. The single exception is the monthly contact-form probe, which posts
one synthetic message to the public ``POST /api/contact`` endpoint — the same
call any visitor makes. It touches no config, no page and no post, it is gated
to once per calendar month, and the attempt is stamped in history before the
send so a retried run can never submit twice.

Credentials resolve from the *profile* the cron job runs under, via
``bl_site_publish_tool``'s own helpers — imported rather than copied so a
client's site URL/password can never be resolved two different ways.

Run history lives in the profile's own Hermes home
(``$HERMES_HOME/bl_site_health_history.json``), which is what makes "uptime
over the last 30 days" and "the monthly report is due" answerable at all: a
single cron run only ever sees one moment. Nothing in bl-site-package is
required for it.
"""

from __future__ import annotations

import difflib
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from tools.bl_site_publish_tool import _get_jwt, _get_site_credentials, _http_json

# The five-page structure is fixed by the product, plus the catalog, the three
# legal pages and the two crawler files Eleventy generates. A client cannot add
# a sixth page, so this list is exhaustive by construction — that is precisely
# why a broken-link sweep here needs no judgment about what "should" exist.
CORE_ROUTES = ("/", "/quienes-somos", "/servicios", "/contacto", "/blog")
CATALOG_ROUTES = ("/productos",)
LEGAL_ROUTES = ("/privacidad", "/condiciones", "/uso-de-ia")
CRAWLER_ROUTES = ("/robots.txt", "/sitemap.xml")

# Headers src/server.js sets unconditionally, plus HSTS which it sets only when
# NODE_ENV=production. A missing HSTS is the single most common instance
# misconfiguration and is invisible from the panel, so it is worth naming.
EXPECTED_HEADERS = (
    "content-security-policy",
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
    "permissions-policy",
    "strict-transport-security",
)

# Page-level budgets. Fixed numbers, not a judgment call: a page over these is
# reported, and the agent's fix list for it is equally fixed.
SLOW_PAGE_MS = 2500
HEAVY_PAGE_BYTES = 1_500_000
TLS_WARN_DAYS = 21

# Legal/identity fields the LocalBusiness JSON-LD and the legal pages read. The
# agent may never invent these — it only reports which are empty.
LEGAL_FIELDS = ("legal_name", "legal_id", "legal_address", "legal_email")
BIZ_FIELDS = ("biz_city", "biz_phone", "biz_street", "biz_postal_code")
PAGE_TEXT_FIELDS = (
    "page_index_title", "page_index_subtitle", "page_index_desc",
    "page_quienes_title", "page_quienes_desc",
    "page_servicios_title", "page_servicios_desc",
    "page_contacto_title", "page_contacto_desc",
    "page_blog_title",
)

HISTORY_FILE = "bl_site_health_history.json"
HISTORY_KEEP = 120  # ~4 months of daily runs; enough for a 30-day rollup.
LINK_CHECK_CAP = 120  # Bound the sweep so one link-happy blog can't run forever.
REQUEST_TIMEOUT = 20

# --- Social / contact reachability -----------------------------------------
# The client's social and contact endpoints are config fields, not free text,
# so "is this a well-formed WhatsApp number / e-mail / profile URL" is a format
# question with one answer. bl-site-package exposes exactly these two social
# fields (PUBLIC_CONFIG_KEYS in src/db/database.js); they feed the
# LocalBusiness `sameAs` array, so a dead one degrades structured data
# silently — which is why this is its own finding category and not a line item
# inside the generic outbound-link sweep.
SOCIAL_CONFIG_FIELDS = ("biz_facebook", "biz_instagram")
SOCIAL_CHECK_CAP = 10
# Only these three statuses count as "this profile is gone". Facebook,
# Instagram and LinkedIn routinely answer 403/429/999 to a datacenter IP that
# is not a browser; treating those as dead would hand the client a monthly
# false positive on a profile that works fine for humans.
DEAD_SOCIAL_STATUSES = (0, 404, 410)
# E.164: 8 digits is the shortest plausible national number, 15 the hard
# maximum. Anything outside cannot dial and cannot be a valid `wa.me` target.
PHONE_MIN_DIGITS = 8
PHONE_MAX_DIGITS = 15

# --- Duplicate / thin content ----------------------------------------------
# String similarity, not meaning. Two titles at >=0.90 character similarity are
# duplicates by any reading; below that the tool says nothing rather than
# guessing. Reported only — rewriting a client's copy is not this product.
DUPLICATE_RATIO = 0.90
THIN_META_MIN_CHARS = 50

# --- Structured data --------------------------------------------------------
# Required properties per schema.org type, from what bl-site-package's own
# builders emit (src/content/structured-data.js). A block missing one of these
# is invalid to Google's Rich Results test — a mechanical fact, not taste.
JSONLD_REQUIRED = {
    "BlogPosting": ("headline", "author", "datePublished"),
    "Article": ("headline", "author", "datePublished"),
    "FAQPage": ("mainEntity",),
    "Product": ("name",),
    "WebPage": ("name",),
}
# LocalBusiness has ~80 schema.org subtypes and the site sanitizes @type into
# any of them, so unknown types fall back to the one property they all require.
JSONLD_DEFAULT_REQUIRED = ("name",)
# Types whose `url` must be the page it is embedded in. LocalBusiness is
# excluded on purpose: it carries the site root URL on every page by design.
JSONLD_SELF_URL_TYPES = ("BlogPosting", "Article")

# --- Old-site redirect sweep -----------------------------------------------
# Only runs when the profile carries OLD_SITE_URL (same optional flag the
# onboarding-content and product-articles agents use). Bounded: the old URL
# list comes from the old sitemap, or failing that one level of homepage links.
OLD_SITE_PATH_CAP = 40
# Paths worth sweeping. Assets are excluded — a missing old .jpg is not a lost
# page and would drown the finding that matters.
SWEEPABLE_SUFFIXES = ("", ".html", ".htm", ".php", ".asp", ".aspx", ".shtml")

# --- Release drift ----------------------------------------------------------
# Every client runs the same bl-site-package code; main's package.json is the
# single statement of what "current" is. The deployed side comes from the
# instance's own GET /api/site/status (bl-site-package PR #32). Any mismatch
# counts as outdated — a version *ahead* of main was deployed outside the
# release flow, which is exactly as much BigLobster's problem as a stale one.
RELEASED_PACKAGE_JSON_URL = (
    "https://raw.githubusercontent.com/br41s/bl-site-package/main/package.json"
)

# --- Monthly contact-form probe --------------------------------------------
# Sends one real message through the client's public form, so the volume is
# capped at one per calendar month and stamped in history *before* the send.
FORM_TEST_NAME = "BigLobster · prueba de mantenimiento"
FORM_TEST_EMAIL = "no-reply@biglobster.top"

_HREF_RE = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_IMG_SRC_RE = re.compile(r"""\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_IMG_ALT_RE = re.compile(r"""\balt\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_DESC_RE = re.compile(
    r"""<meta\b[^>]*\bname\s*=\s*["']description["'][^>]*>""", re.IGNORECASE
)
_META_CONTENT_RE = re.compile(r"""\bcontent\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
_LOC_RE = re.compile(r"<loc>", re.IGNORECASE)
_LOC_VALUE_RE = re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.IGNORECASE)
_JSONLD_RE = re.compile(
    r"""<script\b[^>]*\btype\s*=\s*["']application/ld\+json["'][^>]*>(.*?)</script>""",
    re.IGNORECASE | re.DOTALL,
)
# Deliberately permissive: this catches "no @", "no dot in the domain" and
# whitespace — the failure modes a config field actually has — without trying
# to out-parse RFC 5322.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
_WA_LINK_RE = re.compile(
    r"""^https?://(?:api\.whatsapp\.com/send\?[^"']*?phone=|wa\.me/)(\+?[\d\s\-().]+)""",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# HTTP primitives
# ---------------------------------------------------------------------------

def _fetch(url: str, method: str = "GET") -> dict:
    """Fetch one URL and return a status/timing record. Never raises.

    A transport failure (DNS, refused, TLS, timeout) is recorded as status 0
    with a reason, because for an availability check "the host did not answer"
    and "the host answered 500" are both outages and both have to survive into
    the report.
    """
    started = time.monotonic()
    req = urllib.request.Request(url, method=method, headers={"User-Agent": "hermes-bl-maintenance/1"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = resp.read(4 * 1024 * 1024)
            return {
                "url": url,
                "status": resp.status,
                "ms": int((time.monotonic() - started) * 1000),
                "bytes": len(body),
                "headers": {k.lower(): v for k, v in resp.headers.items()},
                "body": body.decode("utf-8", errors="replace"),
            }
    except urllib.error.HTTPError as exc:
        return {
            "url": url,
            "status": exc.code,
            "ms": int((time.monotonic() - started) * 1000),
            "bytes": 0,
            "headers": {},
            "body": "",
        }
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
        return {
            "url": url,
            "status": 0,
            "ms": int((time.monotonic() - started) * 1000),
            "bytes": 0,
            "headers": {},
            "body": "",
            "error": str(getattr(exc, "reason", exc)),
        }


def _tls_days_remaining(site_url: str) -> Optional[dict]:
    """Days left on the TLS certificate, or None for a non-HTTPS/unreachable host."""
    parsed = urllib.parse.urlparse(site_url)
    if parsed.scheme != "https":
        return None
    host = parsed.hostname
    port = parsed.port or 443
    if not host:
        return None
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=REQUEST_TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
    except (OSError, ssl.SSLError) as exc:
        return {"host": host, "error": str(exc)}
    not_after = cert.get("notAfter")
    if not not_after:
        return {"host": host, "error": "certificate carried no notAfter"}
    expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    days = (expires - datetime.now(timezone.utc)).days
    return {"host": host, "expires": expires.date().isoformat(), "days_remaining": days}


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _links_in(html: str) -> list[str]:
    return [h.strip() for h in _HREF_RE.findall(html)]


def _images_in(html: str) -> list[dict]:
    out = []
    for tag in _IMG_RE.findall(html):
        src = _IMG_SRC_RE.search(tag)
        alt = _IMG_ALT_RE.search(tag)
        out.append({
            "src": src.group(1) if src else "",
            "alt": alt.group(1).strip() if alt else None,
        })
    return out


def _meta_description(html: str) -> str:
    tag = _META_DESC_RE.search(html)
    if not tag:
        return ""
    content = _META_CONTENT_RE.search(tag.group(0))
    return content.group(1).strip() if content else ""


def _has_meta_description(html: str) -> bool:
    return bool(_meta_description(html))


def _title_text(html: str) -> str:
    match = _TITLE_RE.search(html)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def _digits(value: Optional[str]) -> str:
    return re.sub(r"\D", "", value or "")


def _normalized(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _normalize(href: str, site_url: str) -> Optional[str]:
    """Absolute URL for a checkable link, or None for one we deliberately skip."""
    href = href.strip()
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:", "whatsapp:")):
        return None
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return site_url + href
    if href.startswith(("http://", "https://")):
        return href
    return None


# ---------------------------------------------------------------------------
# Social and contact endpoints
# ---------------------------------------------------------------------------

def _bad_phone(raw: str) -> Optional[str]:
    """Reason this string cannot be dialled, or None. Pure format arithmetic."""
    digits = _digits(raw)
    if not digits:
        return "no digits"
    if len(digits) < PHONE_MIN_DIGITS:
        return f"only {len(digits)} digits"
    if len(digits) > PHONE_MAX_DIGITS:
        return f"{len(digits)} digits, E.164 allows at most {PHONE_MAX_DIGITS}"
    return None


def _check_social_contact(config: dict, site_url: str, page_html: dict[str, str]) -> list[dict]:
    """Format-check every contact endpoint, and fetch the social profiles.

    Split out from ``broken_links`` on purpose: a dead Instagram profile and a
    dead link inside a blog post are the same HTTP fact but a different problem
    for the client, and "2 dead social links" has to be readable as such in the
    monthly report. `mailto:`/`tel:`/WhatsApp hrefs are skipped by
    ``_normalize`` — nothing checked them until now.
    """
    issues: list[dict] = []

    wa = (config.get("whatsapp_number") or "").strip()
    if wa:
        reason = _bad_phone(wa)
        if reason:
            issues.append({
                "kind": "whatsapp", "source": "config:whatsapp_number",
                "value": wa, "issue": "invalid_format", "detail": reason,
            })

    phone = (config.get("biz_phone") or "").strip()
    if phone:
        reason = _bad_phone(phone)
        if reason:
            issues.append({
                "kind": "phone", "source": "config:biz_phone",
                "value": phone, "issue": "invalid_format", "detail": reason,
            })

    email = (config.get("legal_email") or "").strip()
    if email and not _EMAIL_RE.match(email):
        issues.append({
            "kind": "email", "source": "config:legal_email",
            "value": email, "issue": "invalid_format",
            "detail": "not a syntactically valid address",
        })

    # Endpoints as they are actually rendered, which can drift from config if a
    # page body carries a hand-written mailto:/tel:/wa.me link.
    seen: set[tuple[str, str]] = set()
    for path, html in page_html.items():
        for href in _links_in(html):
            low = href.lower()
            if low.startswith("mailto:"):
                addr = href[7:].split("?", 1)[0].strip()
                if addr and not _EMAIL_RE.match(addr) and ("email", addr) not in seen:
                    seen.add(("email", addr))
                    issues.append({
                        "kind": "email", "source": path, "value": addr,
                        "issue": "invalid_format",
                        "detail": "not a syntactically valid address",
                    })
            elif low.startswith("tel:"):
                num = href[4:].strip()
                reason = _bad_phone(num)
                if reason and ("phone", num) not in seen:
                    seen.add(("phone", num))
                    issues.append({
                        "kind": "phone", "source": path, "value": num,
                        "issue": "invalid_format", "detail": reason,
                    })
            else:
                match = _WA_LINK_RE.match(href)
                if match:
                    num = match.group(1).strip()
                    reason = _bad_phone(num)
                    if reason and ("whatsapp", num) not in seen:
                        seen.add(("whatsapp", num))
                        issues.append({
                            "kind": "whatsapp", "source": path, "value": num,
                            "issue": "invalid_format", "detail": reason,
                        })

    checked = 0
    for field in SOCIAL_CONFIG_FIELDS:
        value = (config.get(field) or "").strip()
        if not value:
            continue
        if not value.startswith(("http://", "https://")):
            issues.append({
                "kind": "social", "source": f"config:{field}", "value": value,
                "issue": "invalid_url",
                "detail": "must be a full https:// profile URL; it is emitted "
                          "into the LocalBusiness sameAs array as-is",
            })
            continue
        if checked >= SOCIAL_CHECK_CAP:
            continue
        checked += 1
        res = _fetch(value, method="HEAD")
        if res["status"] in (405, 501, 0):
            res = _fetch(value)
        if res["status"] in DEAD_SOCIAL_STATUSES:
            issues.append({
                "kind": "social", "source": f"config:{field}", "value": value,
                "issue": "dead_profile", "status": res["status"],
                "detail": res.get("error") or "profile did not resolve",
            })
    return issues


# ---------------------------------------------------------------------------
# Duplicate / thin content
# ---------------------------------------------------------------------------

def _duplicate_pairs(values: dict[str, str]) -> list[dict]:
    """Every pair of pages whose text is >= DUPLICATE_RATIO similar.

    ``difflib`` on normalized strings — a character-level ratio against a fixed
    threshold, so the same two pages are flagged on every run or on none. It is
    not a semantic judgment and is never auto-fixed.
    """
    items = [(path, text) for path, text in sorted(values.items()) if text]
    out: list[dict] = []
    for i, (path_a, text_a) in enumerate(items):
        for path_b, text_b in items[i + 1:]:
            ratio = difflib.SequenceMatcher(None, text_a, text_b).ratio()
            if ratio >= DUPLICATE_RATIO:
                out.append({
                    "pages": [path_a, path_b],
                    "ratio": round(ratio, 2),
                    "value": text_a[:140],
                })
    return out


def _check_duplicate_content(titles: dict[str, str], descriptions: dict[str, str]) -> dict:
    thin = [
        {"page": path, "chars": len(text)}
        for path, text in sorted(descriptions.items())
        if len(text) < THIN_META_MIN_CHARS
    ]
    return {
        "duplicate_titles": _duplicate_pairs({p: _normalized(t) for p, t in titles.items()}),
        "duplicate_descriptions": _duplicate_pairs(
            {p: _normalized(d) for p, d in descriptions.items()}
        ),
        "missing_titles": sorted(p for p, t in titles.items() if not t.strip()),
        "thin_descriptions": thin,
        "similarity_threshold": DUPLICATE_RATIO,
        "thin_below_chars": THIN_META_MIN_CHARS,
    }


# ---------------------------------------------------------------------------
# Structured data (JSON-LD)
# ---------------------------------------------------------------------------

def _validate_jsonld_block(block: dict, page: str, index: int, page_url: str) -> list[dict]:
    def issue(kind: str, **extra) -> dict:
        return {"page": page, "block": index, "issue": kind, **extra}

    out: list[dict] = []
    context = block.get("@context")
    if not (isinstance(context, str) and "schema.org" in context):
        out.append(issue("missing_context", value=str(context)[:60]))

    # schema.org allows @type to be a list ("this is both a Store and a
    # Plumber"). bl-site-package never emits one, but reporting a legal
    # construct as missing would be a lie, so take the first named type.
    ld_type = block.get("@type")
    if isinstance(ld_type, list):
        ld_type = next((t for t in ld_type if isinstance(t, str) and t.strip()), None)
    if not isinstance(ld_type, str) or not ld_type.strip():
        out.append(issue("missing_type"))
        return out
    ld_type = ld_type.strip()

    required = JSONLD_REQUIRED.get(ld_type, JSONLD_DEFAULT_REQUIRED)
    for field in required:
        value = block.get(field)
        if value is None or (isinstance(value, (str, list, dict)) and not value):
            out.append(issue("missing_required_field", type=ld_type, field=field))

    # Type shape. Only the properties whose *type* is unambiguous in
    # schema.org and which bl-site-package actually emits.
    if "address" in block and not isinstance(block["address"], dict):
        out.append(issue("wrong_type", type=ld_type, field="address", expected="object"))
    if "geo" in block:
        geo = block["geo"]
        if not isinstance(geo, dict):
            out.append(issue("wrong_type", type=ld_type, field="geo", expected="object"))
        else:
            for coord in ("latitude", "longitude"):
                if coord in geo and not isinstance(geo[coord], (int, float)):
                    out.append(issue(
                        "wrong_type", type=ld_type, field=f"geo.{coord}", expected="number",
                    ))
    for field in ("openingHours", "sameAs"):
        if field in block and not isinstance(block[field], list):
            out.append(issue("wrong_type", type=ld_type, field=field, expected="array"))

    if ld_type == "FAQPage":
        entities = block.get("mainEntity")
        if entities is not None and not isinstance(entities, list):
            out.append(issue("wrong_type", type=ld_type, field="mainEntity", expected="array"))
        elif isinstance(entities, list):
            for n, item in enumerate(entities):
                if not isinstance(item, dict) or item.get("@type") != "Question":
                    out.append(issue("faq_item_not_question", type=ld_type, item=n))
                    continue
                if not str(item.get("name") or "").strip():
                    out.append(issue("faq_question_empty", type=ld_type, item=n))
                answer = item.get("acceptedAnswer")
                if not isinstance(answer, dict) or not str(answer.get("text") or "").strip():
                    out.append(issue("faq_answer_empty", type=ld_type, item=n))

    # The drift that has bitten BigLobster's own site: structured data pointing
    # somewhere other than the page carrying it, which is what a blank or stale
    # `site_url` config key produces.
    if ld_type in JSONLD_SELF_URL_TYPES:
        raw_url = block.get("url")
        declared = raw_url.strip() if isinstance(raw_url, str) else ""
        if declared and declared.rstrip("/") != page_url.rstrip("/"):
            out.append(issue(
                "url_mismatch", type=ld_type, field="url",
                declared=declared, expected=page_url,
            ))
    return out


def _check_structured_data(page_html: dict[str, str], site_url: str) -> dict:
    issues: list[dict] = []
    without: list[str] = []
    blocks_checked = 0

    for path, html in sorted(page_html.items()):
        raw_blocks = _JSONLD_RE.findall(html)
        if not raw_blocks:
            without.append(path)
            continue
        page_url = site_url + ("" if path == "/" else path)
        for index, raw in enumerate(raw_blocks):
            blocks_checked += 1
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                issues.append({
                    "page": path, "block": index, "issue": "invalid_json",
                    "detail": str(exc)[:120],
                })
                continue
            # A @graph or a bare array is legal JSON-LD; validate each member.
            if isinstance(parsed, dict) and isinstance(parsed.get("@graph"), list):
                members = parsed["@graph"]
            elif isinstance(parsed, list):
                members = parsed
            else:
                members = [parsed]
            for member in members:
                if not isinstance(member, dict):
                    issues.append({
                        "page": path, "block": index, "issue": "not_an_object",
                    })
                    continue
                issues.extend(_validate_jsonld_block(member, path, index, page_url))

    return {
        "blocks_checked": blocks_checked,
        "pages_without_jsonld": without,
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# Old-site redirect sweep
# ---------------------------------------------------------------------------

def _sweepable(path: str) -> bool:
    tail = path.rsplit("/", 1)[-1]
    if "." not in tail:
        return True
    return ("." + tail.rsplit(".", 1)[-1]).lower() in SWEEPABLE_SUFFIXES


def _old_site_paths(old_site_url: str) -> tuple[list[str], str]:
    """Paths the old site publishes, and where the list came from.

    Sitemap first because it is the site's own statement of what it publishes;
    homepage links are the fallback when there is no sitemap. Both are exact
    reads of what the old host serves — no crawling heuristics, no depth
    beyond one level, and hard-capped.
    """
    sitemap = _fetch(old_site_url + "/sitemap.xml")
    paths: list[str] = []
    source = "sitemap"
    if sitemap["status"] == 200 and _LOC_RE.search(sitemap["body"]):
        for loc in _LOC_VALUE_RE.findall(sitemap["body"]):
            parsed = urllib.parse.urlparse(loc)
            if parsed.path:
                paths.append(parsed.path)
    else:
        source = "homepage_links"
        home = _fetch(old_site_url + "/")
        if home["status"] == 200:
            for href in _links_in(home["body"]):
                target = _normalize(href, old_site_url)
                if target and target.startswith(old_site_url):
                    parsed = urllib.parse.urlparse(target)
                    if parsed.path:
                        paths.append(parsed.path)

    unique = sorted({p for p in paths if p not in ("", "/") and _sweepable(p)})
    return unique[:OLD_SITE_PATH_CAP], source


def _old_site_sweep(old_site_url: str, site_url: str) -> dict:
    """Which of the old site's paths still resolve on the new one.

    urllib follows redirects, so a 200 means "the visitor gets a page" whether
    that came from a redirect or from the path existing outright — which is the
    only question a client cares about. Reported, never fixed: a redirect is
    web-server configuration, not something the panel API can write.
    """
    paths, source = _old_site_paths(old_site_url)
    dead_ends: list[dict] = []
    resolved = 0
    for path in paths:
        res = _fetch(site_url + path)
        if res["status"] == 200:
            resolved += 1
        else:
            dead_ends.append({
                "old_url": old_site_url + path,
                "new_url": site_url + path,
                "status": res["status"],
            })
    return {
        "old_site_url": old_site_url,
        "path_source": source,
        "paths_checked": len(paths),
        "path_cap": OLD_SITE_PATH_CAP,
        "list_capped": len(paths) >= OLD_SITE_PATH_CAP,
        "resolved": resolved,
        "dead_ends": dead_ends,
    }


# ---------------------------------------------------------------------------
# Same-site redirect candidates
# ---------------------------------------------------------------------------
# GSC is not wired per rental client (bl-site-package-seo-agent.prompt runs
# without it), so the historical-search-analytics signal BigLobster's
# find_dead_urls.py uses is unavailable here. The next-cheapest signal that
# needs no extra API is a run-to-run diff of the site's OWN sitemap: a URL
# present last run and gone from the current one, confirmed dead by a live
# check, is a real candidate independent of whether Google ever indexed it.
REDIRECT_CANDIDATE_CAP = 40
REDIRECT_WATCH_KEEP_DAYS = 60
REDIRECT_DEAD_STATUSES = (404, 410)


def _redirect_candidates(sitemap_body: str, history: dict) -> dict:
    """URLs that were in this site's sitemap last run and are gone now.

    Same two-run-confirmation discipline as find_dead_urls.py, and for the
    same reason: a URL must look dead on THIS run and on the run that first
    noticed it missing before it is confirmed — a transient sitemap hiccup or
    outage must never be enough on its own to redirect away a page that is
    still there. Report-only today: nothing on this side writes a redirect
    yet (that needs a redirects table on the client's own instance), so this
    surfaces the same way old_site_redirects already does.
    """
    current_urls = {loc.strip() for loc in _LOC_VALUE_RE.findall(sitemap_body) if loc.strip()}
    prior_urls = set(history.get("known_sitemap_urls", []))
    watch = history.setdefault("redirect_watch", {})
    # Newly missing this run, PLUS anything already under watch that still
    # isn't back in the sitemap — without the latter, a URL that vanished
    # once would drop out of "disappeared" on the very next run (it's no
    # longer in the prior snapshot either) and silently stop being checked
    # before it ever reached two-run confirmation.
    newly_missing = prior_urls - current_urls
    still_missing = {u for u in watch if u not in current_urls}
    all_disappeared = sorted(newly_missing | still_missing)
    disappeared = all_disappeared[:REDIRECT_CANDIDATE_CAP]
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    confirmed_dead: list[dict] = []
    pending: list[dict] = []
    now_alive: list[dict] = []

    for url in disappeared:
        res = _fetch(url)
        prior = watch.get(url)
        if res["status"] in REDIRECT_DEAD_STATUSES:
            record = {"url": url, "status": res["status"], "checked_at": now_iso}
            if prior and prior.get("last_status") in REDIRECT_DEAD_STATUSES:
                record["first_seen_dead"] = prior.get("first_seen_dead", now_iso)
                confirmed_dead.append(record)
            else:
                record["first_seen_dead"] = now_iso
                pending.append(record)
            watch[url] = {
                "last_status": res["status"], "last_checked": now_iso,
                "first_seen_dead": record["first_seen_dead"],
            }
        elif res["status"] == 200:
            now_alive.append({"url": url, "status": res["status"]})
            watch.pop(url, None)
        # Anything else (5xx, transport failure) is an outage, not evidence of
        # removal — the watch entry is left exactly as it was.

    cutoff = time.time() - REDIRECT_WATCH_KEEP_DAYS * 86400
    pruned = {}
    for url, rec in watch.items():
        try:
            last = datetime.fromisoformat(rec["last_checked"]).timestamp()
        except (KeyError, ValueError, TypeError):
            continue
        if last >= cutoff:
            pruned[url] = rec
    history["redirect_watch"] = pruned
    history["known_sitemap_urls"] = sorted(current_urls)

    return {
        "path_source": "sitemap_diff",
        "checked": len(disappeared),
        "cap": REDIRECT_CANDIDATE_CAP,
        "list_capped": len(all_disappeared) > REDIRECT_CANDIDATE_CAP,
        "confirmed_dead": confirmed_dead,
        "pending_confirmation": pending,
        "now_alive": now_alive,
    }


# ---------------------------------------------------------------------------
# Release drift
# ---------------------------------------------------------------------------

def _instance_status(site_url: str, token: str) -> Optional[dict]:
    """The instance's own ``GET /api/site/status``, or None when it can't answer.

    The endpoint exists since bl-site-package PR #32, so a 404 here usually
    *is* the finding: an instance old enough to lack it is outdated by
    definition, and it surfaces as ``release.deployed = null`` rather than as
    a crashed check.
    """
    try:
        status = _http_json("GET", f"{site_url}/api/site/status", token=token)
    except (RuntimeError, OSError, ValueError):
        return None
    return status if isinstance(status, dict) else None


def _latest_released_version() -> Optional[str]:
    """bl-site-package's version on main, or None when GitHub can't be read.

    Goes through ``_fetch`` (which never raises), so a GitHub outage degrades
    the release check to "latest unknown" instead of failing the whole run.
    """
    res = _fetch(RELEASED_PACKAGE_JSON_URL)
    if res["status"] != 200:
        return None
    try:
        version = json.loads(res["body"]).get("version")
    except (json.JSONDecodeError, AttributeError):
        return None
    return version.strip() if isinstance(version, str) and version.strip() else None


def _release_drift(site_status: Optional[dict]) -> dict:
    """Deployed vs released version. ``outdated`` is null when either side is
    unknown — the agent reports "could not compare", it never guesses."""
    deployed = None
    if site_status:
        raw = site_status.get("version")
        if isinstance(raw, str) and raw.strip():
            deployed = raw.strip()
    latest = _latest_released_version()
    return {
        "deployed": deployed,
        "latest": latest,
        # Direction-agnostic on purpose — see RELEASED_PACKAGE_JSON_URL.
        "outdated": (deployed != latest) if deployed and latest else None,
    }


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def _history_path() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / HISTORY_FILE


def _load_history() -> dict:
    path = _history_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"runs": [], "reports": [], "form_checks": []}
    if not isinstance(data, dict):
        return {"runs": [], "reports": [], "form_checks": []}
    data.setdefault("runs", [])
    data.setdefault("reports", [])
    data.setdefault("form_checks", [])
    data.setdefault("known_sitemap_urls", [])
    data.setdefault("redirect_watch", {})
    return data


def _save_history(history: dict) -> None:
    history["runs"] = history["runs"][-HISTORY_KEEP:]
    history["reports"] = history["reports"][-24:]
    history["form_checks"] = history.setdefault("form_checks", [])[-24:]
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, indent=2), encoding="utf-8")


def _rollup(history: dict, window: int = 30) -> dict:
    runs = history["runs"][-window:]
    if not runs:
        return {"runs_recorded": 0, "uptime_pct": None, "last_incident": None}
    ok = sum(1 for r in runs if r.get("all_up"))
    incidents = [r for r in runs if not r.get("all_up")]
    return {
        "runs_recorded": len(runs),
        "uptime_pct": round(100.0 * ok / len(runs), 1),
        "last_incident": incidents[-1]["at"] if incidents else None,
        "incidents_in_window": len(incidents),
    }


def _report_due(history: dict, period: str) -> bool:
    """True when no monthly report has been recorded for `period` (YYYY-MM).

    The model never decides whether the report is due — a date comparison is
    exactly the kind of thing that should not depend on which day a model
    thinks it is, and this also makes the report idempotent if a run is retried.
    """
    return not any(r.get("period") == period for r in history["reports"])


def _form_check_due(history: dict, period: str) -> bool:
    """True when the synthetic contact-form probe has not run this month.

    Same date-comparison gate as the monthly report, and for a stronger reason:
    the probe sends a real e-mail through the client's SMTP. It must be
    impossible for a retried cron run to send twice.
    """
    return not any(f.get("period") == period for f in history.get("form_checks", []))


def _run_form_check(
    site_url: str, token: str, history: dict, period: str,
    site_status: Optional[dict] = None,
) -> dict:
    """Post one synthetic message through the public contact form and confirm
    it landed in the panel inbox.

    What this proves, exactly: ``POST /api/contact`` is reachable, accepts a
    well-formed submission and persists it — i.e. a real lead is not being
    dropped on the floor by a 500, a broken rate limiter or a regressed route.

    What it cannot prove: that the notification e-mail arrived. ``src/api/
    contact.js`` catches every SMTP error into ``console.error`` and answers
    ``{success: true}`` regardless, so a configured mailer that fails at send
    time is invisible from out here and ``email_delivery_verified`` stays
    False. What ``GET /api/site/status`` (bl-site-package PR #32) *does*
    answer is whether the mail path is configured at all: its
    ``smtp_configured`` / ``notify_email_configured`` booleans are echoed into
    the result, separating "presumably sent" from "certainly never sent" —
    the common failure. Both are None on an instance too old to have the
    endpoint.

    The attempt is stamped into history *before* the POST: a crash between
    sending and verifying then costs one month's result, never a second e-mail
    into the client's inbox.
    """
    marker = f"BL-MAINT-{period}-{int(time.time())}"
    entry = {
        "period": period,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "marker": marker,
    }
    history.setdefault("form_checks", []).append(entry)
    _save_history(history)

    result = {
        "ran": True,
        "period": period,
        "marker": marker,
        # Never true from out here. See the docstring.
        "email_delivery_verified": False,
        "smtp_configured": site_status.get("smtp_configured") if site_status else None,
        "notify_email_configured": site_status.get("notify_email_configured") if site_status else None,
    }
    try:
        _http_json("POST", f"{site_url}/api/contact", {
            "name": FORM_TEST_NAME,
            "email": FORM_TEST_EMAIL,
            "message": (
                "Mensaje de prueba automático del servicio de mantenimiento web "
                "de BigLobster. Comprueba una vez al mes que el formulario de "
                f"contacto sigue funcionando. No requiere respuesta. [{marker}]"
            ),
        })
    except RuntimeError as exc:
        entry["result"] = "submit_failed"
        _save_history(history)
        result.update({"submitted": False, "persisted": False, "error": str(exc)[:300]})
        return result

    try:
        inbox = _http_json("GET", f"{site_url}/api/contact", token=token).get("messages", [])
    except RuntimeError as exc:
        entry["result"] = "inbox_unreadable"
        _save_history(history)
        result.update({"submitted": True, "persisted": None, "error": str(exc)[:300]})
        return result

    persisted = any(marker in (m.get("message") or "") for m in inbox)
    entry["result"] = "persisted" if persisted else "not_persisted"
    _save_history(history)
    result.update({"submitted": True, "persisted": persisted})
    return result


def _get_old_site_url() -> Optional[str]:
    """The client's previous site, if they gave one at provisioning.

    Same per-profile env var the onboarding-content and product-articles agents
    read (``scripts/provision_bl_client.py`` writes it). Absent for a client
    with no previous site, and the sweep then does not run at all.
    """
    from hermes_cli.config import get_env_value

    return (get_env_value("OLD_SITE_URL") or "").strip().rstrip("/") or None


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

def _run_check(site_url: str, token: str) -> dict:
    config = _http_json("GET", f"{site_url}/api/site/config")
    # None on an instance predating /api/site/status — which the release
    # section then reports as drift rather than this call crashing the run.
    site_status = _instance_status(site_url, token)
    posts = _http_json("GET", f"{site_url}/api/blog/posts", token=token).get("posts", [])
    published = [p for p in posts if (p.get("status") or "published") == "published"]

    routes = list(CORE_ROUTES + CATALOG_ROUTES + LEGAL_ROUTES + CRAWLER_ROUTES)
    post_routes = [f"/blog/{p.get('slug')}" for p in published if p.get("slug")]

    pages: list[dict] = []
    down: list[dict] = []
    slow: list[dict] = []
    heavy: list[dict] = []
    image_issues: list[dict] = []
    seen_links: dict[str, list[str]] = {}
    root_headers: dict = {}
    sitemap_body = ""
    # HTML of every page that answered 200, kept for the checks that read the
    # document rather than its status: duplicate titles/descriptions, JSON-LD,
    # and the mailto:/tel:/wa.me links the link sweep deliberately skips.
    page_html: dict[str, str] = {}
    titles: dict[str, str] = {}
    descriptions: dict[str, str] = {}

    for path in routes + post_routes:
        res = _fetch(site_url + path)
        record = {
            "path": path,
            "status": res["status"],
            "ms": res["ms"],
            "bytes": res["bytes"],
        }
        is_html = path not in CRAWLER_ROUTES
        if path == "/":
            root_headers = res["headers"]
        if path == "/sitemap.xml":
            sitemap_body = res["body"]
        if res["status"] != 200:
            record["error"] = res.get("error")
            down.append(record)
        else:
            if res["ms"] > SLOW_PAGE_MS:
                slow.append(record)
            if res["bytes"] > HEAVY_PAGE_BYTES:
                heavy.append(record)
            if is_html:
                page_html[path] = res["body"]
                titles[path] = _title_text(res["body"])
                descriptions[path] = _meta_description(res["body"])
                record["title_present"] = bool(titles[path])
                record["meta_description_present"] = bool(descriptions[path])
                for img in _images_in(res["body"]):
                    if img["alt"] is None or img["alt"] == "":
                        image_issues.append({"page": path, "src": img["src"], "issue": "missing_alt"})
                    elif img["src"].startswith("http") and site_url not in img["src"]:
                        image_issues.append({"page": path, "src": img["src"], "issue": "hotlinked"})
                for href in _links_in(res["body"]):
                    target = _normalize(href, site_url)
                    if target:
                        seen_links.setdefault(target, []).append(path)
        pages.append(record)

    # Sitemap emptiness: Eleventy renders zero <loc> entries when the site_url
    # config key is blank, which silently makes the whole site uncrawlable.
    sitemap_urls = len(_LOC_RE.findall(sitemap_body))

    # Link sweep. Each unique target is checked once; internal ones we already
    # fetched are answered from the page results instead of being re-fetched.
    already: dict[str, int] = {}
    for page in pages:
        full = site_url + page["path"]
        already[full] = page["status"]
        already[full.rstrip("/")] = page["status"]
        already[full.rstrip("/") + "/"] = page["status"]
    broken: list[dict] = []
    checked = 0
    for target, found_on in seen_links.items():
        status = already.get(target)
        if status is None:
            if checked >= LINK_CHECK_CAP:
                continue
            checked += 1
            res = _fetch(target, method="HEAD")
            # Some hosts refuse HEAD; a 405/501 gets one GET retry before we
            # call a link dead, so we don't hand the client false positives.
            if res["status"] in (405, 501, 0):
                res = _fetch(target)
            status = res["status"]
        if status == 0 or status >= 400:
            broken.append({
                "url": target,
                "status": status,
                "internal": target.startswith(site_url),
                "found_on": sorted(set(found_on)),
            })

    # Publish drift: the DB says published, the built site doesn't have it.
    # Means src/build/rebuild.js didn't run (or failed) after the last write.
    page_status = {p["path"]: p["status"] for p in pages}
    drift = [
        {"slug": p.get("slug"), "title": p.get("title")}
        for p in published
        if page_status.get(f"/blog/{p.get('slug')}") != 200
    ]

    missing_headers = [h for h in EXPECTED_HEADERS if h not in root_headers]

    empty_page_fields = [f for f in PAGE_TEXT_FIELDS if not (config.get(f) or "").strip()]
    empty_legal = [f for f in LEGAL_FIELDS if not (config.get(f) or "").strip()]
    empty_biz = [f for f in BIZ_FIELDS if not (config.get(f) or "").strip()]

    social_contact_issues = _check_social_contact(config, site_url, page_html)
    duplicate_content = _check_duplicate_content(titles, descriptions)
    structured_data = _check_structured_data(page_html, site_url)

    old_site_url = _get_old_site_url()
    old_site_redirects = _old_site_sweep(old_site_url, site_url) if old_site_url else None

    all_up = not down
    now = datetime.now(timezone.utc)
    period = now.strftime("%Y-%m")

    history = _load_history()
    redirect_candidates = _redirect_candidates(sitemap_body, history)

    # Monthly, and only when the site is actually up: probing the form during an
    # outage would just record a failure we already know about, and would burn
    # the month's single allowed submission on it.
    if all_up and _form_check_due(history, period):
        form_check = _run_form_check(site_url, token, history, period, site_status)
    else:
        form_check = {
            "ran": False,
            "period": period,
            "reason": "already_run_this_month" if all_up else "site_down",
        }

    history["runs"].append({
        "at": now.isoformat(timespec="seconds"),
        "all_up": all_up,
        "down": [d["path"] for d in down],
        "broken_links": len(broken),
        "slowest_ms": max((p["ms"] for p in pages), default=0),
    })
    _save_history(history)

    return {
        "checked_at": now.isoformat(timespec="seconds"),
        "site_url": site_url,
        "company_name": config.get("company_name"),
        "sector": config.get("sector"),
        "availability": {
            "all_up": all_up,
            "checked": len(pages),
            "down": down,
        },
        "pages": pages,
        "slow_pages": slow,
        "heavy_pages": heavy,
        "broken_links": broken,
        # The complete set of internal targets that may legitimately exist, so
        # the agent rewrites a broken internal link to one of these and never
        # invents a route. Post URLs are /blog/<slug> for the slugs listed by
        # bl_site_publish(action="list_posts").
        "valid_internal_routes": list(CORE_ROUTES + CATALOG_ROUTES + LEGAL_ROUTES),
        "links_checked": len(seen_links),
        "link_check_capped": checked >= LINK_CHECK_CAP,
        "publish_drift": drift,
        "tls": _tls_days_remaining(site_url),
        "tls_warn_days": TLS_WARN_DAYS,
        "missing_security_headers": missing_headers,
        # Operator finding, never a client one: an outdated instance is
        # redeployed by BigLobster, the client is not told "your site is old".
        "release": _release_drift(site_status),
        "sitemap": {
            "urls": sitemap_urls,
            "site_url_config": (config.get("site_url") or "").strip(),
        },
        "image_issues": image_issues,
        "social_contact_issues": social_contact_issues,
        "duplicate_content": duplicate_content,
        "structured_data": structured_data,
        # None when the client never had a previous site (no OLD_SITE_URL).
        "old_site_redirects": old_site_redirects,
        # Same-site 404s: URLs that were in OUR OWN sitemap last run and are
        # gone now, confirmed dead on two separate runs. Empty on the first
        # ever run (nothing to diff against yet) — not a sign of a clean site.
        "redirect_candidates": redirect_candidates,
        "form_check": form_check,
        "empty_page_fields": empty_page_fields,
        "empty_legal_fields": empty_legal,
        "empty_business_fields": empty_biz,
        "posts": {"published": len(published), "total": len(posts)},
        "history": _rollup(history),
        "report_period": period,
        "report_due": _report_due(history, period),
    }


def bl_site_health(action: str = "check", period: Optional[str] = None, summary: Optional[str] = None) -> str:
    from tools.registry import tool_error

    site_url, password = _get_site_credentials()
    if not site_url or not password:
        return tool_error(
            "BL_SITE_URL and/or BL_SITE_PANEL_PASSWORD are not set for this profile. "
            "This tool only works when run under a client's dedicated profile."
        )

    try:
        if action == "check":
            token = _get_jwt(site_url, password)
            return json.dumps(_run_check(site_url, token))

        if action == "record_report":
            history = _load_history()
            stamp = period or datetime.now(timezone.utc).strftime("%Y-%m")
            if not _report_due(history, stamp):
                return json.dumps({"success": True, "period": stamp, "already_recorded": True})
            history["reports"].append({
                "period": stamp,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "summary": (summary or "")[:2000],
            })
            _save_history(history)
            return json.dumps({"success": True, "period": stamp, "already_recorded": False})

        if action == "history":
            history = _load_history()
            return json.dumps({
                "success": True,
                "rollup": _rollup(history),
                "runs": history["runs"][-30:],
                "reports": [r["period"] for r in history["reports"]],
                "form_checks": history.get("form_checks", [])[-12:],
            })

        return tool_error(f"Unknown action '{action}'. Use 'check', 'history' or 'record_report'.")
    except RuntimeError as exc:
        return tool_error(str(exc))


BL_SITE_HEALTH_SCHEMA = {
    "name": "bl_site_health",
    "description": (
        "Run the deterministic maintenance health check on the bl-site-package client site this "
        "profile is dedicated to, and read/stamp its run history. "
        "action='check' fetches every fixed route, the legal pages, robots.txt/sitemap.xml and "
        "every published blog post, then returns availability, response times, page weight, broken "
        "links (internal and outbound), publish drift (posts the API calls published but that 404 "
        "on the built site), TLS certificate days remaining, missing security headers, sitemap URL "
        "count, images with no alt text or hotlinked from another host, empty page/legal/business "
        "config fields, a 30-day uptime rollup from previous runs, and 'report_due' — true when no "
        "monthly report has been recorded for the current month yet. "
        "It also returns 'social_contact_issues' (malformed WhatsApp/phone/e-mail endpoints and "
        "social profile URLs that no longer resolve), 'duplicate_content' (page titles or meta "
        "descriptions that are near-identical by string similarity, plus ones that are too short), "
        "'structured_data' (JSON-LD blocks that fail to parse or are missing required schema.org "
        "fields), 'old_site_redirects' (only when the profile has OLD_SITE_URL: old paths that now "
        "dead-end on the new site; null otherwise), 'redirect_candidates' (URLs that were in "
        "THIS site's own sitemap on a previous run and are gone now — 'confirmed_dead' means "
        "dead on two separate runs and is the list worth reporting; 'pending_confirmation' just "
        "went missing this run and needs one more run to confirm; empty on the very first run "
        "ever, which is not a sign of a clean site), 'release' (the instance's deployed "
        "bl-site-package version vs the latest released on main; 'outdated' is true on ANY "
        "mismatch and null when either side is unknown — an outdated instance is reported to "
        "BigLobster, who redeploys it; it is never something to tell the client), and "
        "'form_check' — once per calendar month it posts one synthetic message through the "
        "public contact form and confirms it reached the panel inbox; it can never confirm the "
        "notification e-mail was delivered, but its 'smtp_configured'/'notify_email_configured' "
        "flags (from the instance's /api/site/status) say whether the mail path is configured "
        "at all. "
        "action='history' returns the stored run history without checking anything. "
        "action='record_report' stamps the monthly report as delivered for 'period' (YYYY-MM, "
        "defaults to the current month) so it is never produced twice. "
        "This tool never writes to the client's site — apply fixes with bl_site_publish."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["check", "history", "record_report"],
                "description": "Which operation to perform.",
            },
            "period": {
                "type": "string",
                "description": "Month as YYYY-MM for record_report. Defaults to the current month.",
            },
            "summary": {
                "type": "string",
                "description": "Optional one-paragraph summary stored alongside a recorded report.",
            },
        },
        "required": ["action"],
    },
}

from tools.registry import registry  # noqa: E402

# Registered into the EXISTING bl_site_publish toolset on purpose: the rented
# profiles already have that toolset enabled, so no client config changes and no
# new toolset gets switched on anywhere else.
registry.register(
    name="bl_site_health",
    toolset="bl_site_publish",
    schema=BL_SITE_HEALTH_SCHEMA,
    handler=lambda args, **kw: bl_site_health(
        action=args.get("action", "check"),
        period=args.get("period"),
        summary=args.get("summary"),
    ),
)
