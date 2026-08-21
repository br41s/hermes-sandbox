"""Write product sheets on a bl-site-package client site.

The client's catalogue comes from a distributor feed (Liderpapel), and the feed
supplies a rich description for only part of it — on Shoroban, 11,274 of 14,487
products. The rest render as a name, a price and a button. This tool is how the
rented agent writes the ones the distributor never wrote, and how it improves
the ones it wrote badly.

What comes back from ``get_sheet`` is the *feed's own* data for that product:
its specifications, barcode, manufacturer reference, documents and images. That
is the material a sheet is written from, and — unless the operator's prompt says
otherwise — the only material. It is licensed data from the client's own
supplier about the exact article they sell, which is a far stronger footing than
anything found by searching for a product name.

The site owns every fact. This tool submits a title and a body; the barcode, the
manufacturer reference and the fingerprint that detects the distributor changing
something underneath a published sheet are all stamped server-side and cannot be
supplied here. A wrong identifier is worse than none, and an agent has no
business asserting a barcode the feed already knows.

Publishing is gated by the site, not by this tool or by the model: a sheet needs
a title, a body and specifications in the feed before it can go live. A refusal
comes back with its reasons attached, which are meant to be acted on rather than
worked around.

Credentials resolve from the profile this runs under, through
``bl_site_publish_tool``'s helpers rather than a second copy, so a client's site
can never be resolved two different ways.
"""

from __future__ import annotations

import json
from typing import Optional

import urllib.error
import urllib.parse
import urllib.request

from tools.bl_site_publish_tool import _get_jwt, _get_site_credentials

# A batch is meant to be worked one product at a time; this bound stops a run
# from pulling a queue so long the agent loses the thread of it.
MAX_BATCH = 25
DEFAULT_BATCH = 10
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
        # 422 is the publishing gate refusing, and its body lists exactly what
        # is missing. Passed through verbatim: it is instructions, not noise.
        try:
            payload = json.loads(detail)
        except ValueError:
            raise RuntimeError(f"HTTP {e.code} from {url}: {detail}") from e
        if e.code == 422 and payload.get("blockers"):
            raise RuntimeError(
                "La ficha no cumple los requisitos para publicarse: "
                + "; ".join(payload["blockers"])
            ) from e
        raise RuntimeError(f"HTTP {e.code} from {url}: {payload.get('error', detail)}") from e


def bl_site_product(
    action: str,
    sku: Optional[str] = None,
    display_name: Optional[str] = None,
    description_md: Optional[str] = None,
    evidence: Optional[list] = None,
    publish: Optional[bool] = None,
    limit: Optional[int] = None,
    reason: Optional[str] = None,
) -> str:
    from tools.registry import tool_error

    site_url, password = _get_site_credentials()
    if not site_url or not password:
        return tool_error(
            "Este perfil no tiene BL_SITE_URL y BL_SITE_PANEL_PASSWORD configurados."
        )

    try:
        token = _get_jwt(site_url, password)
        base = f"{site_url}/api/product-content"

        if action == "next_batch":
            size = min(max(int(limit or DEFAULT_BATCH), 1), MAX_BATCH)
            result = _request("GET", f"{base}/queue?limit={size}", token)
            return json.dumps(result, ensure_ascii=False)

        if action == "get_sheet":
            if not sku:
                return tool_error("get_sheet requiere 'sku'.")
            result = _request("GET", f"{base}/{urllib.parse.quote(str(sku))}", token)
            return json.dumps(result, ensure_ascii=False)

        if action == "write_sheet":
            if not sku:
                return tool_error("write_sheet requiere 'sku'.")
            if not display_name and not description_md:
                return tool_error(
                    "write_sheet requiere al menos 'display_name' o 'description_md'."
                )
            body = {
                "display_name": display_name,
                "description_md": description_md,
                # Draft unless told otherwise. A half-written sheet that reaches
                # a visitor is worse than one that stays unwritten.
                "status": "owned" if publish else "enriched",
            }
            if evidence is not None:
                body["evidence"] = evidence
            result = _request(
                "PUT", f"{base}/{urllib.parse.quote(str(sku))}", token, body
            )
            return json.dumps(
                {
                    "success": True,
                    "sku": sku,
                    "status": result.get("status"),
                    "display_name": result.get("display_name"),
                },
                ensure_ascii=False,
            )

        if action == "skip_sheet":
            if not sku:
                return tool_error("skip_sheet requiere 'sku'.")
            if not reason:
                return tool_error(
                    "skip_sheet requiere 'reason' — qué falta para poder escribirla."
                )
            result = _request(
                "PUT",
                f"{base}/{urllib.parse.quote(str(sku))}",
                token,
                {"status": "skipped", "skip_reason": reason},
            )
            return json.dumps(
                {"success": True, "sku": sku, "status": result.get("status")},
                ensure_ascii=False,
            )

        return tool_error(
            f"Acción desconocida '{action}'. Usa 'next_batch', 'get_sheet', "
            "'write_sheet' o 'skip_sheet'."
        )
    except RuntimeError as e:
        return tool_error(str(e))


BL_SITE_PRODUCT_SCHEMA = {
    "name": "bl_site_product",
    "description": (
        "Write and publish product sheets on the bl-site-package client site this profile is "
        "dedicated to. "
        "Use action='next_batch' to get the products to work on: those in stock whose sheet "
        "nobody has written yet, dearest first, plus any published sheet whose underlying feed "
        "data has changed since it was written and needs re-checking. "
        "Use action='get_sheet' with 'sku' to read everything known about one product — the "
        "distributor's specifications, barcode, manufacturer reference, documents and images. "
        "This is the material the sheet is written from. "
        "Use action='write_sheet' with 'sku', 'display_name' and 'description_md' to save the "
        "sheet. It saves as a draft that visitors never see; pass publish=true to put it live. "
        "The site refuses to publish a sheet that lacks a title, a body, or specifications in "
        "the feed, and says which of those is missing — fix it and call again rather than "
        "publishing something thin. "
        "Use action='skip_sheet' with 'sku' and 'reason' when a product genuinely has nothing "
        "to write a sheet from — the feed knows a brand and nothing else. That is the correct "
        "outcome for such a product, and recording it is what keeps it from leading the batch "
        "again tomorrow; it returns to the queue by itself if the distributor ever supplies "
        "more. Never pad a sheet with invented detail to avoid skipping. "
        "The barcode, the manufacturer reference and the change-detection fingerprint are "
        "recorded by the site itself and cannot be passed here. "
        "Only ever touches the one site configured for this profile (BL_SITE_URL) — never "
        "another client's."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["next_batch", "get_sheet", "write_sheet", "skip_sheet"],
                "description": "Which operation to perform.",
            },
            "sku": {
                "type": "string",
                "description": (
                    "Product code, as returned by next_batch. Required for get_sheet and "
                    "write_sheet."
                ),
            },
            "display_name": {
                "type": "string",
                "description": (
                    "The product title to show, replacing the distributor's. Distributor titles "
                    "are warehouse labels — no capitalisation, no part number, e.g. 'Ink-jet hp "
                    "quietjet/plus think jet/plus negro plain inkjet'. Write what a buyer would "
                    "search for. Never invent a model or capacity the feed does not state. "
                    "Changing it never changes the product's URL."
                ),
            },
            "description_md": {
                "type": "string",
                "description": (
                    "The sheet body, in Markdown. Ground every factual claim in what get_sheet "
                    "returned for this product — never in memory of the brand, and never in a "
                    "similar product. If the feed does not state a figure, leave it out."
                ),
            },
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional http(s) URLs used as sources beyond the feed, recorded so a claim "
                    "can be traced back later. Omit when the sheet is written from feed data alone."
                ),
            },
            "publish": {
                "type": "boolean",
                "description": (
                    "True to put the sheet live. Omit or false to save a draft. Only publish a "
                    "sheet you would be happy for a customer to read."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "skip_sheet only: what is missing, in one line — e.g. 'el feed solo trae la "
                    "marca, sin características'. Recorded so the gap is visible rather than "
                    "silently retried."
                ),
            },
            "limit": {
                "type": "integer",
                "description": f"next_batch only: how many products to fetch (default {DEFAULT_BATCH}, max {MAX_BATCH}).",
            },
        },
        "required": ["action"],
    },
}

from tools.registry import registry  # noqa: E402

registry.register(
    name="bl_site_product",
    toolset="bl_site_product",
    schema=BL_SITE_PRODUCT_SCHEMA,
    handler=lambda args, **kw: bl_site_product(
        action=args.get("action", ""),
        sku=args.get("sku"),
        display_name=args.get("display_name"),
        description_md=args.get("description_md"),
        evidence=args.get("evidence"),
        publish=args.get("publish"),
        limit=args.get("limit"),
        reason=args.get("reason"),
    ),
)
