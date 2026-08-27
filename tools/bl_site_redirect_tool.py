"""Find and write same-site 301 redirects on a bl-site-package client site.

Companion to ``bl_site_health``'s ``redirect_candidates``: that tool only
detects that a URL is gone (a sitemap diff, confirmed dead on two separate
runs); it has no idea what the URL should now point to, and nothing on the
Hermes side could write a redirect at all until the site grew a
``/api/redirects`` write path (bl-site-package PR #65) to match.

``find_target`` is the one new piece of reasoning this file adds: given a dead
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
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

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
            f"Acción desconocida '{action}'. Usa 'find_target', 'list', 'propose', "
            "'publish' o 'remove'."
        )
    except RuntimeError as e:
        return tool_error(str(e))


BL_SITE_REDIRECT_SCHEMA = {
    "name": "bl_site_redirect",
    "description": (
        "Find and manage same-site 301 redirects for the bl-site-package client site this "
        "profile is dedicated to — for a URL confirmed dead by bl_site_health's "
        "'redirect_candidates', map it to the current page and redirect it there. "
        "Use action='find_target' with 'old_path' (the dead URL) to discover a candidate: it "
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
                "enum": ["find_target", "list", "propose", "publish", "remove"],
                "description": "Which operation to perform.",
            },
            "old_path": {
                "type": "string",
                "description": "The dead URL's path (e.g. '/productos/viejo.html'). Required "
                               "for 'find_target' and 'propose'.",
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
