"""Tests for the identity gate.

The whole value of this tool is what it refuses. A false accept publishes a
wrong specification on a shop's live catalogue under the client's name, so the
cases that matter are the near-misses: same brand and near-identical name but a
different yield, a barcode off by one digit, a reference that differs by a
suffix. Those are what a search returns, and what a model would otherwise treat
as good enough.
"""

import json

import pytest

from tools import product_enrich_tool as mod

OURS = {"gtin": "50043859629256", "mpn": "4691001", "brand": "Fellowes"}


def ld(**fields):
    return {"@type": "Product", **fields}


# --- the check digit ---------------------------------------------------------


def test_a_real_barcode_passes_its_checksum():
    for code in ("50043859629256", "8423473140325", "0088698004388"):
        assert mod.gtin_checksum_valid(code), code


def test_a_barcode_with_one_wrong_digit_fails():
    # This is the realistic corruption: a typo on a competitor's page. Matching
    # on it would tie two unrelated products together.
    assert not mod.gtin_checksum_valid("50043859629257")
    assert not mod.gtin_checksum_valid("8423473140326")


def test_a_barcode_of_the_wrong_length_fails():
    assert not mod.gtin_checksum_valid("123")
    assert not mod.gtin_checksum_valid("")


def test_padding_does_not_make_two_barcodes_different():
    # A UPC-A on the box and the same code stored as a 13-digit EAN.
    assert mod.same_gtin("0088698004388", "88698004388")
    assert not mod.same_gtin("0088698004388", "0088698004389")


def test_reference_punctuation_is_ignored():
    assert mod.normalize_ref("-CE262A-") == mod.normalize_ref("ce262a")
    assert mod.normalize_ref("CE262A") != mod.normalize_ref("CE263A")


# --- the verdict -------------------------------------------------------------


def test_a_matching_barcode_verifies():
    r = mod.judge(ld(gtin13="50043859629256", name="Fellowes 99Ci"), OURS)
    assert r["verdict"] == "verified"
    assert r["tier"] == "gtin"


def test_a_different_barcode_is_rejected_however_similar_the_name():
    # The dangerous near-miss: same brand, same family, one letter apart in the
    # model, and a different yield. A name-similarity check would accept it.
    r = mod.judge(
        ld(gtin13="50043859629999", name="Destructora de documentos Fellowes 99Ci"),
        OURS,
    )
    assert r["verdict"] == "rejected"
    assert "EAN" in r["reason"]


def test_a_contradicting_reference_outranks_a_matching_name():
    r = mod.judge(ld(mpn="4691999", name="Fellowes 99Ci"), OURS)
    assert r["verdict"] == "rejected"


def test_a_matching_reference_verifies_when_there_is_no_barcode():
    r = mod.judge(ld(mpn="4691001", brand={"name": "Fellowes"}), OURS)
    assert r["verdict"] == "verified"
    assert r["tier"] == "mpn"


def test_the_same_reference_under_another_brand_is_rejected():
    # Manufacturer references are not globally unique; "4691001" means one
    # thing at Fellowes and something else elsewhere.
    r = mod.judge(ld(mpn="4691001", brand={"name": "Rexel"}), OURS)
    assert r["verdict"] == "rejected"
    assert "marca" in r["reason"]


def test_a_barcode_that_fails_its_checksum_is_rejected_even_when_it_matches():
    ours = {"gtin": "50043859629257", "mpn": None, "brand": "Fellowes"}
    r = mod.judge(ld(gtin13="50043859629257"), ours)
    assert r["verdict"] == "rejected"
    assert "control" in r["reason"]


def test_a_page_with_no_identifiers_is_rejected():
    # A page can be about the right product and still be unusable: without an
    # identifier there is no way to know, and "probably" is not good enough.
    r = mod.judge(ld(name="Destructora de documentos Fellowes 99Ci"), OURS)
    assert r["verdict"] == "rejected"


def test_the_barcode_is_preferred_over_the_reference():
    r = mod.judge(ld(gtin13="50043859629256", mpn="algo-distinto"), OURS)
    assert r["verdict"] == "rejected", "a contradicting reference still rejects"


# --- extraction --------------------------------------------------------------


HTML = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Fellowes 99Ci",
 "gtin13":"50043859629256","brand":{"@type":"Brand","name":"Fellowes"},
 "description":"Destruye hasta 18 hojas en partículas de 4 x 38 mm.",
 "additionalProperty":[{"@type":"PropertyValue","name":"Nivel de seguridad","value":"P-4"}]}
</script></head><body></body></html>
"""


def test_finds_a_product_in_json_ld():
    products = mod.extract_products(HTML)
    assert len(products) == 1
    assert products[0]["gtin13"] == "50043859629256"


def test_finds_a_product_nested_in_a_graph():
    html = """<script type="application/ld+json">
    {"@context":"https://schema.org","@graph":[{"@type":"WebPage"},
     {"@type":"Product","name":"X","gtin13":"50043859629256"}]}</script>"""
    assert len(mod.extract_products(html)) == 1


def test_ignores_json_that_does_not_parse():
    assert mod.extract_products('<script type="application/ld+json">{ not json </script>') == []


def test_a_page_with_no_structured_data_yields_nothing():
    assert mod.extract_products("<html><body><h1>Fellowes 99Ci</h1></body></html>") == []


# --- the tool end to end -----------------------------------------------------


@pytest.fixture(autouse=True)
def _site(monkeypatch):
    monkeypatch.setattr(mod, "_our_product", lambda _sku: dict(OURS))


def test_verified_pages_hand_over_their_facts(monkeypatch):
    monkeypatch.setattr(mod, "_fetch", lambda _url: HTML)
    out = json.loads(mod.product_enrich(action="verify", sku="78276", url="https://x.test/p"))

    assert out["verdict"] == "verified"
    assert out["found"]["description"].startswith("Destruye hasta 18 hojas")
    assert out["found"]["specs"]["Nivel de seguridad"] == "P-4"


def test_a_rejected_page_hands_over_nothing_to_quote(monkeypatch):
    # The load-bearing rule. If unverified text came back with a warning
    # attached, it would be in the conversation and a model would use it.
    wrong = HTML.replace("50043859629256", "50043859629999")
    monkeypatch.setattr(mod, "_fetch", lambda _url: wrong)

    out = json.loads(mod.product_enrich(action="verify", sku="78276", url="https://x.test/p"))
    assert out["verdict"] == "rejected"
    assert "found" not in out
    assert "Destruye hasta 18 hojas" not in json.dumps(out, ensure_ascii=False)


def test_identity_comes_from_our_catalogue_not_the_caller(monkeypatch):
    # The caller passes only a sku and a url. There is no parameter through
    # which an agent could assert a barcode to make a match pass.
    monkeypatch.setattr(mod, "_fetch", lambda _url: HTML)
    import inspect

    params = set(inspect.signature(mod.product_enrich).parameters)
    assert params == {"action", "sku", "url"}


def test_a_product_with_no_identifiers_cannot_be_researched(monkeypatch):
    monkeypatch.setattr(mod, "_our_product", lambda _sku: {"gtin": None, "mpn": None, "brand": "X"})
    monkeypatch.setattr(mod, "_fetch", lambda _url: HTML)

    out = json.loads(mod.product_enrich(action="verify", sku="1", url="https://x.test/p"))
    assert out["verdict"] == "rejected"
    assert "found" not in out


def test_an_unreachable_page_is_a_rejection_not_a_crash(monkeypatch):
    def boom(_url):
        raise TimeoutError("timed out")

    monkeypatch.setattr(mod, "_fetch", boom)
    out = json.loads(mod.product_enrich(action="verify", sku="78276", url="https://x.test/p"))
    assert out["verdict"] == "rejected"


def test_requires_an_http_url():
    assert "http" in mod.product_enrich(action="verify", sku="1", url="file:///etc/passwd")


def test_rejects_an_unknown_action():
    assert "desconocida" in mod.product_enrich(action="scrape_everything", sku="1", url="https://x.test")
