"""Tests for the Website Maintenance health check.

The value of this tool is that it measures the same things every run, so the
tests pin the parts that decide what a client is told: what counts as down,
what counts as a broken link, publish drift, the empty-sitemap detection, and
the once-per-month report gate (which must survive a retried run).

Every HTTP call is faked — the point is the classification logic, not urllib.
"""

import json

import pytest

from tools import bl_site_health_tool as health

SITE = "https://cliente.example"

PAGE_HTML = """
<html><head><title>Inicio</title>
<meta name="description" content="Fontanería en Vigo">
</head><body>
<a href="/servicios">Servicios</a>
<a href="/pagina-inventada">Rota</a>
<a href="https://externo.example/roto">Fuera</a>
<a href="mailto:hola@cliente.example">Mail</a>
<img src="/uploads/a.webp" alt="Un baño reformado">
<img src="/uploads/b.webp" alt="">
<img src="https://otrodominio.example/c.jpg" alt="Foto ajena">
</body></html>
"""

SITEMAP_WITH_URLS = "<urlset><url><loc>https://cliente.example/</loc></url></urlset>"
SITEMAP_EMPTY = "<urlset></urlset>"

CONFIG = {
    "company_name": "Fontanería García",
    "sector": "Instalaciones",
    "site_url": SITE,
    "page_index_title": "Fontanería en Vigo",
    "page_index_subtitle": "Urgencias 24h",
    "page_index_desc": "Reparaciones y reformas",
    "page_quienes_title": "Quiénes somos",
    "page_quienes_desc": "Equipo propio",
    "page_servicios_title": "Servicios",
    "page_servicios_desc": "Lo que hacemos",
    "page_contacto_title": "Contacto",
    "page_contacto_desc": "Escríbenos",
    "page_blog_title": "Blog",
    "legal_name": "García e Hijos SL",
    "legal_id": "B12345678",
    "legal_address": "Calle Real 1",
    "legal_email": "legal@cliente.example",
    "biz_city": "Vigo",
    "biz_phone": "600000000",
    "biz_street": "Calle Real 1",
    "biz_postal_code": "36201",
}

POSTS = {"posts": [
    {"id": 1, "slug": "post-vivo", "title": "Post vivo", "status": "published"},
    {"id": 2, "slug": "post-borrador", "title": "Borrador", "status": "draft"},
]}

ALL_GOOD_HEADERS = {h: "x" for h in health.EXPECTED_HEADERS}


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    """Point the run history at a temp dir — it normally lives in the profile home."""
    monkeypatch.setattr(health, "_history_path", lambda: tmp_path / "history.json")


@pytest.fixture(autouse=True)
def fake_api(monkeypatch):
    def _api(method, url, body=None, token=None):
        if url.endswith("/api/site/config"):
            return dict(CONFIG)
        if url.endswith("/api/blog/posts"):
            return POSTS
        raise AssertionError(f"unexpected API call {url}")

    monkeypatch.setattr(health, "_http_json", _api)
    monkeypatch.setattr(health, "_tls_days_remaining", lambda url: {
        "host": "cliente.example", "expires": "2027-01-01", "days_remaining": 200,
    })


def make_fetcher(overrides=None, sitemap=SITEMAP_WITH_URLS):
    """Fake _fetch: everything 200 with PAGE_HTML unless overridden per URL."""
    overrides = overrides or {}

    def _fetch(url, method="GET"):
        if url in overrides:
            return {"url": url, "bytes": 100, "ms": 50, "headers": {}, "body": "", **overrides[url]}
        body = PAGE_HTML
        if url.endswith("/sitemap.xml"):
            body = sitemap
        elif url.endswith("/robots.txt"):
            body = "User-agent: *"
        return {
            "url": url,
            "status": 200,
            "ms": 100,
            "bytes": len(body),
            "headers": dict(ALL_GOOD_HEADERS) if url == SITE + "/" else {},
            "body": body,
        }

    return _fetch


def run(monkeypatch, **kwargs):
    monkeypatch.setattr(health, "_fetch", make_fetcher(**kwargs))
    return health._run_check(SITE, "tok")


def test_healthy_site_reports_all_up(monkeypatch):
    result = run(monkeypatch)
    assert result["availability"]["all_up"] is True
    assert result["availability"]["down"] == []
    assert result["missing_security_headers"] == []
    assert result["publish_drift"] == []


def test_a_down_route_is_an_outage(monkeypatch):
    result = run(monkeypatch, overrides={SITE + "/contacto": {"status": 502}})
    assert result["availability"]["all_up"] is False
    assert [d["path"] for d in result["availability"]["down"]] == ["/contacto"]


def test_transport_failure_counts_as_down(monkeypatch):
    # status 0 is what _fetch returns for DNS/refused/TLS/timeouts. "Did not
    # answer" and "answered 500" are both outages and must both survive.
    result = run(monkeypatch, overrides={SITE + "/servicios": {"status": 0, "error": "timed out"}})
    down = result["availability"]["down"]
    assert [d["path"] for d in down] == ["/servicios"]
    assert down[0]["error"] == "timed out"


def test_broken_internal_and_external_links_are_found(monkeypatch):
    result = run(monkeypatch, overrides={
        SITE + "/pagina-inventada": {"status": 404},
        "https://externo.example/roto": {"status": 404},
    })
    urls = {b["url"]: b for b in result["broken_links"]}
    assert urls[SITE + "/pagina-inventada"]["internal"] is True
    assert urls["https://externo.example/roto"]["internal"] is False
    # The page each broken link was found on is what makes it fixable.
    assert "/" in urls[SITE + "/pagina-inventada"]["found_on"]


def test_mailto_and_anchors_are_never_checked(monkeypatch):
    result = run(monkeypatch)
    assert not any("mailto:" in b["url"] for b in result["broken_links"])


def test_valid_internal_routes_are_published_for_the_agent(monkeypatch):
    # The agent rewrites broken internal links to one of these and may not
    # invent a route, so the list has to come from the tool, not the prompt.
    result = run(monkeypatch)
    assert "/servicios" in result["valid_internal_routes"]
    assert "/privacidad" in result["valid_internal_routes"]


def test_publish_drift_is_a_published_post_that_404s(monkeypatch):
    result = run(monkeypatch, overrides={SITE + "/blog/post-vivo": {"status": 404}})
    assert [d["slug"] for d in result["publish_drift"]] == ["post-vivo"]


def test_drafts_are_not_expected_to_be_reachable(monkeypatch):
    result = run(monkeypatch)
    assert all(d["slug"] != "post-borrador" for d in result["publish_drift"])
    assert result["posts"] == {"published": 1, "total": 2}


def test_empty_sitemap_is_detected(monkeypatch):
    result = run(monkeypatch, sitemap=SITEMAP_EMPTY)
    assert result["sitemap"]["urls"] == 0


def test_missing_security_header_is_reported(monkeypatch):
    headers = {h: "x" for h in health.EXPECTED_HEADERS if h != "strict-transport-security"}
    result = run(monkeypatch, overrides={
        SITE + "/": {"status": 200, "headers": headers, "body": PAGE_HTML},
    })
    assert result["missing_security_headers"] == ["strict-transport-security"]


def test_image_issues_split_missing_alt_from_hotlinked(monkeypatch):
    result = run(monkeypatch)
    issues = {(i["src"], i["issue"]) for i in result["image_issues"]}
    assert ("/uploads/b.webp", "missing_alt") in issues
    assert ("https://otrodominio.example/c.jpg", "hotlinked") in issues
    assert not any(i["src"] == "/uploads/a.webp" for i in result["image_issues"])


def test_slow_and_heavy_pages_use_fixed_budgets(monkeypatch):
    result = run(monkeypatch, overrides={
        SITE + "/servicios": {
            "status": 200, "ms": health.SLOW_PAGE_MS + 1,
            "bytes": health.HEAVY_PAGE_BYTES + 1, "body": PAGE_HTML,
        },
    })
    assert [p["path"] for p in result["slow_pages"]] == ["/servicios"]
    assert [p["path"] for p in result["heavy_pages"]] == ["/servicios"]


def test_empty_config_fields_are_reported_never_filled(monkeypatch):
    def _api(method, url, body=None, token=None):
        if url.endswith("/api/site/config"):
            return {**CONFIG, "legal_id": "", "biz_phone": "   ", "page_servicios_desc": ""}
        return POSTS

    monkeypatch.setattr(health, "_http_json", _api)
    result = run(monkeypatch)
    assert result["empty_legal_fields"] == ["legal_id"]
    assert result["empty_business_fields"] == ["biz_phone"]
    assert result["empty_page_fields"] == ["page_servicios_desc"]


def test_history_accumulates_and_rolls_up(monkeypatch):
    run(monkeypatch)
    run(monkeypatch, overrides={SITE + "/": {"status": 500}})
    result = run(monkeypatch)
    assert result["history"]["runs_recorded"] == 3
    assert result["history"]["incidents_in_window"] == 1
    assert result["history"]["uptime_pct"] == pytest.approx(66.7)


def test_report_is_due_once_per_month_and_stamping_is_idempotent(monkeypatch):
    result = run(monkeypatch)
    period = result["report_period"]
    assert result["report_due"] is True

    first = json.loads(health.bl_site_health(action="record_report", period=period, summary="ok"))
    assert first == {"success": True, "period": period, "already_recorded": False}

    # A retried run must not produce a second report for the same month.
    assert run(monkeypatch)["report_due"] is False
    second = json.loads(health.bl_site_health(action="record_report", period=period))
    assert second["already_recorded"] is True


def test_tool_refuses_without_profile_credentials(monkeypatch):
    monkeypatch.setattr(health, "_get_site_credentials", lambda: (None, None))
    out = health.bl_site_health(action="check")
    assert "BL_SITE_URL" in out


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setattr(health, "_get_site_credentials", lambda: (SITE, "pw"))
    monkeypatch.setattr(health, "_get_jwt", lambda url, pw: "tok")
