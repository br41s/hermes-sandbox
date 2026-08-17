"""Tests for the Website Maintenance health check.

The value of this tool is that it measures the same things every run, so the
tests pin the parts that decide what a client is told: what counts as down,
what counts as a broken link, publish drift, the empty-sitemap detection, and
the once-per-month report gate (which must survive a retried run).

Every HTTP call is faked — the point is the classification logic, not urllib.
"""

import hashlib
import json

import pytest

from tools import bl_site_health_tool as health

SITE = "https://cliente.example"

GOOD_LD = json.dumps({
    "@context": "https://schema.org",
    "@type": "Plumber",
    "name": "Fontanería García",
    "telephone": "+34600000000",
    "address": {"@type": "PostalAddress", "addressLocality": "Vigo"},
})

PAGE_HTML = f"""
<html><head><title>Inicio</title>
<meta name="description" content="Fontanería y reformas de baño en Vigo, urgencias 24 horas">
<script type="application/ld+json">{GOOD_LD}</script>
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


def page(title="Inicio", desc="Fontanería y reformas de baño en Vigo, urgencias 24 horas",
         ld=None, body=""):
    """One HTML page, for the checks that read the document rather than its status."""
    script = f'<script type="application/ld+json">{ld}</script>' if ld is not None else ""
    return (
        f"<html><head><title>{title}</title>"
        f'<meta name="description" content="{desc}">{script}</head>'
        f"<body>{body}</body></html>"
    )

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

# What a current instance's GET /api/site/status answers (bl-site-package
# PR #32). The default fetcher serves the same version from main's
# package.json, so the healthy fixture is "up to date".
STATUS = {
    "version": "1.3.0",
    "built_at": "2026-08-01T00:00:00Z",
    "last_build_ok": True,
    "smtp_configured": True,
    "notify_email_configured": True,
    "posts": {"published": 1, "draft": 1},
}

ALL_GOOD_HEADERS = {h: "x" for h in health.EXPECTED_HEADERS}


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    """Point the run history at a temp dir — it normally lives in the profile home."""
    monkeypatch.setattr(health, "_history_path", lambda: tmp_path / "history.json")


class FakeApi:
    """Stands in for the client's panel API.

    Contact is modelled as a real inbox — POST appends, GET returns — because
    the monthly form probe is only meaningful if "the message came back out"
    can actually be false.
    """

    def __init__(self):
        self.inbox: list[dict] = []
        self.posts: list[dict] = []
        self.fail_post = False
        self.swallow_post = False  # accepts the POST but never stores it
        # None = the instance predates /api/site/status and 404s it, which
        # the release check must survive (and report as drift).
        self.status: dict | None = dict(STATUS)

    def __call__(self, method, url, body=None, token=None):
        if url.endswith("/api/site/config"):
            return dict(CONFIG)
        if url.endswith("/api/site/status"):
            if self.status is None:
                raise RuntimeError("HTTP 404 from /api/site/status: Cannot GET")
            return dict(self.status)
        if url.endswith("/api/blog/posts"):
            return POSTS
        if url.endswith("/api/contact"):
            if method == "POST":
                if self.fail_post:
                    raise RuntimeError("HTTP 500 from /api/contact: boom")
                self.posts.append(body)
                if not self.swallow_post:
                    self.inbox.append({"id": len(self.inbox) + 1, **body})
                return {"success": True}
            return {"messages": list(self.inbox)}
        raise AssertionError(f"unexpected API call {url}")


@pytest.fixture(autouse=True)
def fake_api(monkeypatch):
    api = FakeApi()
    monkeypatch.setattr(health, "_http_json", api)
    monkeypatch.setattr(health, "_tls_days_remaining", lambda url: {
        "host": "cliente.example", "expires": "2027-01-01", "days_remaining": 200,
    })
    # Most clients have no previous site; the sweep opts in via OLD_SITE_URL.
    monkeypatch.setattr(health, "_get_old_site_url", lambda: None)
    return api


def make_fetcher(overrides=None, sitemap=SITEMAP_WITH_URLS):
    """Fake _fetch: everything 200 with PAGE_HTML unless overridden per URL."""
    overrides = overrides or {}

    def _fetch(url, method="GET"):
        if url in overrides:
            return {"url": url, "bytes": 100, "ms": 50, "headers": {}, "body": "", **overrides[url]}
        body = PAGE_HTML
        if url == health.RELEASED_PACKAGE_JSON_URL:
            body = json.dumps({"name": "bl-site-package", "version": STATUS["version"]})
        elif url.endswith("/sitemap.xml"):
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


def run(monkeypatch, config=None, **kwargs):
    if config is not None:
        api = health._http_json

        def _with_config(method, url, body=None, token=None):
            if url.endswith("/api/site/config"):
                return dict(config)
            return api(method, url, body=body, token=token)

        monkeypatch.setattr(health, "_http_json", _with_config)
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


def test_empty_config_fields_are_reported_never_filled(monkeypatch, fake_api):
    result = run(monkeypatch, config={
        **CONFIG, "legal_id": "", "biz_phone": "   ", "page_servicios_desc": "",
    })
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


# ---------------------------------------------------------------------------
# Social and contact endpoints
# ---------------------------------------------------------------------------

def test_malformed_contact_endpoints_are_their_own_category(monkeypatch):
    result = run(monkeypatch, config={
        **CONFIG,
        "whatsapp_number": "12",              # too few digits to dial
        "biz_phone": "600 00",                # ditto
        "legal_email": "legal-at-cliente",    # no @
    })
    found = {(i["kind"], i["source"]) for i in result["social_contact_issues"]}
    assert ("whatsapp", "config:whatsapp_number") in found
    assert ("phone", "config:biz_phone") in found
    assert ("email", "config:legal_email") in found
    # And it stays out of broken_links — "2 dead social links" has to be
    # readable as such in the report.
    assert not any("legal-at-cliente" in b["url"] for b in result["broken_links"])


def test_wellformed_contact_endpoints_are_silent(monkeypatch):
    assert run(monkeypatch)["social_contact_issues"] == []


def test_dead_social_profile_is_reported(monkeypatch):
    result = run(
        monkeypatch,
        config={**CONFIG, "biz_instagram": "https://instagram.com/se-fue"},
        overrides={"https://instagram.com/se-fue": {"status": 404}},
    )
    issues = [i for i in result["social_contact_issues"] if i["kind"] == "social"]
    assert [i["issue"] for i in issues] == ["dead_profile"]


def test_bot_blocked_social_profile_is_not_called_dead(monkeypatch):
    # Facebook/Instagram/LinkedIn answer 403/429/999 to a datacenter IP. Calling
    # that "dead" would be a monthly false positive on a working profile.
    for status in (403, 429, 999):
        result = run(
            monkeypatch,
            config={**CONFIG, "biz_facebook": "https://facebook.com/cliente"},
            overrides={"https://facebook.com/cliente": {"status": status}},
        )
        assert result["social_contact_issues"] == [], status


def test_social_field_that_is_not_a_url_is_reported_without_fetching(monkeypatch):
    result = run(monkeypatch, config={**CONFIG, "biz_facebook": "@cliente"})
    assert [i["issue"] for i in result["social_contact_issues"]] == ["invalid_url"]


def test_malformed_mailto_in_page_html_is_caught(monkeypatch):
    # _normalize skips mailto:/tel:/whatsapp: on purpose, so nothing checked
    # these until this category existed.
    result = run(monkeypatch, overrides={
        SITE + "/contacto": {
            "status": 200,
            "body": page(body='<a href="mailto:hola cliente.example">Mail</a>'),
        },
    })
    assert [(i["kind"], i["issue"]) for i in result["social_contact_issues"]] == [
        ("email", "invalid_format"),
    ]


# ---------------------------------------------------------------------------
# Duplicate / thin content
# ---------------------------------------------------------------------------

def test_identical_titles_across_pages_are_flagged(monkeypatch):
    # Every page in the fixture serves the same <title>.
    result = run(monkeypatch)
    pairs = result["duplicate_content"]["duplicate_titles"]
    assert pairs
    assert all(p["ratio"] >= health.DUPLICATE_RATIO for p in pairs)


def test_distinct_titles_and_descriptions_are_silent(monkeypatch):
    def fetcher(url, method="GET"):
        if url.endswith("/sitemap.xml"):
            body = SITEMAP_WITH_URLS
        elif url.endswith("/robots.txt"):
            body = "User-agent: *"
        else:
            slug = url.rsplit("/", 1)[-1] or "inicio"
            # Genuinely different prose, not the same sentence with one word
            # swapped — that would (correctly) trip the similarity threshold.
            body = page(
                title=f"Titulo unico de {slug}",
                desc=" ".join(
                    f"{slug}{n}" for n in hashlib.sha1(slug.encode()).hexdigest()[:14]
                ),
                ld=GOOD_LD,
            )
        return {"url": url, "status": 200, "ms": 100, "bytes": len(body),
                "headers": dict(ALL_GOOD_HEADERS) if url == SITE + "/" else {}, "body": body}

    monkeypatch.setattr(health, "_fetch", fetcher)
    dup = health._run_check(SITE, "tok")["duplicate_content"]
    assert dup["duplicate_titles"] == []
    assert dup["duplicate_descriptions"] == []
    assert dup["thin_descriptions"] == []


def test_short_meta_description_is_thin(monkeypatch):
    result = run(monkeypatch, overrides={
        SITE + "/servicios": {"status": 200, "body": page(desc="Servicios")},
    })
    thin = result["duplicate_content"]["thin_descriptions"]
    assert [t["page"] for t in thin] == ["/servicios"]
    assert thin[0]["chars"] < health.THIN_META_MIN_CHARS


def test_duplicate_content_is_never_auto_fixed(monkeypatch):
    # The tool reports; there is no write path for it. Guards the boundary.
    result = run(monkeypatch)
    assert set(result["duplicate_content"]) == {
        "duplicate_titles", "duplicate_descriptions", "missing_titles",
        "thin_descriptions", "similarity_threshold", "thin_below_chars",
    }


# ---------------------------------------------------------------------------
# Structured data
# ---------------------------------------------------------------------------

def test_valid_jsonld_produces_no_issues(monkeypatch):
    sd = run(monkeypatch)["structured_data"]
    assert sd["issues"] == []
    assert sd["blocks_checked"] > 0


def test_unparseable_jsonld_is_reported(monkeypatch):
    result = run(monkeypatch, overrides={
        SITE + "/servicios": {"status": 200, "body": page(ld="{ nope, }")},
    })
    issues = result["structured_data"]["issues"]
    assert [i["issue"] for i in issues] == ["invalid_json"]
    assert issues[0]["page"] == "/servicios"


def test_missing_required_schema_field_is_reported(monkeypatch):
    ld = json.dumps({
        "@context": "https://schema.org", "@type": "BlogPosting",
        "headline": "Como cambiar un grifo", "author": {"@type": "Organization", "name": "X"},
    })
    result = run(monkeypatch, overrides={
        SITE + "/blog/post-vivo": {"status": 200, "body": page(ld=ld)},
    })
    issues = result["structured_data"]["issues"]
    assert [(i["issue"], i["field"]) for i in issues] == [
        ("missing_required_field", "datePublished"),
    ]


def test_faq_with_an_empty_answer_is_reported(monkeypatch):
    # The exact drift class that has bitten biglobster.top twice: a FAQPage
    # whose structured data no longer matches what the page says.
    ld = json.dumps({
        "@context": "https://schema.org", "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": "Cuanto tarda?",
             "acceptedAnswer": {"@type": "Answer", "text": "Un dia"}},
            {"@type": "Question", "name": "Y el precio?",
             "acceptedAnswer": {"@type": "Answer", "text": "  "}},
        ],
    })
    result = run(monkeypatch, overrides={
        SITE + "/blog/post-vivo": {"status": 200, "body": page(ld=ld)},
    })
    issues = result["structured_data"]["issues"]
    assert [(i["issue"], i["item"]) for i in issues] == [("faq_answer_empty", 1)]


def test_article_pointing_at_the_wrong_url_is_reported(monkeypatch):
    # What a blank or stale site_url config key produces.
    ld = json.dumps({
        "@context": "https://schema.org", "@type": "BlogPosting",
        "headline": "H", "author": "A", "datePublished": "2026-01-01T00:00:00Z",
        "url": "https://otro-sitio.example/blog/post-vivo",
    })
    result = run(monkeypatch, overrides={
        SITE + "/blog/post-vivo": {"status": 200, "body": page(ld=ld)},
    })
    issue = result["structured_data"]["issues"][0]
    assert issue["issue"] == "url_mismatch"
    assert issue["expected"] == SITE + "/blog/post-vivo"


def test_localbusiness_site_wide_url_is_not_a_mismatch(monkeypatch):
    # base.njk emits the same LocalBusiness on every page, carrying the site
    # root URL by design. Flagging that would be noise on every run.
    assert run(monkeypatch)["structured_data"]["issues"] == []


def test_graph_and_list_wrappers_are_unwrapped_not_rejected(monkeypatch):
    # A @graph and a bare array are both legal JSON-LD. Reporting either as
    # broken would be a false positive on a perfectly valid page.
    ld = json.dumps({"@graph": [json.loads(GOOD_LD), json.loads(GOOD_LD)]})
    result = run(monkeypatch, overrides={
        SITE + "/servicios": {"status": 200, "body": page(ld=ld)},
    })
    assert result["structured_data"]["issues"] == []


def test_a_list_valued_type_is_not_reported_as_missing(monkeypatch):
    # schema.org allows @type: ["Store", "Plumber"].
    ld = json.dumps({
        "@context": "https://schema.org", "@type": ["Store", "Plumber"],
        "name": "Fontanería García",
    })
    result = run(monkeypatch, overrides={
        SITE + "/servicios": {"status": 200, "body": page(ld=ld)},
    })
    assert result["structured_data"]["issues"] == []


def test_hostile_jsonld_never_crashes_the_check(monkeypatch):
    # Whatever a client's template emits, the daily run has to survive it —
    # a traceback here means no report at all that day.
    ld = json.dumps([
        "una cadena suelta",
        {"@context": "https://schema.org", "@type": "FAQPage",
         "mainEntity": [{"@type": "Question", "name": 7, "acceptedAnswer": "no soy objeto"}]},
        {"@context": 5, "@type": "BlogPosting", "url": {"nope": True},
         "headline": "H", "author": "A", "datePublished": "x", "sameAs": "no soy lista"},
    ])
    result = run(monkeypatch, overrides={
        SITE + "/servicios": {"status": 200, "body": page(ld=ld)},
    })
    kinds = {i["issue"] for i in result["structured_data"]["issues"]}
    assert "not_an_object" in kinds
    assert "faq_answer_empty" in kinds
    assert "missing_context" in kinds
    assert "wrong_type" in kinds


def test_page_without_any_jsonld_is_listed(monkeypatch):
    result = run(monkeypatch, overrides={
        SITE + "/contacto": {"status": 200, "body": page(ld=None)},
    })
    assert result["structured_data"]["pages_without_jsonld"] == ["/contacto"]


# ---------------------------------------------------------------------------
# Old-site redirect sweep
# ---------------------------------------------------------------------------

OLD = "https://vieja.example"
OLD_SITEMAP = (
    "<urlset>"
    f"<url><loc>{OLD}/servicios</loc></url>"
    f"<url><loc>{OLD}/vieja-pagina</loc></url>"
    f"<url><loc>{OLD}/foto.jpg</loc></url>"
    "</urlset>"
)


def test_sweep_does_not_run_without_an_old_site(monkeypatch):
    assert run(monkeypatch)["old_site_redirects"] is None


def test_old_paths_that_dead_end_on_the_new_site_are_reported(monkeypatch):
    monkeypatch.setattr(health, "_get_old_site_url", lambda: OLD)
    result = run(monkeypatch, overrides={
        OLD + "/sitemap.xml": {"status": 200, "body": OLD_SITEMAP},
        SITE + "/vieja-pagina": {"status": 404},
    })
    sweep = result["old_site_redirects"]
    assert sweep["path_source"] == "sitemap"
    # /foto.jpg is an asset, not a lost page — it never enters the sweep.
    assert sweep["paths_checked"] == 2
    assert sweep["resolved"] == 1
    assert [d["new_url"] for d in sweep["dead_ends"]] == [SITE + "/vieja-pagina"]


def test_sweep_falls_back_to_homepage_links_without_a_sitemap(monkeypatch):
    monkeypatch.setattr(health, "_get_old_site_url", lambda: OLD)
    result = run(monkeypatch, overrides={
        OLD + "/sitemap.xml": {"status": 404},
        OLD + "/": {"status": 200, "body": page(body=(
            f'<a href="/quienes-somos">Nosotros</a>'
            f'<a href="{OLD}/tarifas">Tarifas</a>'
            f'<a href="https://otro.example/x">Fuera</a>'
        ))},
        SITE + "/tarifas": {"status": 404},
    })
    sweep = result["old_site_redirects"]
    assert sweep["path_source"] == "homepage_links"
    # Only the old site's own paths; an outbound link is not a path to migrate.
    assert sweep["paths_checked"] == 2
    assert [d["new_url"] for d in sweep["dead_ends"]] == [SITE + "/tarifas"]


# ---------------------------------------------------------------------------
# Release drift
# ---------------------------------------------------------------------------

def test_matching_versions_are_not_outdated(monkeypatch):
    release = run(monkeypatch)["release"]
    assert release == {"deployed": "1.3.0", "latest": "1.3.0", "outdated": False}


def test_stale_instance_is_outdated(monkeypatch, fake_api):
    fake_api.status = {**STATUS, "version": "1.2.0"}
    release = run(monkeypatch)["release"]
    assert release == {"deployed": "1.2.0", "latest": "1.3.0", "outdated": True}


def test_instance_ahead_of_main_is_also_outdated(monkeypatch, fake_api):
    # Deployed > released means something shipped outside the release flow.
    # Drift in either direction is BigLobster's to reconcile.
    fake_api.status = {**STATUS, "version": "1.4.0"}
    assert run(monkeypatch)["release"]["outdated"] is True


def test_instance_without_the_status_endpoint_is_null_not_a_crash(monkeypatch, fake_api):
    # An instance predating bl-site-package PR #32 404s /api/site/status.
    # That is itself drift evidence and must never take down the whole check.
    fake_api.status = None
    result = run(monkeypatch)
    assert result["release"]["deployed"] is None
    assert result["release"]["outdated"] is None
    assert result["availability"]["all_up"] is True


def test_github_outage_degrades_latest_to_unknown(monkeypatch):
    release = run(monkeypatch, overrides={
        health.RELEASED_PACKAGE_JSON_URL: {"status": 503},
    })["release"]
    assert release == {"deployed": "1.3.0", "latest": None, "outdated": None}


def test_garbage_package_json_is_unknown_not_a_crash(monkeypatch):
    release = run(monkeypatch, overrides={
        health.RELEASED_PACKAGE_JSON_URL: {
            "status": 200, "body": "<html>rate limited</html>",
        },
    })["release"]
    assert release["latest"] is None
    assert release["outdated"] is None


# ---------------------------------------------------------------------------
# Monthly contact-form probe
# ---------------------------------------------------------------------------

def test_form_probe_submits_once_and_confirms_it_persisted(monkeypatch, fake_api):
    result = run(monkeypatch)["form_check"]
    assert result["ran"] is True
    assert result["submitted"] is True
    assert result["persisted"] is True
    assert len(fake_api.posts) == 1
    assert result["marker"] in fake_api.posts[0]["message"]


def test_form_probe_never_claims_the_email_was_delivered(monkeypatch):
    # src/api/contact.js swallows SMTP errors into console.error and answers
    # success either way, so delivery itself stays unobservable. What
    # /api/site/status does answer is whether the mail path is configured.
    result = run(monkeypatch)["form_check"]
    assert result["email_delivery_verified"] is False
    assert result["smtp_configured"] is True
    assert result["notify_email_configured"] is True


def test_form_probe_surfaces_an_unconfigured_mailer(monkeypatch, fake_api):
    # The common failure: form persists fine, notification mail can never
    # arrive. The flag turns "presumably sent" into "certainly never sent".
    fake_api.status = {**STATUS, "smtp_configured": False}
    result = run(monkeypatch)["form_check"]
    assert result["smtp_configured"] is False
    assert result["persisted"] is True


def test_form_probe_mail_flags_are_null_without_the_status_endpoint(monkeypatch, fake_api):
    fake_api.status = None
    result = run(monkeypatch)["form_check"]
    assert result["smtp_configured"] is None
    assert result["notify_email_configured"] is None


def test_form_probe_runs_at_most_once_a_month_even_on_a_retry(monkeypatch, fake_api):
    run(monkeypatch)
    second = run(monkeypatch)["form_check"]
    assert second["ran"] is False
    assert second["reason"] == "already_run_this_month"
    assert len(fake_api.posts) == 1


def test_form_probe_is_stamped_before_sending(monkeypatch, fake_api):
    # A submission the site accepts but never stores must still burn the
    # month's attempt — otherwise every retry re-mails the client.
    fake_api.swallow_post = True
    first = run(monkeypatch)["form_check"]
    assert first["submitted"] is True and first["persisted"] is False
    assert run(monkeypatch)["form_check"]["ran"] is False
    assert len(fake_api.posts) == 1


def test_a_failed_submission_is_reported_and_not_retried_this_month(monkeypatch, fake_api):
    fake_api.fail_post = True
    result = run(monkeypatch)["form_check"]
    assert result["submitted"] is False
    assert "500" in result["error"]
    assert run(monkeypatch)["form_check"]["ran"] is False


def test_form_probe_is_skipped_while_the_site_is_down(monkeypatch, fake_api):
    result = run(monkeypatch, overrides={SITE + "/contacto": {"status": 502}})
    assert result["form_check"] == {
        "ran": False, "period": result["report_period"], "reason": "site_down",
    }
    assert fake_api.posts == []


def test_history_action_exposes_the_form_probes(monkeypatch):
    run(monkeypatch)
    out = json.loads(health.bl_site_health(action="history"))
    assert len(out["form_checks"]) == 1
    assert out["form_checks"][0]["result"] == "persisted"


def test_tool_refuses_without_profile_credentials(monkeypatch):
    monkeypatch.setattr(health, "_get_site_credentials", lambda: (None, None))
    out = health.bl_site_health(action="check")
    assert "BL_SITE_URL" in out


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setattr(health, "_get_site_credentials", lambda: (SITE, "pw"))
    monkeypatch.setattr(health, "_get_jwt", lambda url, pw: "tok")
