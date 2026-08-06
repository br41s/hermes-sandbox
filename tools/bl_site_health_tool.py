"""Deterministic health check for a bl-site-package client site.

The Website Maintenance rental is a recurring, fixed-price product, so the
*checking* half of it may not be "the model pokes around the site and reports
what it feels like reporting" — two runs a week apart have to check the same
things, in the same order, with the same thresholds. This module is that half:
every measurement below is code, and the model that consumes the result only
decides which of a fixed list of mechanical fixes to apply and how to word the
monthly report.

It is deliberately read-only. Everything it finds is written back through
``bl_site_publish`` (page fields, post bodies) by the agent, so there is exactly
one write path to a client's site and it is already audited.

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


def _has_meta_description(html: str) -> bool:
    tag = _META_DESC_RE.search(html)
    if not tag:
        return False
    content = _META_CONTENT_RE.search(tag.group(0))
    return bool(content and content.group(1).strip())


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
        return {"runs": [], "reports": []}
    if not isinstance(data, dict):
        return {"runs": [], "reports": []}
    data.setdefault("runs", [])
    data.setdefault("reports", [])
    return data


def _save_history(history: dict) -> None:
    history["runs"] = history["runs"][-HISTORY_KEEP:]
    history["reports"] = history["reports"][-24:]
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


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

def _run_check(site_url: str, token: str) -> dict:
    config = _http_json("GET", f"{site_url}/api/site/config")
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
                record["title_present"] = bool(_TITLE_RE.search(res["body"]))
                record["meta_description_present"] = _has_meta_description(res["body"])
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

    all_up = not down
    now = datetime.now(timezone.utc)
    period = now.strftime("%Y-%m")

    history = _load_history()
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
        "sitemap": {
            "urls": sitemap_urls,
            "site_url_config": (config.get("site_url") or "").strip(),
        },
        "image_issues": image_issues,
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
