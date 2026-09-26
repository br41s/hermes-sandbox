"""Tests for web_search transient-outage resilience.

Regression cover for the 2026-09-16 Content Updater loss (session
``cron_4a0ebe8779ca_20260916_232408``): Exa returned
``503 "temporarily over capacity. Please retry with exponential backoff"``,
the agent loop re-issued ``web_search`` five times with no wait, and
``same_tool_failure_halt`` killed the run.

Covers:
- failure classification (transient vs permanent vs interrupted)
- in-call backoff, and that it is bounded by a shared wall-clock budget
- fallback to a second provider, and the billing-critical rule that a
  tenant on ddgs can never fall back onto Exa (issue #174)
- an outage staying visibly distinct from an empty result set
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from tools import web_search_resilience as wsr
from tests.tools.conftest import register_all_web_providers


EXA_503 = (
    "Exa search failed: Request failed with status code 503: "
    '{"error":"Exa is temporarily over capacity. Please retry with exponential backoff"}'
)


class FakeProvider:
    """Scriptable WebSearchProvider stand-in.

    ``responses`` is consumed one entry per ``search()`` call; the last entry
    repeats once exhausted, so a permanently-down provider is expressed as a
    single-element list.
    """

    def __init__(self, name: str, responses: List[Any], *, available: bool = True,
                 supports: bool = True):
        self._name = name
        self._responses = list(responses)
        self._available = available
        self._supports = supports
        self.calls: List[tuple] = []

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self._available

    def supports_search(self) -> bool:
        return self._supports

    def search(self, query: str, limit: int = 5):
        self.calls.append((query, limit))
        item = self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def ok(n: int = 1) -> Dict[str, Any]:
    return {
        "success": True,
        "data": {"web": [{"title": f"r{i}", "url": f"https://e/{i}",
                          "description": "", "position": i} for i in range(n)]},
    }


def fail(msg: str) -> Dict[str, Any]:
    return {"success": False, "error": msg}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
class TestClassifySearchFailure:
    @pytest.mark.parametrize("text", [
        EXA_503,
        "Request failed with status code 429",
        "HTTP 502 Bad Gateway",
        "status=504",
        "Server error (500)",
        "Exa is temporarily over capacity. Please retry with exponential backoff",
        "httpx.ConnectError: [Errno 61] Connection refused",
        "Read timed out after 30s",
        "The remote end closed connection without response",
        "Rate limit exceeded, try again later",
    ])
    def test_transient(self, text):
        assert wsr.classify_search_failure(text) == wsr.TRANSIENT

    @pytest.mark.parametrize("text", [
        "Request failed with status code 401: unauthorized",
        "Request failed with status code 403",
        "HTTP 400 Bad Request: query must not be empty",
        "EXA_API_KEY is not set",
        "Exa SDK not installed: No module named 'exa_py'",
        "status code 422: validation error",
        "ddgs package is not installed — run `pip install ddgs`",
    ])
    def test_permanent(self, text):
        assert wsr.classify_search_failure(text) == wsr.PERMANENT

    def test_interrupted(self):
        assert wsr.classify_search_failure("Interrupted") == wsr.INTERRUPTED

    def test_unknown_defaults_to_permanent(self):
        """An unrecognised error is likelier a deterministic bug than a blip.

        Falling back still covers the run, so the conservative default costs
        retry coverage but never costs wall clock.
        """
        assert wsr.classify_search_failure("something bizarre happened") == wsr.PERMANENT
        assert wsr.classify_search_failure("") == wsr.PERMANENT
        assert wsr.classify_search_failure(None) == wsr.PERMANENT

    def test_auth_code_beats_retryish_prose(self):
        """A 401 whose body says "try again later" must not be retried."""
        assert wsr.classify_search_failure(
            "Request failed with status code 401: please try again later"
        ) == wsr.PERMANENT

    def test_auth_code_beats_a_transient_code_in_the_same_body(self):
        """Pins the permanent-BEFORE-transient ordering of the code check.

        A gateway can echo an upstream 503 inside a body that is itself a 403
        for us. Retrying that is pure waste: our credentials will still be
        rejected in one second. Swapping the two membership tests in
        ``classify_search_failure`` must fail this test.
        """
        assert wsr.classify_search_failure(
            'Request failed with status code 403: {"detail":"upstream HTTP 503"}'
        ) == wsr.PERMANENT

    def test_requests_HTTPError_shape_is_transient(self):
        """The single commonest real 5xx shape. An anchored `\\bhttp\\b` pattern
        missed `HTTPError 503` and classified the module's own reason-for-
        existing as PERMANENT — zero retries, zero fallback, silently."""
        assert wsr.classify_search_failure(
            "exa search raised: HTTPError 503 Server Error for url https://api.exa.ai/search"
        ) == wsr.TRANSIENT

    def test_HTTPError_code_is_read_without_help_from_prose(self):
        """Isolates the status pattern itself.

        The fuller `HTTPError 503 Server Error` string also matches the
        "server error" phrase, so it passes even with a broken code pattern.
        This variant carries the code and NO transient prose, so only a
        pattern that reads the digits after `HTTPError` can classify it.
        """
        assert wsr.classify_search_failure(
            "exa search raised: HTTPError 503 for https://api.exa.ai/search"
        ) == wsr.TRANSIENT

    def test_HTTPError_auth_code_is_still_permanent(self):
        assert wsr.classify_search_failure(
            "exa search raised: HTTPError 401 for https://api.exa.ai/search"
        ) == wsr.PERMANENT

    def test_echoed_query_digits_cannot_veto_a_real_outage(self):
        """Trailing context must not outrank the error's own leading verdict.

        A permanent-wins-anywhere rule let an error echoing a user query
        ("what does HTTP 404 mean") turn a genuine capacity outage into a
        no-retry-no-fallback permanent failure.
        """
        assert wsr.classify_search_failure(
            'Exa search failed: {"error": "over capacity"} '
            "for query 'what does HTTP 404 mean'"
        ) == wsr.TRANSIENT

    @pytest.mark.parametrize("text", [
        "connection interrupted by peer",
        "exa search raised: Interrupted system call",   # EINTR
    ])
    def test_the_word_interrupted_does_not_forge_a_user_abort(self, text):
        """Only the providers' exact sentinel means "the user stopped us".

        Substring matching laundered transient network errors into an abort,
        which skips retry AND fallback — the worst possible misread.
        """
        assert wsr.classify_search_failure(text) == wsr.TRANSIENT

    def test_exact_sentinel_is_still_an_interrupt(self):
        assert wsr.classify_search_failure("Interrupted") == wsr.INTERRUPTED
        assert wsr.classify_search_failure("Interrupted.") == wsr.INTERRUPTED

    def test_query_text_is_not_read_as_a_status_code(self):
        """Narrow status patterns — a bare 3-digit run in prose is not a status."""
        assert wsr.classify_search_failure(
            "No results for 'boeing 503 fuel burn'"
        ) == wsr.PERMANENT


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------
class TestBackoff:
    def test_transient_failure_is_retried_until_it_succeeds(self):
        slept: List[float] = []
        provider = FakeProvider("exa", [fail(EXA_503), fail(EXA_503), ok(3)])

        outcome = wsr.search_with_resilience(
            provider, "q", 5,
            sleep_fn=lambda s: (slept.append(s), s)[1],
            backoff_fn=lambda attempt: float(attempt),
        )

        assert outcome.succeeded
        assert outcome.provider_used == "exa"
        assert outcome.fell_back is False
        assert len(provider.calls) == 3
        assert slept == [1.0, 2.0], "must wait between retries, not hammer"

    def test_permanent_failure_is_not_retried(self):
        slept: List[float] = []
        provider = FakeProvider("exa", [fail("status code 401: unauthorized")])

        outcome = wsr.search_with_resilience(
            provider, "q", 5, sleep_fn=lambda s: (slept.append(s), s)[1],
        )

        assert not outcome.succeeded
        assert len(provider.calls) == 1, "auth errors must fail fast"
        assert slept == []

    def test_attempt_cap_is_honoured(self):
        provider = FakeProvider("exa", [fail(EXA_503)])
        outcome = wsr.search_with_resilience(
            provider, "q", 5, max_attempts=3,
            sleep_fn=lambda s: s, backoff_fn=lambda a: 0.01,
        )
        assert len(provider.calls) == 3
        assert not outcome.succeeded

    def test_total_wait_budget_bounds_the_call(self):
        """The budget, not the attempt count, is what bounds a tool call.

        A cron job with profile/workdir runs on a single-thread pool, so an
        unbounded tool wait blocks every other agent.
        """
        slept: List[float] = []
        primary = FakeProvider("exa", [fail(EXA_503)])
        secondary = FakeProvider("ddgs", [fail(EXA_503)])

        outcome = wsr.search_with_resilience(
            primary, "q", 5,
            fallbacks=[secondary],
            max_attempts=10,
            total_wait_budget_s=5.0,
            sleep_fn=lambda s: (slept.append(s), s)[1],
            backoff_fn=lambda attempt: 100.0,
        )

        assert sum(slept) <= 5.0
        assert outcome.total_wait_s <= 5.0
        assert not outcome.succeeded

    def test_total_deadline_bounds_slow_provider_CALLS_not_just_sleeps(self):
        """The wait budget caps sleeping; the deadline caps everything.

        ddgs self-caps at 30s per call and a ddgs timeout classifies as
        TRANSIENT, so without an absolute deadline a chain could spend
        ~210s inside provider.search() alone while sleeping almost nothing —
        holding the single-thread cron pool the whole time.
        """
        clock = {"t": 0.0}

        def fake_monotonic():
            return clock["t"]

        class SlowProvider(FakeProvider):
            def search(self, query, limit=5):
                clock["t"] += 30.0  # each call burns 30s of wall clock
                return super().search(query, limit)

        primary = SlowProvider("exa", [fail(EXA_503)])
        secondary = SlowProvider("ddgs", [fail(EXA_503)])

        outcome = wsr.search_with_resilience(
            primary, "q", 5,
            fallbacks=[secondary],
            max_attempts=3,
            total_wait_budget_s=30.0,
            total_deadline_s=90.0,
            sleep_fn=lambda s: 0.0,   # sleeping is free; only CALLS burn time
            # Must be POSITIVE: a 0.0 backoff makes the retry loop break early
            # on its own, so the test would pass without the deadline doing
            # anything at all (caught by mutation testing).
            backoff_fn=lambda a: 1.0,
            monotonic=fake_monotonic,
        )

        total_calls = len(primary.calls) + len(secondary.calls)
        assert total_calls <= 3, (
            f"deadline did not bound provider call time: {total_calls} calls "
            f"= {total_calls * 30}s of wall clock"
        )
        assert not outcome.succeeded
        assert outcome.response["error_kind"] == "search_provider_unavailable"

    def test_sleep_is_clamped_to_the_remaining_deadline(self):
        """Never sleep past the deadline — the wait budget alone doesn't know
        how much wall clock the provider calls already consumed."""
        clock = {"t": 0.0}
        slept: List[float] = []

        class SlowProvider(FakeProvider):
            def search(self, query, limit=5):
                clock["t"] += 20.0  # each call eats 20s of the 25s deadline
                return super().search(query, limit)

        wsr.search_with_resilience(
            SlowProvider("exa", [fail(EXA_503)]), "q", 5,
            max_attempts=3,
            total_wait_budget_s=30.0,   # budget alone would allow a long sleep
            total_deadline_s=25.0,      # but only 5s of wall clock is left
            sleep_fn=lambda s: (slept.append(s), 0.0)[1],
            backoff_fn=lambda a: 30.0,  # backoff WANTS 30s
            monotonic=lambda: clock["t"],
        )

        assert slept, "expected at least one backoff sleep"
        assert max(slept) <= 5.0, (
            f"slept {max(slept)}s past a deadline with only 5s left"
        )

    def test_permanent_failure_short_circuits_before_the_deadline_matters(self):
        """A non-transient failure returns verbatim even if the call was slow —
        the deadline governs retrying, not the shape of a completed answer."""
        clock = {"t": 0.0}
        original = fail("EXA_API_KEY is not set")

        class SlowProvider(FakeProvider):
            def search(self, query, limit=5):
                clock["t"] += 100.0
                return super().search(query, limit)

        outcome = wsr.search_with_resilience(
            SlowProvider("exa", [original]), "q", 5,
            total_deadline_s=50.0,
            sleep_fn=lambda s: 0.0,
            monotonic=lambda: clock["t"],
        )
        assert outcome.response == original
        assert "error_kind" not in outcome.response

    def test_default_backoff_grows_and_is_jittered(self):
        """Uses agent.retry_utils.jittered_backoff rather than a fixed sleep."""
        provider = FakeProvider("exa", [fail(EXA_503)])
        slept: List[float] = []
        wsr.search_with_resilience(
            provider, "q", 5, max_attempts=3,
            sleep_fn=lambda s: (slept.append(s), 0.0)[1],
        )
        assert len(slept) == 2
        assert slept[0] >= wsr._BASE_DELAY_S
        assert slept[1] > slept[0], "delay must grow between attempts"
        assert all(s <= wsr._MAX_DELAY_S * 1.5 for s in slept)


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------
class TestDegenerateTuning:
    """The public helper is callable from outside web_tools; degenerate
    numbers must fail safe rather than silently disabling a bound."""

    def test_nan_deadline_does_not_silently_disable_the_deadline(self):
        provider = FakeProvider("exa", [fail(EXA_503)])
        outcome = wsr.search_with_resilience(
            provider, "q", 5,
            max_attempts=2,
            total_deadline_s=float("nan"),
            sleep_fn=lambda s: 0.0,
            backoff_fn=lambda a: 0.0,
        )
        # NaN > 0 is False, so a naive check yields deadline=None. Either way
        # the attempt cap must still bound the call.
        assert len(provider.calls) <= 2
        assert not outcome.succeeded

    def test_zero_or_negative_attempts_still_makes_one_attempt(self):
        provider = FakeProvider("exa", [ok(1)])
        outcome = wsr.search_with_resilience(
            provider, "q", 5, max_attempts=0, sleep_fn=lambda s: 0.0,
        )
        assert outcome.succeeded
        assert len(provider.calls) == 1


class TestFallback:
    def test_falls_back_when_primary_stays_down(self):
        primary = FakeProvider("exa", [fail(EXA_503)])
        secondary = FakeProvider("ddgs", [ok(2)])

        outcome = wsr.search_with_resilience(
            primary, "q", 5, fallbacks=[secondary],
            sleep_fn=lambda s: s, backoff_fn=lambda a: 0.0,
        )

        assert outcome.succeeded
        assert outcome.provider_used == "ddgs"
        assert outcome.fell_back is True
        assert secondary.calls == [("q", 5)]

    def test_permanent_primary_failure_does_NOT_fall_back(self):
        """A misconfiguration is not an outage.

        Failing over on a dead API key would mask the misconfiguration and
        replace a precise error ("EXA_API_KEY is not set") with plausible
        results from another backend. It would also mean any deterministic
        bug triggers ddgs's lazy pip-install and a live DuckDuckGo query.
        """
        slept: List[float] = []
        primary = FakeProvider("exa", [fail("EXA_API_KEY is not set")])
        secondary = FakeProvider("ddgs", [ok(1)])

        outcome = wsr.search_with_resilience(
            primary, "q", 5, fallbacks=[secondary],
            sleep_fn=lambda s: (slept.append(s), s)[1],
        )

        assert not outcome.succeeded
        assert len(primary.calls) == 1
        assert secondary.calls == [], "a config error must not reach the fallback"
        assert slept == []

    def test_non_transient_failure_is_returned_verbatim(self):
        """The provider's own error envelope must survive untouched."""
        original = fail("EXA_API_KEY is not set")
        outcome = wsr.search_with_resilience(
            FakeProvider("exa", [original]), "q", 5, sleep_fn=lambda s: s,
        )
        assert outcome.response == original
        assert "error_kind" not in outcome.response

    def test_empty_results_are_a_real_answer_and_never_trigger_fallback(self):
        """"Nobody published anything" must not be retried as if it were an
        outage — conflating the two is the failure mode this module prevents."""
        primary = FakeProvider("exa", [ok(0)])
        secondary = FakeProvider("ddgs", [ok(5)])

        outcome = wsr.search_with_resilience(
            primary, "q", 5, fallbacks=[secondary], sleep_fn=lambda s: s,
        )

        assert outcome.succeeded
        assert outcome.provider_used == "exa"
        assert outcome.fell_back is False
        assert secondary.calls == []

    def test_transient_exception_is_absorbed_and_failed_over(self):
        primary = FakeProvider("exa", [RuntimeError("boom 503 over capacity")])
        secondary = FakeProvider("ddgs", [ok(1)])

        outcome = wsr.search_with_resilience(
            primary, "q", 5, fallbacks=[secondary],
            sleep_fn=lambda s: s, backoff_fn=lambda a: 0.0,
        )
        assert outcome.succeeded and outcome.provider_used == "ddgs"

    def test_fallback_exception_does_not_erase_the_primary_outage(self):
        """Re-raising from a FALLBACK would replace a real outage report with
        the fallback's own exception and discard the trail, so neither the
        agent nor Langfuse would learn the primary was down."""
        primary = FakeProvider("exa", [fail(EXA_503)])
        secondary = FakeProvider("ddgs", [KeyError("results")])

        outcome = wsr.search_with_resilience(
            primary, "q", 5, fallbacks=[secondary],
            sleep_fn=lambda s: 0.0, backoff_fn=lambda a: 0.0,
        )
        assert outcome.response["error_kind"] == "search_provider_unavailable"
        tried = [t["provider"] for t in outcome.response["providers_tried"]]
        assert tried == ["exa", "ddgs"], tried
        assert "503" in outcome.response["providers_tried"][0]["detail"]

    def test_fallback_gets_exactly_one_shot(self):
        """A fallback is a last resort, not a second place to hammer. ddgs
        re-runs its lazy pip-install on every search() and leaks a worker
        thread per timeout, so retrying it multiplies both."""
        primary = FakeProvider("exa", [fail(EXA_503)])
        secondary = FakeProvider("ddgs", [fail(EXA_503)])

        wsr.search_with_resilience(
            primary, "q", 5, fallbacks=[secondary],
            max_attempts=3,
            sleep_fn=lambda s: 0.0, backoff_fn=lambda a: 0.1,
        )
        assert len(primary.calls) == 3, "primary keeps the full retry budget"
        assert len(secondary.calls) == 1, "fallback must get exactly one shot"

    def test_non_transient_exception_propagates_to_the_caller(self):
        """web_search_tool's outer handler owns the sanitised envelope
        ("Error searching web: boom"). Swallowing the exception here would
        leak a different, unsanitised error shape to the model — see
        tests/tools/test_web_tools_config.py::
        test_search_error_response_does_not_expose_diagnostics.
        """
        primary = FakeProvider("exa", [RuntimeError("boom")])
        secondary = FakeProvider("ddgs", [ok(1)])

        with pytest.raises(RuntimeError, match="boom"):
            wsr.search_with_resilience(
                primary, "q", 5, fallbacks=[secondary], sleep_fn=lambda s: s,
            )
        assert secondary.calls == []


class TestInterrupt:
    def test_interrupt_aborts_without_sleeping(self):
        slept: List[float] = []
        provider = FakeProvider("exa", [fail(EXA_503)])

        outcome = wsr.search_with_resilience(
            provider, "q", 5,
            is_interrupted=lambda: True,
            sleep_fn=lambda s: (slept.append(s), s)[1],
        )

        assert outcome.response == {"success": False, "error": "Interrupted"}
        assert provider.calls == []
        assert slept == []

    def test_provider_reported_interrupt_stops_the_chain(self):
        primary = FakeProvider("exa", [fail("Interrupted")])
        secondary = FakeProvider("ddgs", [ok(1)])

        outcome = wsr.search_with_resilience(
            primary, "q", 5, fallbacks=[secondary], sleep_fn=lambda s: s,
        )

        assert not outcome.succeeded
        assert secondary.calls == [], "an interrupt is not an outage"


# ---------------------------------------------------------------------------
# Outage is distinguishable from "no results"
# ---------------------------------------------------------------------------
class TestOutageIsDistinguishable:
    def test_exhausted_chain_returns_a_typed_unavailable_error(self):
        primary = FakeProvider("exa", [fail(EXA_503)])
        secondary = FakeProvider("ddgs", [fail("status code 502")])

        outcome = wsr.search_with_resilience(
            primary, "q", 5, fallbacks=[secondary],
            sleep_fn=lambda s: s, backoff_fn=lambda a: 0.0,
        )

        resp = outcome.response
        assert resp["success"] is False
        assert resp["error_kind"] == "search_provider_unavailable"
        assert [p["provider"] for p in resp["providers_tried"]] == ["exa", "ddgs"]
        assert "503" in resp["providers_tried"][0]["detail"]
        # The model must not read an outage as "no sources exist".
        assert "NOT an empty result set" in resp["error"]

    def test_empty_result_set_carries_no_outage_signal(self):
        outcome = wsr.search_with_resilience(
            FakeProvider("exa", [ok(0)]), "q", 5, sleep_fn=lambda s: s,
        )
        assert outcome.response["success"] is True
        assert "error_kind" not in outcome.response
        assert outcome.response["data"]["web"] == []


# ---------------------------------------------------------------------------
# Fallback chain resolution against the real registry
# ---------------------------------------------------------------------------
class TestResolveSearchFallbacks:
    """The fallback chain is a billing boundary (issue #174), not a preference."""

    @staticmethod
    def _registry(*provider_classes):
        from agent.web_search_registry import register_provider, _reset_for_tests
        _reset_for_tests()
        for cls in provider_classes:
            register_provider(cls())

    def test_tenant_on_ddgs_never_falls_back_to_exa_EVEN_WITH_THE_KEY_SET(
        self, monkeypatch
    ):
        """Billing guard, issue #174 — the version that actually holds in prod.

        Rented tenants run ``web.search_backend: ddgs`` and are denied
        EXA_API_KEY so their searches cannot be billed to BigLobster's Exa
        account. But that denial only strips the key from the tenant's ``.env``
        FILE, while ``exa.is_available()`` resolves via
        ``hermes_cli.config.get_env_value`` which reads ``os.environ`` FIRST —
        and ``cron/scheduler.py:_job_profile_context`` only adds and restores
        env keys, never deletes one the parent already had.

        So this test sets EXA_API_KEY **on purpose**. An earlier version of this
        test deleted it, which made the guard look green while the real tenant
        runtime resolved ``ddgs -> exa`` and billed BigLobster on every ddgs
        blip. If this test ever fails, the leak is back.
        """
        from plugins.web.exa.provider import ExaWebSearchProvider
        from plugins.web.ddgs.provider import DDGSWebSearchProvider

        self._registry(ExaWebSearchProvider, DDGSWebSearchProvider)
        monkeypatch.setenv("EXA_API_KEY", "bl-shared-key")
        monkeypatch.setattr(
            "hermes_cli.config.get_env_value",
            lambda name: "bl-shared-key" if name == "EXA_API_KEY" else None,
            raising=False,
        )
        try:
            names = [p.name for p in wsr.resolve_search_fallbacks("ddgs")]
            assert names == [], (
                f"tenant resolved paid fallback(s) {names} — this bills "
                "BigLobster (#174)"
            )
        finally:
            from agent.web_search_registry import _reset_for_tests
            _reset_for_tests()

    def test_no_paid_provider_is_ever_selected_automatically(self, monkeypatch):
        """Even with every paid key present, the automatic fallback is keyless."""
        from plugins.web.exa.provider import ExaWebSearchProvider
        from plugins.web.ddgs.provider import DDGSWebSearchProvider
        from plugins.web.firecrawl.provider import FirecrawlWebSearchProvider
        from plugins.web.parallel.provider import ParallelWebSearchProvider

        self._registry(
            ExaWebSearchProvider, DDGSWebSearchProvider,
            FirecrawlWebSearchProvider, ParallelWebSearchProvider,
        )
        for var in ("EXA_API_KEY", "FIRECRAWL_API_KEY",
                    "PARALLEL_API_KEY", "BRAVE_SEARCH_API_KEY"):
            monkeypatch.setenv(var, "k")
        monkeypatch.setattr(
            "hermes_cli.config.get_env_value", lambda name: "k", raising=False
        )
        try:
            names = [p.name for p in wsr.resolve_search_fallbacks("exa")]
            assert names == ["ddgs"], names
        finally:
            from agent.web_search_registry import _reset_for_tests
            _reset_for_tests()

    def test_availability_is_deliberately_not_consulted(self, monkeypatch):
        """ddgs reports unavailable until installed, but self-installs on first
        search — that is how a fresh tenant gets its first web search today."""
        from plugins.web.ddgs.provider import DDGSWebSearchProvider

        self._registry(DDGSWebSearchProvider)
        monkeypatch.setattr(
            DDGSWebSearchProvider, "is_available", lambda self: False
        )
        try:
            names = [p.name for p in wsr.resolve_search_fallbacks("exa")]
            assert names == ["ddgs"], (
                "an unavailable-but-self-installing ddgs was filtered out"
            )
        finally:
            from agent.web_search_registry import _reset_for_tests
            _reset_for_tests()

    def test_primary_is_never_its_own_fallback(self):
        from plugins.web.ddgs.provider import DDGSWebSearchProvider

        self._registry(DDGSWebSearchProvider)
        try:
            assert wsr.resolve_search_fallbacks("ddgs") == []
        finally:
            from agent.web_search_registry import _reset_for_tests
            _reset_for_tests()

    def test_empty_registry_yields_no_fallback(self):
        from agent.web_search_registry import _reset_for_tests
        _reset_for_tests()
        assert wsr.resolve_search_fallbacks("exa") == []


# ---------------------------------------------------------------------------
# End-to-end through web_search_tool
# ---------------------------------------------------------------------------
class TestWebSearchToolIntegration:
    def test_transient_outage_is_ridden_out_inside_one_tool_call(self, monkeypatch):
        """The whole point: one tool call, one result, zero guardrail budget."""
        from tools import web_tools

        provider = FakeProvider("exa", [fail(EXA_503), ok(2)])
        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
        monkeypatch.setattr(web_tools, "_get_search_backend", lambda: "exa")
        monkeypatch.setattr(
            "agent.web_search_registry.get_provider", lambda name: provider
        )
        monkeypatch.setattr(wsr, "resolve_search_fallbacks", lambda name: [])
        monkeypatch.setattr(wsr, "interruptible_sleep", lambda s, **kw: 0.0)

        result = json.loads(web_tools.web_search_tool("hermes", limit=5))

        assert result["success"] is True
        assert len(provider.calls) == 2, "the retry happened inside ONE tool call"
        # A non-degraded success keeps the documented response shape exactly
        # (agent/web_search_provider.py) — no annotation keys added.
        assert set(result) == {"success", "data"}, result

    def test_fallback_success_is_labelled_degraded(self, monkeypatch):
        from tools import web_tools

        primary = FakeProvider("exa", [fail(EXA_503)])
        secondary = FakeProvider("ddgs", [ok(3)])
        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
        monkeypatch.setattr(web_tools, "_get_search_backend", lambda: "exa")
        monkeypatch.setattr(
            "agent.web_search_registry.get_provider", lambda name: primary
        )
        monkeypatch.setattr(wsr, "resolve_search_fallbacks", lambda name: [secondary])
        monkeypatch.setattr(wsr, "interruptible_sleep", lambda s, **kw: 0.0)

        raw = web_tools.web_search_tool("hermes", limit=5)
        result = json.loads(raw)

        assert result["success"] is True
        assert result["search_provider"] == "ddgs"
        assert result["degraded"]["primary"] == "exa"
        assert result["degraded"]["primary_attempts"] == wsr.DEFAULT_MAX_ATTEMPTS

    def test_degraded_success_is_not_classified_as_a_tool_failure(self, monkeypatch):
        """A successful fallback must not count against same_tool_failure_halt.

        agent.tool_guardrails.classify_tool_failure flags any result whose
        first 500 chars contain '"error"' or '"failed"', so the degraded
        metadata keys must avoid those literals.
        """
        from tools import web_tools
        from agent.tool_guardrails import classify_tool_failure

        primary = FakeProvider("exa", [fail(EXA_503)])
        secondary = FakeProvider("ddgs", [ok(0)])  # empty AND degraded: worst case
        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
        monkeypatch.setattr(web_tools, "_get_search_backend", lambda: "exa")
        monkeypatch.setattr(
            "agent.web_search_registry.get_provider", lambda name: primary
        )
        monkeypatch.setattr(wsr, "resolve_search_fallbacks", lambda name: [secondary])
        monkeypatch.setattr(wsr, "interruptible_sleep", lambda s, **kw: 0.0)

        raw = web_tools.web_search_tool("hermes", limit=5)
        failed, _tag = classify_tool_failure("web_search", raw)
        # agent/display._detect_tool_failure is what agent/tool_executor.py
        # ACTUALLY calls per tool result; classify_tool_failure is only the
        # standalone fallback. Pin the production one too — they must agree.
        from agent.display import _detect_tool_failure
        prod_failed, _prod_tag = _detect_tool_failure("web_search", raw)

        assert json.loads(raw)["success"] is True
        assert failed is False, (
            "degraded-but-successful search was mislabelled a failure; it would "
            "burn same_tool_failure_halt budget"
        )
        assert prod_failed is False, (
            "PRODUCTION classifier marked a recovered search as failed"
        )

    def test_total_outage_surfaces_the_typed_error(self, monkeypatch):
        from tools import web_tools
        from agent.tool_guardrails import classify_tool_failure

        primary = FakeProvider("exa", [fail(EXA_503)])
        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
        monkeypatch.setattr(web_tools, "_get_search_backend", lambda: "exa")
        monkeypatch.setattr(
            "agent.web_search_registry.get_provider", lambda name: primary
        )
        monkeypatch.setattr(wsr, "resolve_search_fallbacks", lambda name: [])
        monkeypatch.setattr(wsr, "interruptible_sleep", lambda s, **kw: 0.0)

        raw = web_tools.web_search_tool("hermes", limit=5)
        result = json.loads(raw)

        assert result["success"] is False
        assert result["error_kind"] == "search_provider_unavailable"
        # A real outage SHOULD still count against the guardrail — both classifiers.
        from agent.display import _detect_tool_failure
        assert classify_tool_failure("web_search", raw)[0] is True
        assert _detect_tool_failure("web_search", raw)[0] is True
