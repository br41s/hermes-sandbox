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


def gtin_core(raw) -> str:
    """The item reference inside a barcode, without packaging level or check digit.

    A GTIN-14 is [indicator][the GTIN-13 body][a recalculated check digit], so
    a case and the unit inside it share a body but agree on nothing else — not
    even the check digit. Liderpapel stores a GTIN-14 under EAN_UNIDAD for some
    products, so comparing whole codes against a distributor's UPC would miss
    every one of them.
    """
    digits = _digits(raw)
    if len(digits) == 14:
        digits = digits[1:]
    return digits[:-1].lstrip("0") if len(digits) > 1 else ""


def same_item_different_packaging(a, b) -> bool:
    """Same article, different packaging level — a case versus its unit.

    Deliberately NOT treated as a match: the page could be describing a box of
    ten, and its specifications would be about the box. Deliberately not
    treated as a contradiction either, since the article is the same one, so
    the manufacturer reference is still allowed to decide.
    """
    ca, cb = gtin_core(a), gtin_core(b)
    return bool(ca) and ca == cb and not same_gtin(a, b)


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

    if (
        our_gtin
        and their_gtin
        and not same_gtin(our_gtin, their_gtin)
        and not same_item_different_packaging(our_gtin, their_gtin)
    ):
        return {"verdict": "rejected", "outcome": "mismatch", "tier": None,
                "reason": f"la página declara el EAN {their_gtin}, no el nuestro ({our_gtin})"}
    if our_ref and their_ref and normalize_ref(our_ref) != normalize_ref(their_ref):
        return {"verdict": "rejected", "outcome": "mismatch", "tier": None,
                "reason": f"la referencia de la página ({their_ref}) no es la nuestra ({our_ref})"}

    if our_gtin and their_gtin and same_gtin(our_gtin, their_gtin):
        if not gtin_checksum_valid(their_gtin):
            return {"verdict": "rejected", "outcome": "mismatch", "tier": None,
                    "reason": f"el EAN de la página ({their_gtin}) no pasa su dígito de control"}
        return {"verdict": "verified", "outcome": "verified", "tier": "gtin",
                "reason": f"EAN coincidente y válido ({their_gtin})"}

    if our_ref and their_ref and normalize_ref(our_ref) == normalize_ref(their_ref):
        if our_brand and their_brand and normalize_ref(our_brand) != normalize_ref(their_brand):
            return {"verdict": "rejected", "outcome": "mismatch", "tier": None,
                    "reason": f"misma referencia pero otra marca ({their_brand} ≠ {our_brand})"}
        return {"verdict": "verified", "outcome": "verified", "tier": "mpn",
                "reason": f"referencia de fabricante coincidente ({their_ref})"}

    return {"verdict": "rejected", "outcome": "unverifiable", "tier": None,
            "reason": "la página no publica un EAN ni una referencia que podamos comparar"}


# A page can prove identity without publishing schema.org — most manufacturer
# sites do exactly that, printing the reference in the copy and nothing
# machine-readable anywhere. Verified against Fellowes and Rexel: neither
# publishes Product data, both print the reference repeatedly.
#
# The risk this opens is a *listing* page, which mentions our reference among
# twenty others; taking content from one would attribute another product's
# specifications to ours. So a page carrying many product identifiers is
# refused rather than mined.
MAX_OTHER_IDENTIFIERS = 5


def _distinct_eans(text: str) -> set:
    """Barcodes of any length the standard allows.

    Matching only 13 digits let a page full of GTIN-14s past the listing guard,
    because the lookahead that stops a 13-digit match inside a longer run also
    stopped it matching the longer run at all.
    """
    return {
        t
        for t in re.findall(r"(?<!\d)\d{12,14}(?!\d)", text)
        if gtin_checksum_valid(t)
    }


def looks_like_a_listing(text: str, our_gtin) -> bool:
    others = _distinct_eans(text)
    if our_gtin:
        others = {e for e in others if not same_gtin(e, our_gtin)}
    return len(others) > MAX_OTHER_IDENTIFIERS


def reference_is_distinctive(ref) -> bool:
    """Is this reference specific enough to be evidence on its own?

    "4691001" or "2104578EU" identify one article. "12" or "A4" appear on
    every page of a stationery catalogue and prove nothing.
    """
    cleaned = normalize_ref(ref)
    return len(cleaned) >= 5 and any(c.isdigit() for c in cleaned)


def text_mentions_reference(text: str, ref) -> bool:
    """The reference as a standalone token, not as part of a longer code."""
    if not reference_is_distinctive(ref):
        return False
    pattern = re.escape(str(ref).strip())
    return re.search(rf"(?<![A-Za-z0-9]){pattern}(?![A-Za-z0-9])", text, re.I) is not None


def judge_by_text(html: str, ours: dict) -> dict:
    """Identity from the page's own words, when it publishes no product data."""
    text = re.sub(r"<[^>]+>", " ", html)

    if looks_like_a_listing(text, ours.get("gtin")):
        return {"verdict": "rejected", "outcome": "listing", "tier": None,
                "reason": "la página lista muchos productos distintos, así que no se puede "
                          "saber qué texto es de este"}

    our_gtin = ours.get("gtin")
    if our_gtin and any(same_gtin(our_gtin, e) for e in _distinct_eans(text)):
        return {"verdict": "verified", "outcome": "verified", "tier": "gtin-text",
                "reason": f"la página imprime nuestro EAN ({our_gtin})"}

    our_ref = ours.get("mpn")
    if our_ref and text_mentions_reference(text, our_ref):
        brand = ours.get("brand")
        if brand and not re.search(re.escape(brand), text, re.I):
            return {"verdict": "rejected", "outcome": "mismatch", "tier": None,
                    "reason": f"aparece la referencia {our_ref} pero la marca {brand} no"}
        return {"verdict": "verified", "outcome": "verified", "tier": "mpn-text",
                "reason": f"la página imprime nuestra referencia ({our_ref}) y la marca"}

    return {"verdict": "rejected", "outcome": "unverifiable", "tier": None,
            "reason": "la página no menciona ni nuestro EAN ni nuestra referencia"}


def _meta(html: str, *names) -> Optional[str]:
    for name in names:
        m = re.search(
            rf'<meta[^>]+(?:name|property)=[\'"]{re.escape(name)}[\'"][^>]*content=[\'"]([^\'"]+)',
            html, re.I,
        )
        if m:
            return m.group(1).strip()
        m = re.search(
            rf'<meta[^>]+content=[\'"]([^\'"]+)[\'"][^>]*(?:name|property)=[\'"]{re.escape(name)}',
            html, re.I,
        )
        if m:
            return m.group(1).strip()
    return None


def extract_tables(html: str, limit: int = 40) -> dict:
    """Two-column rows from tables and definition lists — the spec sheet.

    Prose is deliberately not scraped: on a product page a table is about that
    product, whereas body text runs into related items, reviews and banners,
    which is where a specification from a different product would come from.
    """
    specs = {}
    rows = re.findall(r"<tr[ >].*?</tr>", html, re.S | re.I)
    for row in rows:
        cells = [
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
            for c in re.findall(r"<t[dh][ >].*?</t[dh]>", row, re.S | re.I)
        ]
        cells = [c for c in cells if c]
        if len(cells) == 2 and 1 < len(cells[0]) <= 80 and 0 < len(cells[1]) <= 200:
            specs.setdefault(cells[0], cells[1])
    pairs = re.findall(r"<dt[ >](.*?)</dt>\s*<dd[ >](.*?)</dd>", html, re.S | re.I)
    for term, desc in pairs:
        t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", term)).strip()
        d = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", desc)).strip()
        if t and d and len(t) <= 80 and len(d) <= 200:
            specs.setdefault(t, d)
    return dict(list(specs.items())[:limit])


class FetchRefused(Exception):
    """The page would not be served to us, as opposed to not existing.

    Retailers behind bot protection answer a plain urllib request with 403 or
    429 while serving the same URL to a browser. That is a different fact from
    a dead link or a genuine mismatch, and lumping them together hid how much
    research was being lost: a run reports "nothing to add" identically whether
    the source had nothing or simply would not open.
    """

    def __init__(self, status: int):
        self.status = status
        super().__init__(f"HTTP {status}")


# Statuses that mean "we were turned away", not "there is nothing here".
REFUSING_STATUSES = {401, 402, 403, 405, 406, 409, 429, 451}


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            return resp.read(MAX_PAGE_BYTES).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as err:
        if err.code in REFUSING_STATUSES:
            raise FetchRefused(err.code) from err
        raise


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
    from tools.bl_site_product_tool import _refuse_if_scripted

    # Same boundary as bl_site_product: verification is per-candidate work, and
    # a script driving it in a loop is an agent skipping the judgement it is
    # here to apply.
    scripted = _refuse_if_scripted()
    if scripted:
        return scripted

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
                "outcome": "no_identifiers",
                "reason": "este producto no tiene EAN ni referencia en el feed, así que "
                          "no hay forma de comprobar que una página hable de él",
                "url": url,
            },
            ensure_ascii=False,
        )

    try:
        html = _fetch(url)
    except FetchRefused as err:
        # Worth telling apart in the run report: this source existed and was
        # withheld, so a run full of these is a tooling limit, not a catalogue
        # with nothing left to add.
        return json.dumps(
            {"verdict": "rejected", "outcome": "blocked", "tier": None,
             "http_status": err.status,
             "reason": f"la página nos rechaza el acceso (HTTP {err.status}); "
                       "probablemente protección anti-bot. No es que el producto "
                       "no coincida: no hemos podido leerla.",
             "url": url},
            ensure_ascii=False,
        )
    except Exception as err:  # noqa: BLE001
        return json.dumps(
            {"verdict": "rejected", "outcome": "unreachable", "tier": None,
             "reason": f"no se pudo leer la página: {err}", "url": url},
            ensure_ascii=False,
        )

    # Structured data first: when a page publishes it, identity and content come
    # from the same block and nothing has to be inferred.
    node = None
    result = None
    for candidate in extract_products(html):
        verdict_ = judge(candidate, ours)
        if verdict_["verdict"] == "verified":
            node, result = candidate, verdict_
            break
        if result is None:
            node, result = candidate, verdict_

    # A page that contradicts us is done: it named a different product and no
    # amount of matching text changes that.
    contradicted = result is not None and "no es la nuestra" in result.get("reason", "") \
        or (result is not None and "no el nuestro" in result.get("reason", ""))

    if (result is None or result["verdict"] != "verified") and not contradicted:
        # Most manufacturer pages publish nothing machine-readable — verified
        # against Fellowes and Rexel — so fall back to what the page says in
        # its own words, with the listing guard doing the scoping.
        text_result = judge_by_text(html, ours)
        if text_result["verdict"] == "verified":
            result, node = text_result, None
        elif result is None:
            result = text_result

    payload = {**result, "url": url, "sku": sku}

    # Content crosses only on a verified identity. On rejection the caller gets
    # a reason and nothing else — there is deliberately no unverified text in
    # the conversation for a model to lean on.
    if result["verdict"] == "verified":
        specs = dict(_specs_of(node) if node else {})
        for key, value in extract_tables(html).items():
            specs.setdefault(key, value)
        description = (node.get("description") if node else None) or _meta(
            html, "og:description", "description"
        )
        payload["found"] = {
            "name": (_first(node, ("name",)) if node else None) or _meta(html, "og:title"),
            "description": (description or "")[:4000] or None,
            "specs": dict(list(specs.items())[:40]),
        }
        # Proving identity is not the same as having something to say. A page
        # can be unmistakably our product and still carry no specifications and
        # no prose — a bare shop listing. Reported apart from a useful match so
        # a run can distinguish "the sources had nothing" from "we could not
        # reach the sources", which reads identically in a skip line otherwise.
        if not payload["found"]["specs"] and not payload["found"]["description"]:
            payload["outcome"] = "verified_thin"
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
        "verifies, write the sheet from the distributor's data alone. "
        "Every answer carries an 'outcome' saying which of these happened, and they are not "
        "the same fact: 'verified' (a usable match), 'verified_thin' (definitely our product, "
        "but the page publishes no specifications worth taking), 'blocked' (the page refused "
        "us — HTTP 403/429, bot protection — so it was never read and might well have been "
        "useful), 'unreachable' (dead link, timeout), 'mismatch' (a different product), "
        "'unverifiable' (the page publishes no barcode or reference to compare), 'listing' "
        "(a category page covering many products), 'no_identifiers' (our own catalogue has "
        "neither barcode nor reference for this product, so nothing can be checked). "
        "Report the tally of these at the end of a run: 'blocked' is a limit of our tooling "
        "and 'verified_thin' is a limit of the sources, and telling them apart is what says "
        "whether better fetching would buy anything."
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

from tools.bl_site_product_tool import _tool_call  # noqa: E402
from tools.registry import registry  # noqa: E402

registry.register(
    name="product_enrich",
    toolset="bl_site_product",
    schema=PRODUCT_ENRICH_SCHEMA,
    handler=lambda args, **kw: _tool_call(
        lambda a: product_enrich(
            action=a.get("action", ""),
            sku=a.get("sku"),
            url=a.get("url"),
        ),
        args,
    ),
)
