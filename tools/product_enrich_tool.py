"""Verify that an external page describes the *same* product, then hand over its facts.

A product sheet is only worth enriching from outside if the outside source is
about the exact article the client sells. "Toner HP 305A" and "Toner HP 305X"
differ by one letter and by 2,400 pages of yield; a search result that looks
right is not evidence. Publishing the wrong yield on a shop's own catalogue is
the one failure here that reaches a customer as a lie.

So identity is decided in code, never by the model:

* The tool reads the product's own identifiers **from the client's site**, not
  from its caller. An agent cannot claim a barcode to make a match pass.
* A candidate page is accepted only on a checksum-valid GTIN match, or on an
  exact manufacturer-reference match with the same brand.
* A page that states a *different* GTIN or reference is rejected outright — a
  contradiction is stronger evidence than a coincidence of names.

And the decisive part: **content is returned only when identity is verified.**
On rejection the caller gets a reason and nothing else, so there is no
unverified competitor text in the conversation for a model to be tempted by.

Only structured data is extracted (schema.org Product in JSON-LD). That is a
deliberate limit: a description and specifications published as machine-readable
data by the manufacturer are a far better footing than text scraped out of
arbitrary markup, and it keeps the identity and the content coming from the same
block.
"""

from __future__ import annotations

import json
import re
from typing import Optional

import urllib.error
import urllib.parse
import urllib.request

from tools.bl_site_publish_tool import _get_jwt, _get_site_credentials

FETCH_TIMEOUT = 25
MAX_PAGE_BYTES = 3_000_000
USER_AGENT = "Mozilla/5.0 (compatible; bl-site-product-research/1.0)"

# schema.org spells the barcode five ways depending on its length and vintage.
GTIN_KEYS = ("gtin13", "gtin", "gtin12", "gtin14", "gtin8", "ean")
MPN_KEYS = ("mpn", "sku", "productID", "model")


def _digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def gtin_checksum_valid(raw) -> bool:
    """GTIN-8/12/13/14 mod-10 check digit.

    A barcode that fails its own checksum is a typo or an invented number, and
    matching on it would make two unrelated products look like one.
    """
    digits = _digits(raw)
    if len(digits) not in (8, 12, 13, 14):
        return False
    body, check = digits[:-1], int(digits[-1])
    total = 0
    # Weights alternate 3/1 from the rightmost body digit leftwards.
    for i, ch in enumerate(reversed(body)):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    return (10 - total % 10) % 10 == check


def same_gtin(a, b) -> bool:
    """Compare barcodes by value, ignoring leading-zero padding.

    A UPC-A on the box and the same code stored as a 13-digit EAN differ only
    by a leading zero and are the same article.
    """
    da, db = _digits(a).lstrip("0"), _digits(b).lstrip("0")
    return bool(da) and da == db


def normalize_ref(value) -> str:
    """Manufacturer references are written -CE262A-, CE262A, ce262a."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _iter_json_ld(html: str):
    for block in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.S | re.I,
    ):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))


def extract_products(html: str) -> list:
    """Every schema.org Product node on the page."""
    found = []
    for node in _iter_json_ld(html):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if any(str(t).lower() == "product" for t in types if t):
            found.append(node)
    return found


def _first(node: dict, keys) -> Optional[str]:
    for key in keys:
        if node.get(key):
            value = node[key]
            if isinstance(value, (list, tuple)):
                value = value[0] if value else None
            if value:
                return str(value)
    return None


def _brand_of(node: dict) -> Optional[str]:
    brand = node.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    if isinstance(brand, (list, tuple)):
        brand = brand[0] if brand else None
        if isinstance(brand, dict):
            brand = brand.get("name")
    return str(brand) if brand else None


def _specs_of(node: dict) -> dict:
    specs = {}
    props = node.get("additionalProperty") or []
    props = props if isinstance(props, list) else [props]
    for prop in props:
        if isinstance(prop, dict) and prop.get("name") and prop.get("value") is not None:
            specs[str(prop["name"])[:80]] = str(prop["value"])[:200]
    return specs


def judge(candidate: dict, ours: dict) -> dict:
    """Decide whether one schema.org Product node is our product.

    Order matters: a contradiction outranks a match. A page that names a
    different barcode is describing something else, however well the rest of it
    lines up.
    """
    their_gtin = _first(candidate, GTIN_KEYS)
    their_ref = _first(candidate, MPN_KEYS)
    their_brand = _brand_of(candidate)

    our_gtin, our_ref, our_brand = ours.get("gtin"), ours.get("mpn"), ours.get("brand")

    if our_gtin and their_gtin and not same_gtin(our_gtin, their_gtin):
        return {"verdict": "rejected", "tier": None,
                "reason": f"la página declara el EAN {their_gtin}, no el nuestro ({our_gtin})"}
    if our_ref and their_ref and normalize_ref(our_ref) != normalize_ref(their_ref):
        return {"verdict": "rejected", "tier": None,
                "reason": f"la referencia de la página ({their_ref}) no es la nuestra ({our_ref})"}

    if our_gtin and their_gtin and same_gtin(our_gtin, their_gtin):
        if not gtin_checksum_valid(their_gtin):
            return {"verdict": "rejected", "tier": None,
                    "reason": f"el EAN de la página ({their_gtin}) no pasa su dígito de control"}
        return {"verdict": "verified", "tier": "gtin",
                "reason": f"EAN coincidente y válido ({their_gtin})"}

    if our_ref and their_ref and normalize_ref(our_ref) == normalize_ref(their_ref):
        if our_brand and their_brand and normalize_ref(our_brand) != normalize_ref(their_brand):
            return {"verdict": "rejected", "tier": None,
                    "reason": f"misma referencia pero otra marca ({their_brand} ≠ {our_brand})"}
        return {"verdict": "verified", "tier": "mpn",
                "reason": f"referencia de fabricante coincidente ({their_ref})"}

    return {"verdict": "rejected", "tier": None,
            "reason": "la página no publica un EAN ni una referencia que podamos comparar"}


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return resp.read(MAX_PAGE_BYTES).decode("utf-8", errors="replace")


def _our_product(sku: str) -> dict:
    site_url, password = _get_site_credentials()
    if not site_url or not password:
        raise RuntimeError(
            "Este perfil no tiene BL_SITE_URL y BL_SITE_PANEL_PASSWORD configurados."
        )
    token = _get_jwt(site_url, password)
    req = urllib.request.Request(
        f"{site_url}/api/product-content/{urllib.parse.quote(str(sku))}"
    )
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))["feed"]


def product_enrich(action: str, sku: Optional[str] = None, url: Optional[str] = None) -> str:
    from tools.registry import tool_error

    if action != "verify":
        return tool_error(f"Acción desconocida '{action}'. Usa 'verify'.")
    if not sku or not url:
        return tool_error("verify requiere 'sku' y 'url'.")
    if not re.match(r"^https?://", url, re.I):
        return tool_error("'url' debe ser una URL http(s).")

    try:
        ours = _our_product(sku)
    except Exception as err:  # noqa: BLE001
        return tool_error(f"No se pudo leer el producto {sku}: {err}")

    if not ours.get("gtin") and not ours.get("mpn"):
        return json.dumps(
            {
                "verdict": "rejected",
                "tier": None,
                "reason": "este producto no tiene EAN ni referencia en el feed, así que "
                          "no hay forma de comprobar que una página hable de él",
                "url": url,
            },
            ensure_ascii=False,
        )

    try:
        html = _fetch(url)
    except Exception as err:  # noqa: BLE001
        return json.dumps(
            {"verdict": "rejected", "tier": None,
             "reason": f"no se pudo leer la página: {err}", "url": url},
            ensure_ascii=False,
        )

    candidates = extract_products(html)
    if not candidates:
        return json.dumps(
            {"verdict": "rejected", "tier": None,
             "reason": "la página no publica datos estructurados de producto (schema.org)",
             "url": url},
            ensure_ascii=False,
        )

    best = None
    for node in candidates:
        result = judge(node, ours)
        if result["verdict"] == "verified":
            best = (result, node)
            break
        if best is None:
            best = (result, node)

    result, node = best
    payload = {**result, "url": url, "sku": sku}

    # Content crosses only on a verified identity. On rejection the caller gets
    # a reason and nothing else — there is deliberately no unverified text in
    # the conversation for a model to lean on.
    if result["verdict"] == "verified":
        payload["found"] = {
            "name": _first(node, ("name",)),
            "description": (node.get("description") or "")[:4000] or None,
            "specs": _specs_of(node),
        }
    return json.dumps(payload, ensure_ascii=False)


PRODUCT_ENRICH_SCHEMA = {
    "name": "product_enrich",
    "description": (
        "Check whether a web page describes the SAME product the client sells, and if it "
        "does, hand back the facts that page publishes. "
        "Use action='verify' with the product's 'sku' and the 'url' of a candidate page "
        "found by searching. "
        "Identity is decided by code, not by you: the tool reads the product's barcode and "
        "manufacturer reference from the client's own catalogue and compares them against "
        "what the page publishes. A page is accepted only on a checksum-valid barcode match, "
        "or on an exact manufacturer-reference match with the same brand. A page naming a "
        "different barcode or reference is rejected however similar the name looks. "
        "On rejection you get a reason and NO content — there is nothing to quote, and that "
        "is deliberate: a product that merely resembles this one is how a false specification "
        "gets published. Verify a page before using anything from it, and if no candidate "
        "verifies, write the sheet from the distributor's data alone."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["verify"], "description": "Only 'verify'."},
            "sku": {"type": "string", "description": "The client's product code."},
            "url": {
                "type": "string",
                "description": "Candidate page to check, from a web search. Prefer the "
                               "manufacturer's own site.",
            },
        },
        "required": ["action", "sku", "url"],
    },
}

from tools.registry import registry  # noqa: E402

registry.register(
    name="product_enrich",
    toolset="bl_site_product",
    schema=PRODUCT_ENRICH_SCHEMA,
    handler=lambda args, **kw: product_enrich(
        action=args.get("action", ""),
        sku=args.get("sku"),
        url=args.get("url"),
    ),
)
