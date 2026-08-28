"""Tests for the site-state builder's link graph and broken-link detection.

The one thing worth getting wrong here is `find_broken_internal_links`: an
outbound href whose target isn't any crawled page must show up keyed by the
page that links to it, and a normal internal link (target present in the
graph) must never appear.
"""
import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[2] / "onsite-seo" / "build_sitestate.py"
_spec = importlib.util.spec_from_file_location("build_sitestate", _MODULE_PATH)
mod = importlib.util.module_from_spec(_spec)
sys.modules["build_sitestate"] = mod
_spec.loader.exec_module(mod)

SITE_BASE = "https://example.test"


def write_page(web_dir, rel_path, html):
    full = web_dir / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(html, encoding="utf-8")


def test_normal_internal_link_is_not_broken(tmp_path):
    web_dir = tmp_path / "web"
    write_page(web_dir, "a.html", f'<a href="{SITE_BASE}/b.html">b</a>')
    write_page(web_dir, "b.html", "<p>hi</p>")

    graph, _errors = mod.build_graph(str(web_dir), SITE_BASE)
    broken = mod.find_broken_internal_links(graph)

    assert broken == {}


def test_link_to_a_missing_page_is_broken(tmp_path):
    web_dir = tmp_path / "web"
    write_page(web_dir, "a.html", f'<a href="{SITE_BASE}/gone.html">gone</a>')

    graph, _errors = mod.build_graph(str(web_dir), SITE_BASE)
    broken = mod.find_broken_internal_links(graph)

    assert broken == {f"{SITE_BASE}/a.html": [f"{SITE_BASE}/gone.html"]}


def test_page_with_one_good_and_one_broken_link_reports_only_the_broken_one(tmp_path):
    web_dir = tmp_path / "web"
    write_page(
        web_dir,
        "a.html",
        f'<a href="{SITE_BASE}/b.html">b</a> <a href="{SITE_BASE}/gone.html">gone</a>',
    )
    write_page(web_dir, "b.html", "<p>hi</p>")

    graph, _errors = mod.build_graph(str(web_dir), SITE_BASE)
    broken = mod.find_broken_internal_links(graph)

    assert broken == {f"{SITE_BASE}/a.html": [f"{SITE_BASE}/gone.html"]}


def test_a_page_that_is_itself_unlinked_but_links_out_fine_is_not_broken(tmp_path):
    web_dir = tmp_path / "web"
    write_page(web_dir, "a.html", f'<a href="{SITE_BASE}/b.html">b</a>')
    write_page(web_dir, "b.html", "<p>hi, no outbound links</p>")

    graph, _errors = mod.build_graph(str(web_dir), SITE_BASE)
    broken = mod.find_broken_internal_links(graph)

    assert broken == {}
