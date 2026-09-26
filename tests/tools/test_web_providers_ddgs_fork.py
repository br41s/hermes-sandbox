"""Fork's own tests for the DuckDuckGo (ddgs) web search provider, kept out of upstream's file so upstream merges do not conflict."""
from __future__ import annotations

import sys
import types


def _install_fake_ddgs(monkeypatch, *, text_results=None, text_raises=None, text_sleep=None):
    """Install a stub ``ddgs`` module in sys.modules for the duration of a test.

    ``text_results``: iterable of dicts to yield from DDGS().text(...).
    ``text_raises``: if set, DDGS().text raises this exception instead.
    ``text_sleep``: if set, DDGS().text blocks for this many seconds before
        yielding — simulates a hung/slow search for the timeout test.
    """
    import time as _time

    fake = types.ModuleType("ddgs")

    class _FakeDDGS:
        def __init__(self, **kwargs):
            # Accept timeout= (and any other constructor kwargs) — the provider
            # now passes DDGS(timeout=10).
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_a):
            return False
        def text(self, query, max_results=5):
            if text_sleep is not None:
                _time.sleep(text_sleep)
            if text_raises is not None:
                raise text_raises
            for hit in (text_results or []):
                yield hit

    fake.DDGS = _FakeDDGS
    monkeypatch.setitem(sys.modules, "ddgs", fake)
    # The fake module already satisfies search()'s needs — the real
    # tools.lazy_deps.ensure() would see no importlib.metadata distribution
    # for "ddgs" (sys.modules injection doesn't register one) and attempt a
    # real `pip install`. Stub it to a no-op so every test using this helper
    # stays offline.
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *a, **k: None)
    return fake


class TestDDGSProviderLazyInstall:
    def test_search_attempts_lazy_install_before_importing(self, monkeypatch):
        """search() must call tools.lazy_deps.ensure("search.ddgs") so an
        explicit `web.search_backend: ddgs` config (e.g. a freshly
        provisioned rented tenant, before the package has ever been used
        on this host) can actually install itself, instead of permanently
        failing with "ddgs package is not installed" (#174) — the other
        web search backends (exa, firecrawl, parallel) already do this at
        their own SDK-import chokepoint; ddgs never did.
        """
        monkeypatch.delitem(sys.modules, "ddgs", raising=False)
        monkeypatch.delitem(sys.modules, "plugins.web.ddgs.provider", raising=False)
        _install_fake_ddgs(monkeypatch, text_results=[
            {"title": "T", "href": "https://e.example", "body": "B"},
        ])
        # _install_fake_ddgs already stubs tools.lazy_deps.ensure to a no-op —
        # override it here with a spy so this test can assert the call shape.
        calls = []
        monkeypatch.setattr(
            "tools.lazy_deps.ensure",
            lambda feature, **kw: calls.append((feature, kw)),
        )
        from plugins.web.ddgs import provider as ddgs_provider
        from plugins.web.ddgs.provider import DDGSWebSearchProvider

        # v2026.8.31 runs the search in a spawn worker, which cannot see the
        # fake ddgs; route through the in-process helper like upstream's tests.
        from tests.tools.test_web_providers_ddgs import _force_inprocess_search

        _force_inprocess_search(monkeypatch, ddgs_provider)
        result = DDGSWebSearchProvider().search("q", limit=5)

        assert result["success"] is True
        assert calls == [("search.ddgs", {"prompt": False})]
