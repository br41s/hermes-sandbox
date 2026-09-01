"""Find and write same-site 301 redirects on a bl-site-package client site.

``scan`` detects that a URL is gone (a run-to-run sitemap diff, confirmed dead
only after two separate runs agree); ``scan_legacy`` detects a URL from the
client's PRE-bl-site-package platform that Google still has indexed but that
was never in any bl-site-package sitemap, so ``scan`` can never see it (a
sitemap diff only knows about URLs THIS site has published at some point);
``find_target`` figures out what a dead URL should point to now;
``propose``/``publish``/``list``/``remove`` write through the site's own
``/api/redirects`` (bl-site-package PR #65). All these steps live in this one
tool on purpose: detection used to sit inside ``bl_site_health``, but that
coupled a same-site 404 to the Website Maintenance product specifically, when
the actual capability belongs with SEO — every bl-site-package client's
onsite SEO agent (``onsite-seo/bl-site-package-seo-agent.prompt``) should be
able to call it regardless of which other products they've bought. ``scan``
and ``scan_legacy`` share one history file, independent of
``bl_site_health``'s, so the two tools' state never entangles.

``scan_legacy`` exists because a client migrating onto bl-site-package from
a real old platform (confirmed for one client: a PrestaShop storefront) keeps
its old, Google-indexed URLs turning up as 404s indefinitely — with no GSC
access for rental clients (a deliberate architecture decision, not a gap) and
no live old site left to crawl, the only source that doesn't depend on either
is the Wayback Machine's CDX index, which lists every URL it has ever
archived under a domain. Discovery is deliberately rare (every
``LEGACY_REDISCOVERY_DAYS``, not every run) — these URLs are gone forever, so
there is nothing to re-poll for on a daily cadence, unlike ``scan``'s
two-run-confirmation, which exists specifically to catch a URL mid-flap.

``find_target`` is the one piece of reasoning that maps a dead URL to a live
one: given a dead
URL, pull its last Wayback Machine snapshot, read the barcode or manufacturer
reference off whatever schema.org Product data that snapshot published, and
ask the site which LIVE product now carries that identifier
(``GET /api/products?gtin=``/``?mpn=``). Structured data only — the same
primacy ``product_enrich_tool.judge()`` gives it, and for the same reason:
extracting an identifier to go SEARCH for a product with is a stronger claim
than confirming one already suspected, so the weaker text-based fallback
``product_enrich_tool.judge_by_text`` uses does not belong here at all. A dead
URL with no structured identifiers in its archive is a case for a human, not
for this tool guessing from prose.

The site owns every fact from here: ``propose`` always lands as a pending row,
and the server independently re-derives a gtin/mpn claim against the target
product before accepting it — this tool's ``evidence`` is a claim, never
something the site trusts outright. ``publish`` is the one call that makes a
redirect real, and is meant to be reserved for identifier-tier matches; a
content-page redirect resolved by title similarity or human judgement should
stay proposed, not published, until a person acts on it.

Credentials and the scripted-caller guard are shared with the other bl_site_*
tools (imported, never copied), so a client's site can never be resolved two
different ways and this tool refuses the same batch-scripting shortcut they
do: the work is one redirect's worth of judgement at a time.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from tools.bl_site_health_tool import _sweepable
from tools.bl_site_product_tool import _refuse_if_scripted, _tool_call
from tools.bl_site_publish_tool import _get_jwt, _get_site_credentials
from tools.product_enrich_tool import (
    GTIN_KEYS,
    MPN_KEYS,
    MAX_PAGE_BYTES,
    USER_AGENT,
    FETCH_TIMEOUT,
    _first,
    extract_products,
    wayback_snapshot_url,
)

REQUEST_TIMEOUT = 30

# --- scan: run-to-run sitemap diff, own history file -----------------------
_LOC_VALUE_RE = re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.IGNORECASE)
SCAN_HISTORY_FILE = "bl_site_redirect_history.json"
SCAN_CANDIDATE_CAP = 40
SCAN_WATCH_KEEP_DAYS = 60
SCAN_DEAD_STATUSES = (404, 410)
SCAN_REQUEST_TIMEOUT = 20

# --- scan_legacy: one-time-ish Wayback CDX discovery, same history file ----
LEGACY_REDISCOVERY_DAYS = 30
LEGACY_CDX_LIMIT = 2000
LEGACY_CANDIDATE_BATCH = 5
CDX_REQUEST_TIMEOUT = 30


def _scan_history_path() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / SCAN_HISTORY_FILE


def _load_scan_history() -> dict:
    path = _scan_history_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("known_sitemap_urls", [])
    data.setdefault("redirect_watch", {})
    data.setdefault("legacy_discovery", {})
    data.setdefault("legacy_candidates", {})
    return data


def _save_scan_history(history: dict) -> None:
    path = _scan_history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, indent=2), encoding="utf-8")


def _scan_fetch(url: str) -> dict:
    """One GET. Never raises — status 0 means a transport failure, same
    convention as every other fetcher in this codebase."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=SCAN_REQUEST_TIMEOUT) as resp:
            return {"status": resp.status, "body": resp.read(2_000_000).decode("utf-8", errors="replace")}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "body": ""}
    except urllib.error.URLError:
        return {"status": 0, "body": ""}


def _scan_sitemap(site_url: str) -> dict:
    """URLs that were in this site's own sitemap on a prior run and are gone
    now, confirmed dead on two separate runs.

    Same two-run-confirmation discipline as everywhere else this pattern is
    used: a URL must look dead on THIS run AND the run that first noticed it
    missing before it counts as confirmed — a transient outage must never be
    enough on its own to justify a redirect.
    """
    sitemap = _scan_fetch(site_url.rstrip("/") + "/sitemap.xml")
    current_urls = {loc.strip() for loc in _LOC_VALUE_RE.findall(sitemap["body"]) if loc.strip()}

    history = _load_scan_history()
    prior_urls = set(history.get("known_sitemap_urls", []))
    watch = history.setdefault("redirect_watch", {})

    # Newly missing this run, PLUS anything already under watch that still
    # isn't back in the sitemap — without the latter, a URL that vanished once
    # would drop out of "disappeared" on the very next run (it's no longer in
    # the prior snapshot either) before it ever reached two-run confirmation.
    newly_missing = prior_urls - current_urls
    still_missing = {u for u in watch if u not in current_urls}
    all_disappeared = sorted(newly_missing | still_missing)
    disappeared = all_disappeared[:SCAN_CANDIDATE_CAP]

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    confirmed_dead: list[dict] = []
    pending: list[dict] = []
    now_alive: list[dict] = []

    for url in disappeared:
        res = _scan_fetch(url)
        status = res["status"]
        prior = watch.get(url)
        if status in SCAN_DEAD_STATUSES:
            record = {"url": url, "status": status, "checked_at": now_iso}
            if prior and prior.get("last_status") in SCAN_DEAD_STATUSES:
                record["first_seen_dead"] = prior.get("first_seen_dead", now_iso)
                confirmed_dead.append(record)
            else:
                record["first_seen_dead"] = now_iso
                pending.append(record)
            watch[url] = {
                "last_status": status, "last_checked": now_iso,
                "first_seen_dead": record["first_seen_dead"],
            }
        elif status == 200:
            now_alive.append({"url": url, "status": status})
            watch.pop(url, None)
        # Anything else (5xx, transport failure) is an outage, not evidence of
        # removal — the watch entry is left exactly as it was.

    cutoff = time.time() - SCAN_WATCH_KEEP_DAYS * 86400
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
    _save_scan_history(history)

    return {
        "checked": len(disappeared),
        "cap": SCAN_CANDIDATE_CAP,
        "list_capped": len(all_disappeared) > SCAN_CANDIDATE_CAP,
        "confirmed_dead": confirmed_dead,
        "pending_confirmation": pending,
        "now_alive": now_alive,
    }


def _cdx_fetch(domain: str) -> Optional[list[str]]:
    """Every URL Wayback Machine has ever archived under this domain, or None
    on any failure — a discovery miss is a normal, boring answer here (same
    convention ``wayback_snapshot_url`` already uses), not something to raise
    on. ``fl=original`` makes the first row a ``["original"]`` header row."""
    query = urllib.parse.urlencode({
        "url": domain,
        "matchType": "domain",
        "output": "json",
        "collapse": "urlkey",
        "fl": "original",
        "limit": str(LEGACY_CDX_LIMIT),
    })
    req = urllib.request.Request(
        f"http://web.archive.org/cdx/search/cdx?{query}",
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=CDX_REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read(5_000_000).decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, list) or len(data) < 2:
        return []
    return [row[0] for row in data[1:] if row]


def _discover_legacy_urls(site_url: str) -> dict:
    """Legacy-platform URLs discovered via Wayback CDX, drained a few at a
    time through the same find_target/propose/publish loop ``scan``'s own
    candidates use.

    Re-discovery (the CDX call itself) is gated to LEGACY_REDISCOVERY_DAYS —
    these URLs are gone forever, so there is nothing to catch by polling
    daily, unlike ``scan``'s two-run confirmation which exists to catch a URL
    mid-flap. Once a URL is known, it is recorded permanently: a live-200
    check clears it as ``resolves_now``, and ``_find_target`` marks it
    ``processed`` the moment it's actually attempted — so nothing here is
    ever re-queried or re-reported forever.
    """
    history = _load_scan_history()
    discovery = history["legacy_discovery"]
    candidates = history["legacy_candidates"]

    due = True
    last_run = discovery.get("last_run")
    if last_run:
        try:
            age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(last_run)).days
            due = age_days >= LEGACY_REDISCOVERY_DAYS
        except ValueError:
            due = True

    domain = urllib.parse.urlparse(site_url).netloc
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if due:
        found = _cdx_fetch(domain)
        if found is not None:
            for raw_url in found:
                path = urllib.parse.urlparse(raw_url).path
                if not path or path == "/" or not _sweepable(path):
                    continue
                normalized = site_url.rstrip("/") + path
                if normalized not in candidates:
                    candidates[normalized] = {"status": "new", "discovered_at": now_iso}
            discovery["last_run"] = now_iso
            discovery["domain"] = domain
        # else: CDX unreachable/rate-limited this run — soft-fail, last_run
        # stays untouched so the next eligible run tries again.

    new_urls = sorted(u for u, rec in candidates.items() if rec.get("status") == "new")
    batch: list[str] = []
    for url in new_urls[:LEGACY_CANDIDATE_BATCH]:
        res = _scan_fetch(url)
        if res["status"] == 200:
            candidates[url]["status"] = "resolves_now"
        else:
            batch.append(url)

    _save_scan_history(history)
    return {
        "checked_domain": domain,
        "known_legacy_urls": len(candidates),
        "new_candidates": batch,
    }


def _mark_legacy_processed(old_path: str) -> None:
    """Record that a legacy candidate was actually attempted this run —
    called from ``_find_target`` itself, the one call every per-URL loop
    (scan-sourced or legacy-sourced) always makes first, regardless of
    outcome. A no-op for any URL that isn't a known legacy candidate."""
    history = _load_scan_history()
    candidates = history["legacy_candidates"]
    if old_path in candidates and candidates[old_path].get("status") == "new":
        candidates[old_path]["status"] = "processed"
        _save_scan_history(history)


def _request(method: str, url: str, token: str, body: Optional[dict] = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        # 422 is the server's own validation refusing the proposal or the
        # publish, and its body lists exactly what failed. Passed through
        # verbatim: it is instructions for what to try next, not noise.
        try:
            payload = json.loads(detail)
        except ValueError:
            raise RuntimeError(f"HTTP {e.code} from {url}: {detail}") from e
        if e.code == 422 and payload.get("blockers"):
            raise RuntimeError(
                "La redirección no pasa la validación del sitio: "
                + "; ".join(payload["blockers"])
            ) from e
        raise RuntimeError(f"HTTP {e.code} from {url}: {payload.get('error', detail)}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"No se pudo contactar {url}: {e.reason}") from e


def _fetch_snapshot(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return resp.read(MAX_PAGE_BYTES).decode("utf-8", errors="replace")


def _find_target(site_url: str, token: str, old_path: str) -> dict:
    dead_url = old_path if old_path.startswith("http") else site_url.rstrip("/") + old_path
    _mark_legacy_processed(dead_url)
    snapshot_url = wayback_snapshot_url(dead_url)
    if not snapshot_url:
        return {
            "found": False,
            "reason": "no hay una copia archivada de esta URL en Wayback Machine",
        }

    try:
        html = _fetch_snapshot(snapshot_url)
    except Exception as err:  # noqa: BLE001
        return {
            "found": False,
            "reason": f"no se pudo leer la copia archivada: {err}",
            "snapshot_url": snapshot_url,
        }

    gtin = mpn = None
    for node in extract_products(html):
        gtin = gtin or _first(node, GTIN_KEYS)
        mpn = mpn or _first(node, MPN_KEYS)
        if gtin or mpn:
            break

    if not gtin and not mpn:
        return {
            "found": False,
            "reason": "la copia archivada no publica un EAN ni una referencia "
                      "en datos estructurados (schema.org Product)",
            "snapshot_url": snapshot_url,
        }

    tier, identifier = ("gtin", gtin) if gtin else ("mpn", mpn)
    query = urllib.parse.urlencode({tier: identifier})
    result = _request("GET", f"{site_url}/api/products?{query}", token)
    products = result.get("products") or []

    if not products:
        return {
            "found": False,
            "reason": f"ningún producto vivo tiene {tier}={identifier}",
            "snapshot_url": snapshot_url,
            "match_tier": tier,
            "evidence": {tier: identifier},
        }

    product = products[0]
    return {
        "found": True,
        "new_path": f"/productos/{product['slug']}.html",
        "match_tier": tier,
        "evidence": {tier: identifier},
        "snapshot_url": snapshot_url,
        "sku": product.get("sku"),
    }


def bl_site_redirect(
    action: str,
    old_path: Optional[str] = None,
    new_path: Optional[str] = None,
    match_tier: Optional[str] = None,
    evidence: Optional[dict] = None,
    redirect_id: Optional[int] = None,
) -> str:
    from tools.registry import tool_error

    scripted = _refuse_if_scripted()
    if scripted:
        return scripted

    site_url, password = _get_site_credentials()
    if not site_url or not password:
        return tool_error(
            "Este perfil no tiene BL_SITE_URL y BL_SITE_PANEL_PASSWORD configurados."
        )

    try:
        token = _get_jwt(site_url, password)
        base = f"{site_url}/api/redirects"

        if action == "scan":
            return json.dumps(_scan_sitemap(site_url), ensure_ascii=False)

        if action == "scan_legacy":
            return json.dumps(_discover_legacy_urls(site_url), ensure_ascii=False)

        if action == "find_target":
            if not old_path:
                return tool_error("find_target requiere 'old_path'.")
            return json.dumps(_find_target(site_url, token, old_path), ensure_ascii=False)

        if action == "list":
            result = _request("GET", base, token)
            return json.dumps(result, ensure_ascii=False)

        if action == "propose":
            if not old_path or not new_path:
                return tool_error("propose requiere 'old_path' y 'new_path'.")
            body = {"old_path": old_path, "new_path": new_path}
            if match_tier:
                body["match_tier"] = match_tier
            if evidence:
                body["evidence"] = evidence
            result = _request("POST", base, token, body)
            return json.dumps(result, ensure_ascii=False)

        if action == "publish":
            if not redirect_id:
                return tool_error("publish requiere 'redirect_id'.")
            result = _request("POST", f"{base}/{redirect_id}/publish", token)
            return json.dumps(result, ensure_ascii=False)

        if action == "remove":
            if not redirect_id:
                return tool_error("remove requiere 'redirect_id'.")
            result = _request("DELETE", f"{base}/{redirect_id}", token)
            return json.dumps(result, ensure_ascii=False)

        return tool_error(
            f"Acción desconocida '{action}'. Usa 'scan', 'scan_legacy', 'find_target', "
            "'list', 'propose', 'publish' o 'remove'."
        )
    except RuntimeError as e:
        return tool_error(str(e))


BL_SITE_REDIRECT_SCHEMA = {
    "name": "bl_site_redirect",
    "description": (
        "Find and manage same-site 301 redirects for the bl-site-package client site this "
        "profile is dedicated to. "
        "Use action='scan' (no other arguments) once per run to find URLs that were in this "
        "site's own sitemap on a prior run and are gone now. Returns 'confirmed_dead' (dead on "
        "this run AND the run that first noticed it missing — eligible to act on), "
        "'pending_confirmation' (just went missing this run, needs one more run to confirm — do "
        "nothing with these yet) and 'now_alive' (came back, nothing to do). Empty on the very "
        "first run ever, which is not a sign of a clean site — there is nothing to diff against yet. "
        "Use action='scan_legacy' (no other arguments) once per run to find URLs from the "
        "client's platform BEFORE bl-site-package that Google still has indexed — these were "
        "never in this site's own sitemap, so 'scan' can never find them. Discovery itself (a "
        "Wayback Machine CDX lookup) only runs every ~30 days since these URLs are gone forever, "
        "not flapping; returns 'new_candidates', a small batch ready for 'find_target' exactly "
        "like 'confirmed_dead' is. A URL that resolves 200 live is dropped automatically, and "
        "every URL is remembered permanently so nothing is re-suggested twice. "
        "Use action='find_target' with 'old_path' (a URL from 'confirmed_dead' or "
        "'new_candidates') to discover a "
        "candidate: it "
        "reads the barcode/reference off the URL's last Wayback Machine snapshot and looks up "
        "which LIVE product now carries it. Returns found=false with a reason when there is no "
        "archive, the archive publishes no structured barcode/reference, or no live product "
        "matches — that is the normal outcome for a dead CONTENT page (not a product), which "
        "this tool cannot resolve; report it as a human-reviewable suggestion instead. "
        "Use action='propose' with 'old_path', 'new_path' and, when found via find_target, the "
        "SAME 'match_tier' and 'evidence' it returned — never invent or adjust them. The site "
        "re-derives an identifier-tier claim itself and rejects a mismatch; it always saves as "
        "pending regardless of tier, so proposing is safe and never goes live. "
        "Use action='publish' with 'redirect_id' (from propose's response) to make a redirect "
        "real. Only ever do this for a redirect whose match_tier is 'gtin' or 'mpn' — a "
        "content-page match (no match_tier, or resolved by title similarity) must stay proposed "
        "for a human to publish, never published by you. "
        "Use action='list' to see existing redirects (optionally add nothing else; the site "
        "returns all of them). "
        "Use action='remove' with 'redirect_id' to delete a wrong redirect. "
        "Only ever touches the one site configured for this profile (BL_SITE_URL) — never "
        "another client's."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["scan", "scan_legacy", "find_target", "list", "propose", "publish", "remove"],
                "description": "Which operation to perform.",
            },
            "old_path": {
                "type": "string",
                "description": "The dead URL's path (e.g. '/productos/viejo.html'). Required "
                               "for 'find_target' and 'propose'; comes from scan's "
                               "'confirmed_dead' list or scan_legacy's 'new_candidates' list.",
            },
            "new_path": {
                "type": "string",
                "description": "The live URL's path to redirect to. Required for 'propose'.",
            },
            "match_tier": {
                "type": "string",
                "enum": ["gtin", "mpn", "human"],
                "description": "Confidence tier for 'propose'. Use exactly what 'find_target' "
                               "returned for a product match; omit or use 'human' for a "
                               "content-page match you resolved yourself (e.g. by title "
                               "similarity) — never claim 'gtin'/'mpn' without find_target's "
                               "evidence, the site will reject a claim it cannot re-derive.",
            },
            "evidence": {
                "type": "object",
                "description": "For 'propose' with an identifier tier: the SAME evidence object "
                               "'find_target' returned (e.g. {\"gtin\": \"...\"}) — copy it "
                               "verbatim, never construct your own.",
            },
            "redirect_id": {
                "type": "integer",
                "description": "The redirect's id (from propose's or list's response). "
                               "Required for 'publish' and 'remove'.",
            },
        },
        "required": ["action"],
    },
}

from tools.registry import registry  # noqa: E402

# Registered into the EXISTING bl_site_publish toolset on purpose: every
# rented profile already has it enabled, so no client config changes and no
# new toolset gets switched on anywhere else — same reasoning bl_site_health
# already documents for itself.
registry.register(
    name="bl_site_redirect",
    toolset="bl_site_publish",
    schema=BL_SITE_REDIRECT_SCHEMA,
    handler=lambda args, **kw: _tool_call(
        lambda a: bl_site_redirect(
            action=a.get("action", ""),
            old_path=a.get("old_path"),
            new_path=a.get("new_path"),
            match_tier=a.get("match_tier"),
            evidence=a.get("evidence"),
            redirect_id=a.get("redirect_id"),
        ),
        args,
    ),
)
