"""Fork's own tests for agent.auxiliary_client, kept out of upstream's file so upstream merges do not conflict."""

from agent.auxiliary_client import _is_upstream_server_error


class TestIsUpstreamServerError:
    """_is_upstream_server_error detects 5xx / generic upstream errors warranting fallback.

    Regression for the 2026-06-18 FinView "Content Gap Hunter" incident: a
    saturated free model (owl-alpha) surfaced as OpenRouter's generic
    ``RuntimeError("Provider returned error")``. Because that error matched
    none of the payment/connection/rate-limit detectors, the configured paid
    fallback never engaged and the cron hard-failed. It must now be
    fallback-eligible.
    """

    def test_openrouter_provider_returned_error_no_status(self):
        """The exact incident signal: bubbled-up generic upstream error."""
        exc = RuntimeError("Provider returned error")
        assert _is_upstream_server_error(exc) is True

    def test_bad_gateway(self):
        exc = Exception("Bad Gateway")
        exc.status_code = 502
        assert _is_upstream_server_error(exc) is True

    def test_overloaded(self):
        exc = Exception("Overloaded")
        exc.status_code = 529
        assert _is_upstream_server_error(exc) is True

    def test_message_only_overloaded_no_status(self):
        exc = Exception("upstream error: model is overloaded")
        assert _is_upstream_server_error(exc) is True

    def test_no_instances_available(self):
        exc = Exception("No instances available for the requested model")
        assert _is_upstream_server_error(exc) is True

    def test_generic_500_is_NOT_upstream_error(self):
        """Boundary: a bare 500 from the endpoint stays on the same-provider
        retry path (preserves test_non_payment_error_not_caught). Only proxied
        *upstream* failures are fallback-eligible."""
        exc = Exception("Internal Server Error")
        exc.status_code = 500
        assert _is_upstream_server_error(exc) is False

    def test_validation_error_with_upstream_wrapper_is_not_caught(self):
        """A deterministic bad-request wrapped in an upstream error must NOT
        fall back — re-routing won't fix a malformed request."""
        exc = Exception("Provider returned error: unsupported parameter reasoning_effort")
        exc.status_code = 502
        assert _is_upstream_server_error(exc) is False

    def test_429_rate_limit_is_not_upstream_server_error(self):
        """429 is handled by _is_rate_limit_error, not here."""
        exc = Exception("Rate limit exceeded")
        exc.status_code = 429
        assert _is_upstream_server_error(exc) is False

    def test_402_payment_is_not_upstream_server_error(self):
        exc = Exception("Payment Required")
        exc.status_code = 402
        assert _is_upstream_server_error(exc) is False

    def test_unrelated_failure_is_not_upstream_server_error(self):
        exc = Exception("some unrelated failure")
        assert _is_upstream_server_error(exc) is False
