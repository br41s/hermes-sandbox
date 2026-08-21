"""Behaviour tests for the product-sheet write tool.

The tool is the only path a rented agent has into a client's product pages, so
what matters is not that it can POST — it is which things it refuses to carry.
Identifiers and the change-detection fingerprint are the site's to stamp; a
sheet is a draft unless publication is asked for explicitly; and the publishing
gate's refusal has to arrive as readable instructions rather than an HTTP code,
or the agent cannot act on it.
"""

import json
import urllib.error

import pytest

from tools import bl_site_product_tool as mod

SITE = "https://cliente.example"
FEED_SHEET = {
    "sku": "78276",
    "feed": {
        "name": "Destructora de documentos fellowes 99ci",
        "gtin": "50043859629256",
        "mpn": "4691001",
        "features": [{"name": "Marca", "value": "Fellowes"}],
    },
    "content": None,
    "drifted": False,
}


@pytest.fixture(autouse=True)
def _credentials(monkeypatch):
    monkeypatch.setattr(mod, "_get_site_credentials", lambda: (SITE, "panel-pw"))
    monkeypatch.setattr(mod, "_get_jwt", lambda _u, _p: "jwt-token")


def wire(monkeypatch, response=None, error=None):
    """Capture what the tool sends, and control what comes back."""
    sent = {}

    def _request(method, url, token, body=None):
        sent["method"], sent["url"], sent["body"] = method, url, body
        if error is not None:
            raise error
        return response if response is not None else {}

    monkeypatch.setattr(mod, "_request", _request)
    return sent


def test_reports_a_profile_without_a_site(monkeypatch):
    monkeypatch.setattr(mod, "_get_site_credentials", lambda: (None, None))
    assert "BL_SITE_URL" in mod.bl_site_product(action="next_batch")


def test_next_batch_passes_the_queue_through(monkeypatch):
    queue = {"pending": [{"sku": "78276"}], "review": [], "totals": {"owned": 0}}
    wire(monkeypatch, response=queue)

    assert json.loads(mod.bl_site_product(action="next_batch")) == queue


def test_next_batch_is_bounded(monkeypatch):
    # A queue longer than the agent can hold in mind is not a useful batch.
    sent = wire(monkeypatch, response={})
    mod.bl_site_product(action="next_batch", limit=500)
    assert f"limit={mod.MAX_BATCH}" in sent["url"]

    mod.bl_site_product(action="next_batch", limit=0)
    assert "limit=1" in sent["url"]


def test_get_sheet_returns_the_feed_material(monkeypatch):
    wire(monkeypatch, response=FEED_SHEET)
    result = json.loads(mod.bl_site_product(action="get_sheet", sku="78276"))

    assert result["feed"]["gtin"] == "50043859629256"
    assert result["feed"]["features"] == [{"name": "Marca", "value": "Fellowes"}]


def test_writing_saves_a_draft_by_default(monkeypatch):
    # A half-written sheet reaching a visitor is worse than an unwritten one.
    sent = wire(monkeypatch, response={"status": "enriched"})
    mod.bl_site_product(
        action="write_sheet", sku="78276", display_name="T", description_md="B"
    )

    assert sent["method"] == "PUT"
    assert sent["body"]["status"] == "enriched"


def test_publishing_is_explicit(monkeypatch):
    sent = wire(monkeypatch, response={"status": "owned"})
    mod.bl_site_product(
        action="write_sheet", sku="78276", display_name="T", description_md="B", publish=True
    )

    assert sent["body"]["status"] == "owned"


def test_never_submits_identifiers_or_the_fingerprint(monkeypatch):
    # These are the site's to stamp. If the tool could carry them, an agent
    # could assert a barcode the feed already knows, or pin a stale
    # fingerprint and blind change-detection on that sheet permanently.
    sent = wire(monkeypatch, response={"status": "owned"})
    mod.bl_site_product(
        action="write_sheet", sku="78276", display_name="T", description_md="B", publish=True
    )

    for forbidden in ("gtin", "mpn", "source_fingerprint"):
        assert forbidden not in sent["body"]


def test_evidence_is_only_sent_when_given(monkeypatch):
    sent = wire(monkeypatch, response={"status": "enriched"})
    mod.bl_site_product(action="write_sheet", sku="78276", description_md="B")
    assert "evidence" not in sent["body"]

    mod.bl_site_product(
        action="write_sheet", sku="78276", description_md="B", evidence=["https://x.test/a"]
    )
    assert sent["body"]["evidence"] == ["https://x.test/a"]


def test_a_refused_publication_comes_back_as_instructions(monkeypatch):
    # The gate's 422 lists what is missing. If that arrived as "HTTP 422" the
    # agent would have nothing to act on and would likely retry unchanged.
    import io

    err = urllib.error.HTTPError(
        f"{SITE}/api/product-content/78276",
        422,
        "Unprocessable",
        {},
        io.BytesIO(
            json.dumps(
                {
                    "error": "La ficha aún no cumple los requisitos",
                    "blockers": ["falta el cuerpo (description_md)"],
                }
            ).encode()
        ),
    )
    # Exercise the real _request so the HTTPError handling is what is tested.
    monkeypatch.setattr(
        mod.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(err)
    )

    out = mod.bl_site_product(
        action="write_sheet", sku="78276", display_name="T", description_md="B", publish=True
    )
    assert "description_md" in out


def test_requires_a_sku(monkeypatch):
    wire(monkeypatch, response={})
    assert "sku" in mod.bl_site_product(action="get_sheet")
    assert "sku" in mod.bl_site_product(action="write_sheet", display_name="T")


def test_requires_something_to_write(monkeypatch):
    wire(monkeypatch, response={})
    out = mod.bl_site_product(action="write_sheet", sku="78276")
    assert "display_name" in out or "description_md" in out


def test_rejects_an_unknown_action(monkeypatch):
    wire(monkeypatch, response={})
    assert "desconocida" in mod.bl_site_product(action="delete_everything")


def test_skipping_records_why(monkeypatch):
    # Skipping is the correct outcome for a product with nothing to write from.
    # Recording it is what stops the same product leading the batch tomorrow.
    sent = wire(monkeypatch, response={"status": "skipped"})
    mod.bl_site_product(
        action="skip_sheet", sku="78276", reason="el feed solo trae la marca"
    )

    assert sent["body"] == {
        "status": "skipped",
        "skip_reason": "el feed solo trae la marca",
    }


def test_skipping_demands_a_reason(monkeypatch):
    # A skip with no reason is indistinguishable from the agent giving up, and
    # leaves nobody able to see which gaps are the distributor's fault.
    wire(monkeypatch, response={})
    assert "reason" in mod.bl_site_product(action="skip_sheet", sku="78276")


def test_skipping_needs_a_sku(monkeypatch):
    wire(monkeypatch, response={})
    assert "sku" in mod.bl_site_product(action="skip_sheet", reason="sin datos")


def test_skipping_never_carries_a_body(monkeypatch):
    # A skipped product must not leave half-written prose behind on the sheet.
    sent = wire(monkeypatch, response={"status": "skipped"})
    mod.bl_site_product(action="skip_sheet", sku="78276", reason="sin datos")

    assert "description_md" not in sent["body"]
    assert "display_name" not in sent["body"]
