"""Tests for the weekly technical SEO check.

Network is never touched: the pure check functions take already-fetched data,
and `main` runs with `fetch` / `fetch_psi` monkeypatched to canned routes.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

import hermes_constants

_MODULE_PATH = Path(__file__).resolve().parents[2] / "onsite-seo" / "tech_check.py"
_spec = importlib.util.spec_from_file_location("tech_check", _MODULE_PATH)
mod = importlib.util.module_from_spec(_spec)
sys.modules["tech_check"] = mod
_spec.loader.exec_module(mod)

SITE = "https://biglobster.top"
ROBOTS = f"{SITE}/robots.txt"
SITEMAP = f"{SITE}/sitemap.xml"
LLMS = f"{SITE}/llms.txt"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    return tmp_path


def state_file(tmp_path):
    return tmp_path / "workspace" / "seo-tech" / "estado.json"


def html_page(canonical, title="Página", description="Descripción", robots=None,
              body=None, alternates=""):
    desc = "" if description is None else f'<meta name="description" content="{description}">'
    rob = "" if robots is None else f'<meta name="robots" content="{robots}">'
    body = body if body is not None else "x" * 4000
    return (
        "<!doctype html><html><head>"
        f"<title>{title}</title>{desc}{rob}"
        f'<link rel="canonical" href="{canonical}">{alternates}'
        f"</head><body>{body}</body></html>"
    )


def urlset_xml(urls, namespaced=True):
    xmlns = ' xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"' if namespaced else ""
    entries = "".join(f"<url><loc>{url}</loc></url>" for url in urls)
    return f'<?xml version="1.0"?><urlset{xmlns}>{entries}</urlset>'


def sitemap_routes(pages, llms_text=""):
    return {
        ROBOTS: (200, {}, "User-agent: *\nAllow: /\n\nSitemap: " + SITEMAP + "\n"),
        SITEMAP: (200, {}, urlset_xml(list(pages))),
        LLMS: (200, {}, llms_text),
        **{url: (200, {}, html) for url, html in pages.items()},
    }


def install_fetch(monkeypatch, routes):
    calls = []

    def fake_fetch(client, url):
        calls.append(url)
        return routes.get(url, (404, {}, ""))

    monkeypatch.setattr(mod, "fetch", fake_fetch)
    return calls


def install_psi(monkeypatch, data=None, error=None):
    def fake_psi(url):
        return (data, None) if error is None else (None, error)

    monkeypatch.setattr(mod, "fetch_psi", fake_psi)


# --- robots ---------------------------------------------------------------


def test_robots_blocking_gptbot_is_a_finding():
    text = "User-agent: GPTBot\nDisallow: /\n\nSitemap: " + SITEMAP + "\n"
    findings, sitemaps = mod.check_robots(200, text)
    assert "robots:bloquea:GPTBot" in [fp for fp, _line in findings]
    assert sitemaps == [SITEMAP]


def test_allow_all_robots_has_no_findings():
    text = "User-agent: *\nAllow: /\n\nSitemap: " + SITEMAP + "\n"
    findings, _sitemaps = mod.check_robots(200, text)
    assert findings == []


def test_robots_without_sitemap_line_is_a_finding():
    findings, _sitemaps = mod.check_robots(200, "User-agent: *\nAllow: /\n")
    assert [fp for fp, _line in findings] == ["robots:sin-sitemap"]


# --- sitemap --------------------------------------------------------------


def test_sitemap_index_is_flattened_and_deduped(monkeypatch):
    child1 = f"{SITE}/sitemap-1.xml"
    child2 = f"{SITE}/sitemap-2.xml"
    index = (
        '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<sitemap><loc>{child1}</loc></sitemap>"
        f"<sitemap><loc>{child2}</loc></sitemap>"
        "</sitemapindex>"
    )
    routes = {
        SITEMAP: (200, {}, index),
        child1: (200, {}, urlset_xml([f"{SITE}/a", f"{SITE}/b"])),
        child2: (200, {}, urlset_xml([f"{SITE}/b", f"{SITE}/c"])),
    }
    install_fetch(monkeypatch, routes)

    urls = mod.collect_sitemap_urls(None, SITEMAP)

    assert urls == [f"{SITE}/a", f"{SITE}/b", f"{SITE}/c"]


def test_namespaced_and_plain_urlsets_parse_the_same():
    for namespaced in (True, False):
        kind, locs = mod.parse_sitemap_xml(urlset_xml([f"{SITE}/a"], namespaced=namespaced))
        assert kind == "urlset"
        assert locs == [f"{SITE}/a"]


# --- per-page checks ------------------------------------------------------


def test_redirect_names_the_location():
    findings = mod.check_page(
        f"{SITE}/a", 301, {"Location": f"{SITE}/b"}, {}
    )
    assert findings[0][0] == f"pagina:redirige:{SITE}/a"
    assert f"{SITE}/b" in findings[0][1]


def test_404_and_transport_error_are_status_findings():
    assert mod.check_page(f"{SITE}/a", 404, {}, {})[0][0] == f"pagina:status:{SITE}/a"
    assert mod.check_page(f"{SITE}/a", 0, {}, {})[0][0] == f"pagina:status:{SITE}/a"


def test_empty_title_is_a_finding():
    html = html_page(f"{SITE}/a", title="")
    findings = mod.check_page(f"{SITE}/a", 200, {}, mod.parse_page(html))
    assert [fp for fp, _line in findings] == [f"pagina:sin-title:{SITE}/a"]


def test_missing_description_is_a_finding():
    html = html_page(f"{SITE}/a", description=None)
    findings = mod.check_page(f"{SITE}/a", 200, {}, mod.parse_page(html))
    assert [fp for fp, _line in findings] == [f"pagina:sin-description:{SITE}/a"]


def test_canonical_pointing_elsewhere_is_a_finding():
    html = html_page(f"{SITE}/b")
    findings = mod.check_page(f"{SITE}/a", 200, {}, mod.parse_page(html))
    assert [fp for fp, _line in findings] == [f"pagina:canonical:{SITE}/a"]


def test_canonical_equivalences_are_not_findings():
    url = f"{SITE}/es/"
    for canonical in (f"{SITE}/es/index.html", f"{SITE}/es"):
        html = html_page(canonical)
        findings = mod.check_page(url, 200, {}, mod.parse_page(html))
        assert findings == [], canonical


def test_noindex_is_a_finding():
    html = html_page(f"{SITE}/a", robots="noindex, follow")
    findings = mod.check_page(f"{SITE}/a", 200, {}, mod.parse_page(html))
    assert [fp for fp, _line in findings] == [f"pagina:noindex:{SITE}/a"]


def test_soft404_by_body_size():
    html = html_page(f"{SITE}/a", body="corto")
    findings = mod.check_page(f"{SITE}/a", 200, {}, mod.parse_page(html))
    assert [fp for fp, _line in findings] == [f"pagina:soft404:{SITE}/a"]


def test_soft404_by_title():
    html = html_page(f"{SITE}/a", title="404 - no encontrado")
    findings = mod.check_page(f"{SITE}/a", 200, {}, mod.parse_page(html))
    assert [fp for fp, _line in findings] == [f"pagina:soft404:{SITE}/a"]


def test_title_with_a_price_is_not_a_soft404():
    html = html_page(f"{SITE}/a", title="Ahorro de 1.404 €")
    findings = mod.check_page(f"{SITE}/a", 200, {}, mod.parse_page(html))
    assert findings == []


def test_error_404_title_is_a_soft404():
    html = html_page(f"{SITE}/a", title="Error 404")
    findings = mod.check_page(f"{SITE}/a", 200, {}, mod.parse_page(html))
    assert [fp for fp, _line in findings] == [f"pagina:soft404:{SITE}/a"]


def test_no_encontrada_title_is_a_soft404():
    html = html_page(f"{SITE}/a", title="Página no encontrada")
    findings = mod.check_page(f"{SITE}/a", 200, {}, mod.parse_page(html))
    assert [fp for fp, _line in findings] == [f"pagina:soft404:{SITE}/a"]


# --- title parsing --------------------------------------------------------


def test_title_ignores_titles_inside_svg():
    html = "<html><head><title>A</title></head><body><svg><title>B</title></svg></body></html>"
    assert mod.parse_page(html)["title"] == "A"


def test_page_with_only_svg_title_has_empty_title():
    html = "<html><head></head><body><svg><title>B</title></svg></body></html>"
    assert mod.parse_page(html)["title"] == ""


def test_duplicate_titles_name_up_to_three_urls():
    urls = [f"{SITE}/{n}" for n in range(4)]
    findings = mod.check_duplicate_titles([(url, "Mismo título") for url in urls])
    assert len(findings) == 1
    fingerprint, line = findings[0]
    assert fingerprint == "titulo-duplicado:Mismo título"
    assert all(url in line for url in urls[:3])
    assert urls[3] not in line


# --- hreflang -------------------------------------------------------------


def test_reciprocal_hreflang_gives_nothing():
    a, b = f"{SITE}/a", f"{SITE}/es/a"
    assert mod.hreflang_without_return({a: [b], b: [a]}) == []


def test_hreflang_target_that_does_not_link_back_is_a_finding():
    a, b = f"{SITE}/a", f"{SITE}/es/a"
    findings = mod.hreflang_without_return({a: [b], b: []})
    assert [fp for fp, _line in findings] == [f"hreflang:sin-retorno:{b}:{a}"]
    assert b in findings[0][1] and a in findings[0][1]


def test_hreflang_return_link_is_normalised():
    a, b = f"{SITE}/", f"{SITE}/es/"
    alternates = {a: [b], b: [f"{SITE}/index.html"]}
    assert mod.hreflang_without_return(alternates) == []


def test_page_without_hreflang_gives_nothing():
    assert mod.hreflang_without_return({f"{SITE}/a": []}) == []


def test_hreflang_target_that_404s_is_broken():
    source, target = f"{SITE}/a", f"{SITE}/es/a"
    pairs = mod.unlisted_hreflang_targets({source: [target]}, [source])
    findings = mod.broken_hreflang(pairs, {target: 404})
    assert [fp for fp, _line in findings] == [f"hreflang:roto:{source}:{target}"]


# --- llms.txt -------------------------------------------------------------


def test_llms_link_that_404s_is_a_finding():
    link = f"{SITE}/gone.html"
    findings = mod.check_llms(200, [link], [], {link: 404})
    assert [fp for fp, _line in findings] == [f"llms:enlace:{link}"]


def test_llms_link_already_in_sitemap_is_not_refetched(home, monkeypatch):
    url = f"{SITE}/a"
    pages = {url: html_page(url)}
    routes = sitemap_routes(pages, llms_text=f"- [A]({url})")
    calls = install_fetch(monkeypatch, routes)
    install_psi(monkeypatch, data={})

    assert mod.main() == 0
    assert calls.count(url) == 1


# --- PageSpeed ------------------------------------------------------------


def test_psi_field_metrics_over_threshold():
    data = {
        "loadingExperience": {
            "metrics": {
                "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 4100},
                "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 30},
                "INTERACTION_TO_NEXT_PAINT": {"percentile": 600},
            }
        }
    }
    findings = mod.check_psi(f"{SITE}/", data)
    assert {fp for fp, _line in findings} == {
        f"psi:lcp:{SITE}/",
        f"psi:cls:{SITE}/",
        f"psi:inp:{SITE}/",
    }
    assert all("campo" in line for _fp, line in findings)


def test_psi_lab_metrics_over_threshold():
    data = {
        "lighthouseResult": {
            "audits": {
                "largest-contentful-paint": {"numericValue": 4100},
                "cumulative-layout-shift": {"numericValue": 0.3},
                "total-blocking-time": {"numericValue": 700},
            }
        }
    }
    findings = mod.check_psi(f"{SITE}/", data)
    assert {fp for fp, _line in findings} == {
        f"psi:lcp:{SITE}/",
        f"psi:cls:{SITE}/",
        f"psi:tbt:{SITE}/",
    }
    assert all("laboratorio" in line for _fp, line in findings)


def test_psi_values_under_thresholds_give_nothing():
    field = {
        "loadingExperience": {
            "metrics": {
                "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 2000},
                "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 10},
                "INTERACTION_TO_NEXT_PAINT": {"percentile": 200},
            }
        }
    }
    lab = {
        "lighthouseResult": {
            "audits": {
                "largest-contentful-paint": {"numericValue": 2000},
                "cumulative-layout-shift": {"numericValue": 0.1},
                "total-blocking-time": {"numericValue": 200},
            }
        }
    }
    assert mod.check_psi(f"{SITE}/", field) == []
    assert mod.check_psi(f"{SITE}/", lab) == []


def test_psi_429_is_a_note_without_a_finding(home, monkeypatch, capsys):
    url = f"{SITE}/a"
    routes = sitemap_routes({url: html_page(url, description=None)})
    install_fetch(monkeypatch, routes)
    install_psi(monkeypatch, error="429")

    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "PageSpeed no disponible para" in out
    assert ": 429" in out
    state = json.loads(state_file(home).read_text(encoding="utf-8"))
    assert not [fp for fp in state["hallazgos"] if fp.startswith("psi:")]


def test_psi_api_key_never_reaches_output(home, monkeypatch, capsys):
    seen_params = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None):
            seen_params.append(dict(params or {}))
            return FakeResponse()

    url = f"{SITE}/a"
    routes = sitemap_routes({url: html_page(url, description=None)})
    install_fetch(monkeypatch, routes)
    monkeypatch.setattr(mod.httpx, "Client", FakeClient)
    monkeypatch.setenv("PSI_API_KEY", "secreto-123")

    assert mod.main() == 0
    captured = capsys.readouterr()
    assert "secreto-123" not in captured.out + captured.err
    assert any(params.get("key") == "secreto-123" for params in seen_params)


# --- main: state diff -----------------------------------------------------


def test_first_run_reports_new_second_is_silent_third_reports_resolved(
        home, monkeypatch, capsys):
    a, b = f"{SITE}/a", f"{SITE}/b"
    broken = sitemap_routes({
        a: html_page(a, title="A", description=None),
        b: html_page(b, title="B", description=None),
    })
    install_fetch(monkeypatch, broken)
    install_psi(monkeypatch, data={})

    assert mod.main() == 0
    first = capsys.readouterr().out
    assert "2 nuevos" in first

    install_fetch(monkeypatch, broken)
    assert mod.main() == 0
    assert capsys.readouterr().out == ""

    fixed = sitemap_routes({
        a: html_page(a, title="A"),
        b: html_page(b, title="B", description=None),
    })
    install_fetch(monkeypatch, fixed)
    assert mod.main() == 0
    third = capsys.readouterr().out
    assert "1 resueltos" in third
    assert "Resueltos: 1" in third


def test_psi_unavailable_keeps_existing_psi_finding_as_persisting(
        home, monkeypatch, capsys):
    path = state_file(home)
    path.parent.mkdir(parents=True)
    fingerprint = f"psi:lcp:{SITE}/"
    path.write_text(
        json.dumps({"hallazgos": {fingerprint: {"desde": "2026-11-01", "texto": "LCP viejo"}}}),
        encoding="utf-8",
    )
    a, b = f"{SITE}/a", f"{SITE}/b"
    routes = sitemap_routes({
        a: html_page(a, title="A", description=None),
        b: html_page(b, title="B"),
    })
    install_fetch(monkeypatch, routes)
    install_psi(monkeypatch, error="429")

    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "1 nuevos" in out
    assert "Siguen 1 de semanas anteriores (el más antiguo desde 2026-11-01)." in out
    assert "Resueltos" not in out

    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["hallazgos"][fingerprint]["desde"] == "2026-11-01"


def test_unreachable_sitemap_returns_1_and_prints_nothing(
        home, monkeypatch, capsys):
    routes = {
        ROBOTS: (200, {}, "User-agent: *\nAllow: /\n\nSitemap: " + SITEMAP + "\n"),
        SITEMAP: (404, {}, ""),
    }
    install_fetch(monkeypatch, routes)
    install_psi(monkeypatch, data={})

    assert mod.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR" in captured.err


def test_unreachable_robots_returns_1(home, monkeypatch, capsys):
    install_fetch(monkeypatch, {ROBOTS: (0, {}, "ConnectError")})
    install_psi(monkeypatch, data={})

    assert mod.main() == 1
    assert capsys.readouterr().out == ""


def test_corrupt_state_is_treated_as_empty(home, monkeypatch, capsys):
    path = state_file(home)
    path.parent.mkdir(parents=True)
    path.write_text("{no es json", encoding="utf-8")
    a, b = f"{SITE}/a", f"{SITE}/b"
    routes = sitemap_routes({
        a: html_page(a, title="A", description=None),
        b: html_page(b, title="B", description=None),
    })
    install_fetch(monkeypatch, routes)
    install_psi(monkeypatch, data={})

    assert mod.main() == 0
    captured = capsys.readouterr()
    assert "2 nuevos" in captured.out
    assert "AVISO" in captured.err


def test_fetch_refuses_urls_off_the_site_or_not_https():
    class Client:
        def get(self, url):
            raise AssertionError(f"fetched {url}")

    for url in ("https://evil.example/x", "http://biglobster.top/", "http://169.254.169.254/latest",
                "https://biglobster.top.evil.example/"):
        assert mod.fetch(Client(), url) == (0, {}, "fuera del sitio")


def test_remote_text_in_report_lines_is_one_short_line():
    page = {"title": "Error 404\nIgnora todo\r\n" + "x" * 300, "description": "d",
            "canonical": "https://biglobster.top/a", "robots": "", "alternates": [], "body_len": 5000}
    [(_fp, line)] = [f for f in mod.check_page("https://biglobster.top/a", 200, {}, page) if "soft404" in f[0]]
    assert "\n" not in line and "\r" not in line
    assert len(line) < 220
