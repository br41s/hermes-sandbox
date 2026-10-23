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
            yield from (text_results or [])

    fake.DDGS = _FakeDDGS
    monkeypatch.setitem(sys.modules, "ddgs", fake)
    # The fake module already satisfies search()'s needs — the real
    # lazy_ensure() would see no installed distribution for "ddgs"
    # (sys.modules injection doesn't register one) and ask PM to install
    # the extra. Stub it to a no-op so every test using this helper stays offline.
    monkeypatch.setattr("plugins.web._common.lazy_ensure", lambda *a, **k: None)
    return fake


class TestDDGSProviderLazyInstall:
    def test_search_attempts_lazy_install_before_importing(self, monkeypatch):
        """search() must call lazy_ensure("ddgs") (PM's [ddgs] extra) so an
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
        # _install_fake_ddgs already stubs lazy_ensure to a no-op —
        # override it here with a spy so this test can assert the call shape.
        calls = []
        monkeypatch.setattr(
            "plugins.web._common.lazy_ensure",
            lambda extra: calls.append(extra),
        )
        # import_module, not `from plugins.web.ddgs import provider`: with the
        # sys.modules entry dropped above, that form returns the package's stale
        # attribute while a second import loads a fresh module, so the helper
        # below would patch a different module than the one under test.
        import importlib

        ddgs_provider = importlib.import_module("plugins.web.ddgs.provider")
        DDGSWebSearchProvider = ddgs_provider.DDGSWebSearchProvider

        # v2026.8.31 runs the search in a spawn worker, which cannot see the
        # fake ddgs; route through the in-process helper like upstream's tests.
        from tests.tools.test_web_providers_ddgs import _force_inprocess_search

        _force_inprocess_search(monkeypatch, ddgs_provider)
        result = DDGSWebSearchProvider().search("q", limit=5)

        assert result["success"] is True
        assert calls == ["ddgs"]


# TestDDGSWorkerSeesDurableLazyTarget was removed with upstream v0.22: HERMES_LAZY_INSTALL_TARGET
# and tools.lazy_deps.activate_durable_lazy_target no longer exist. PM installs extras into
# dependency generations, and _spawn_worker merges the launcher-composed PYTHONPATH into the
# worker's env, which is upstream's fix for the same "installed but not importable in the
# worker" failure (2026-09-27).
