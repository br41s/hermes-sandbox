"""Behaviour tests for the redirect discovery/write tool.

Two things matter here: `find_target` must only ever trust structured data
(never guess from prose, the way `judge_by_text` is allowed to when
*confirming* a known candidate), and `propose`/`publish` must pass the site's
own validation through readably rather than swallowing it as an opaque HTTP
error.
"""

import json

import pytest


@pytest.fixture
def as_tool():
    from tools.bl_site_product_tool import _AS_TOOL

    token = _AS_TOOL.set(True)
    yield
    _AS_TOOL.reset(token)


@pytest.fixture(autouse=True)
def _dispatched_as_tool(as_tool):
    yield


from tools import bl_site_redirect_tool as mod

SITE = "https://cliente.example"


@pytest.fixture(autouse=True)
def _credentials(monkeypatch):
    monkeypatch.setattr(mod, "_get_site_credentials", lambda: (SITE, "panel-pw"))
    monkeypatch.setattr(mod, "_get_jwt", lambda _u, _p: "jwt-token")


def wire_routes(monkeypatch, routes, capture=None):
    """Answer each request by the first route fragment its URL contains."""

    def _request(method, url, token, body=None):
        if capture is not None:
            capture.append((method, url, body))
        for fragment, response in routes.items():
            if fragment in url:
                return response
        raise AssertionError(f"unexpected request to {url}")

    monkeypatch.setattr(mod, "_request", _request)


def test_reports_a_profile_without_a_site(monkeypatch):
    monkeypatch.setattr(mod, "_get_site_credentials", lambda: (None, None))
    assert "BL_SITE_URL" in mod.bl_site_redirect(action="list")


def test_refuses_to_run_from_a_script():
    from tools.bl_site_product_tool import _AS_TOOL

    token = _AS_TOOL.set(False)
    try:
        out = mod.bl_site_redirect(action="list")
    finally:
        _AS_TOOL.reset(token)
    assert "no desde un script" in out


def test_works_when_dispatched_as_a_tool(monkeypatch):
    from tools.registry import registry

    seen = {}

    def fake(**_k):
        seen["ran"] = True
        return "ok"

    monkeypatch.setattr(mod, "bl_site_redirect", fake)
    entry = registry.get_entry("bl_site_redirect")
    entry.handler({"action": "list"})
    assert seen.get("ran") is True


def test_unknown_action():
    out = mod.bl_site_redirect(action="wat")
    assert "Acción desconocida" in out


# --- find_target: discovery is structured-data-only ------------------------


def test_find_target_requires_old_path():
    assert "old_path" in mod.bl_site_redirect(action="find_target")


def test_no_archive_snapshot_is_a_clean_not_found(monkeypatch):
    monkeypatch.setattr(mod, "wayback_snapshot_url", lambda _url: None)

    out = json.loads(mod.bl_site_redirect(action="find_target", old_path="/productos/viejo.html"))
    assert out["found"] is False
    assert "archivada" in out["reason"]


def test_snapshot_fetch_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(mod, "wayback_snapshot_url", lambda _url: "https://web.archive.org/web/x")

    def boom(_url):
        raise TimeoutError("timed out")

    monkeypatch.setattr(mod, "_fetch_snapshot", boom)
    out = json.loads(mod.bl_site_redirect(action="find_target", old_path="/productos/viejo.html"))
    assert out["found"] is False
    assert "no se pudo leer" in out["reason"]


def test_snapshot_with_no_structured_identifiers_is_not_found(monkeypatch):
    monkeypatch.setattr(mod, "wayback_snapshot_url", lambda _url: "https://web.archive.org/web/x")
    monkeypatch.setattr(mod, "_fetch_snapshot", lambda _url: "<html><body>sin datos</body></html>")

    out = json.loads(mod.bl_site_redirect(action="find_target", old_path="/productos/viejo.html"))
    assert out["found"] is False
    assert "estructurados" in out["reason"]


def test_snapshot_with_no_structured_identifiers_never_queries_the_catalogue(monkeypatch):
    # A dead CONTENT page (a blog post, say) has no gtin/mpn to search on at
    # all — this must not make an unnecessary API call, let alone a wrong one.
    monkeypatch.setattr(mod, "wayback_snapshot_url", lambda _url: "https://web.archive.org/web/x")
    monkeypatch.setattr(mod, "_fetch_snapshot", lambda _url: "<html><body>un artículo</body></html>")

    def unexpected(*_a, **_k):
        raise AssertionError("should not call the catalogue lookup")

    monkeypatch.setattr(mod, "_request", unexpected)
    mod.bl_site_redirect(action="find_target", old_path="/blog/viejo-post")


def _archived_html(**fields):
    parts = ", ".join(f'"{k}": "{v}"' for k, v in fields.items())
    return (
        '<html><body><script type="application/ld+json">'
        f'{{"@type": "Product", {parts}}}'
        "</script></body></html>"
    )


def test_finds_the_live_product_by_gtin(monkeypatch):
    monkeypatch.setattr(mod, "wayback_snapshot_url", lambda _url: "https://web.archive.org/web/x")
    monkeypatch.setattr(
        mod, "_fetch_snapshot", lambda _url: _archived_html(gtin13="50043859629256")
    )
    calls = []
    wire_routes(monkeypatch, {"/api/products": {"products": [{"sku": "100", "slug": "destructora-100"}]}}, calls)

    out = json.loads(mod.bl_site_redirect(action="find_target", old_path="/productos/viejo.html"))
    assert out["found"] is True
    assert out["new_path"] == "/productos/destructora-100.html"
    assert out["match_tier"] == "gtin"
    assert out["evidence"] == {"gtin": "50043859629256"}
    assert out["sku"] == "100"
    assert any("gtin=50043859629256" in url for _m, url, _b in calls)


def test_finds_the_live_product_by_mpn_when_no_gtin(monkeypatch):
    monkeypatch.setattr(mod, "wayback_snapshot_url", lambda _url: "https://web.archive.org/web/x")
    monkeypatch.setattr(mod, "_fetch_snapshot", lambda _url: _archived_html(mpn="4691001"))
    wire_routes(monkeypatch, {"/api/products": {"products": [{"sku": "100", "slug": "destructora-100"}]}})

    out = json.loads(mod.bl_site_redirect(action="find_target", old_path="/productos/viejo.html"))
    assert out["match_tier"] == "mpn"
    assert out["evidence"] == {"mpn": "4691001"}


def test_an_identifier_with_no_current_match_is_not_found(monkeypatch):
    # The product existed once but is gone from the catalogue entirely —
    # different from "never had an identifier to check" and worth saying so.
    monkeypatch.setattr(mod, "wayback_snapshot_url", lambda _url: "https://web.archive.org/web/x")
    monkeypatch.setattr(
        mod, "_fetch_snapshot", lambda _url: _archived_html(gtin13="50043859629256")
    )
    wire_routes(monkeypatch, {"/api/products": {"products": []}})

    out = json.loads(mod.bl_site_redirect(action="find_target", old_path="/productos/viejo.html"))
    assert out["found"] is False
    assert "ningún producto vivo" in out["reason"]


# --- propose / publish / list / remove --------------------------------------


def test_propose_requires_both_paths():
    assert "old_path" in mod.bl_site_redirect(action="propose", new_path="/x")
    assert "old_path" in mod.bl_site_redirect(action="propose", old_path="/x")


def test_propose_sends_tier_and_evidence_verbatim(monkeypatch):
    calls = []
    wire_routes(monkeypatch, {"/api/redirects": {"success": True, "id": 7, "status": "pending"}}, calls)

    mod.bl_site_redirect(
        action="propose",
        old_path="/muerta",
        new_path="/productos/destructora-100.html",
        match_tier="gtin",
        evidence={"gtin": "50043859629256"},
    )

    method, url, body = calls[0]
    assert method == "POST"
    assert body == {
        "old_path": "/muerta",
        "new_path": "/productos/destructora-100.html",
        "match_tier": "gtin",
        "evidence": {"gtin": "50043859629256"},
    }


def test_propose_omits_tier_and_evidence_when_not_given(monkeypatch):
    calls = []
    wire_routes(monkeypatch, {"/api/redirects": {"success": True}}, calls)

    mod.bl_site_redirect(action="propose", old_path="/muerta", new_path="/destino")

    _method, _url, body = calls[0]
    assert "match_tier" not in body
    assert "evidence" not in body


def test_propose_surfaces_the_sites_blockers_readably(monkeypatch):
    import urllib.error

    def refuse(*_a, **_k):
        raise urllib.error.HTTPError(
            f"{SITE}/api/redirects", 422, "Unprocessable",
            {}, __import__("io").BytesIO(
                json.dumps({"blockers": ["new_path no resuelve en el sitio construido ahora mismo"]}).encode()
            ),
        )

    monkeypatch.setattr(mod.urllib.request, "urlopen", refuse)
    out = mod.bl_site_redirect(action="propose", old_path="/a", new_path="/b")
    assert "no pasa la validación" in out
    assert "no resuelve" in out


def test_publish_requires_redirect_id():
    assert "redirect_id" in mod.bl_site_redirect(action="publish")


def test_publish_posts_to_the_right_endpoint(monkeypatch):
    calls = []
    wire_routes(monkeypatch, {"/7/publish": {"success": True, "status": "live"}}, calls)

    out = json.loads(mod.bl_site_redirect(action="publish", redirect_id=7))
    assert out["status"] == "live"
    method, url, _body = calls[0]
    assert method == "POST"
    assert url.endswith("/api/redirects/7/publish")


def test_remove_requires_redirect_id():
    assert "redirect_id" in mod.bl_site_redirect(action="remove")


def test_remove_deletes_the_right_id(monkeypatch):
    calls = []
    wire_routes(monkeypatch, {"/7": {"success": True}}, calls)

    mod.bl_site_redirect(action="remove", redirect_id=7)
    method, url, _body = calls[0]
    assert method == "DELETE"
    assert url.endswith("/api/redirects/7")


def test_list_passes_through(monkeypatch):
    wire_routes(monkeypatch, {"/api/redirects": {"redirects": [{"id": 1}]}})
    out = json.loads(mod.bl_site_redirect(action="list"))
    assert out["redirects"] == [{"id": 1}]


# --- scan: run-to-run sitemap diff, two-run confirmation --------------------
#
# Moved here from bl_site_health_tool.py: detection belongs with the SEO
# agent that acts on it (every bl-site-package client's onsite-seo agent),
# not tied to whichever client happened to also buy Website Maintenance.

SITEMAP_TWO_URLS = (
    "<urlset>"
    f"<url><loc>{SITE}/</loc></url>"
    f"<url><loc>{SITE}/productos/viejo.html</loc></url>"
    "</urlset>"
)
SITEMAP_ONE_URL = f"<urlset><url><loc>{SITE}/</loc></url></urlset>"
DEAD_PATH = SITE + "/productos/viejo.html"


@pytest.fixture(autouse=True)
def _scan_history(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_scan_history_path", lambda: tmp_path / "redirect_history.json")


def run_scan(monkeypatch, sitemap, overrides=None):
    """Fake _scan_fetch: sitemap.xml returns `sitemap`; everything else 200
    unless overridden per URL."""
    overrides = overrides or {}

    def _scan_fetch(url):
        if url in overrides:
            return {"body": "", **overrides[url]}
        if url.endswith("/sitemap.xml"):
            return {"status": 200, "body": sitemap}
        return {"status": 200, "body": ""}

    monkeypatch.setattr(mod, "_scan_fetch", _scan_fetch)
    return json.loads(mod.bl_site_redirect(action="scan"))


def test_scan_first_run_ever_has_nothing_to_diff_against(monkeypatch):
    result = run_scan(monkeypatch, SITEMAP_TWO_URLS)
    assert result["checked"] == 0
    assert result["confirmed_dead"] == []


def test_scan_url_missing_next_run_and_dead_is_pending_not_confirmed(monkeypatch):
    run_scan(monkeypatch, SITEMAP_TWO_URLS)
    result = run_scan(monkeypatch, SITEMAP_ONE_URL, overrides={DEAD_PATH: {"status": 404}})
    assert result["confirmed_dead"] == []
    assert [c["url"] for c in result["pending_confirmation"]] == [DEAD_PATH]


def test_scan_url_dead_on_two_separate_runs_is_confirmed(monkeypatch):
    run_scan(monkeypatch, SITEMAP_TWO_URLS)
    run_scan(monkeypatch, SITEMAP_ONE_URL, overrides={DEAD_PATH: {"status": 404}})
    result = run_scan(monkeypatch, SITEMAP_ONE_URL, overrides={DEAD_PATH: {"status": 404}})
    assert [c["url"] for c in result["confirmed_dead"]] == [DEAD_PATH]
    assert result["pending_confirmation"] == []


def test_scan_a_5xx_does_not_advance_or_reset_confirmation(monkeypatch):
    run_scan(monkeypatch, SITEMAP_TWO_URLS)
    run_scan(monkeypatch, SITEMAP_ONE_URL, overrides={DEAD_PATH: {"status": 404}})
    # Third run: an outage, not evidence the URL is gone.
    result = run_scan(monkeypatch, SITEMAP_ONE_URL, overrides={DEAD_PATH: {"status": 503}})
    assert result["confirmed_dead"] == []
    assert result["checked"] == 1


def test_scan_url_reappearing_alive_clears_the_watch(monkeypatch):
    run_scan(monkeypatch, SITEMAP_TWO_URLS)
    run_scan(monkeypatch, SITEMAP_ONE_URL, overrides={DEAD_PATH: {"status": 404}})
    result = run_scan(monkeypatch, SITEMAP_TWO_URLS)
    assert result["checked"] == 0
    assert result["confirmed_dead"] == []
    assert result["pending_confirmation"] == []
