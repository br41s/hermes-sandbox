"""fork (#51): proxied upstream-provider failures that warrant provider fallback.

Lived in ``agent/auxiliary_client.py`` until upstream v0.23.x moved the auxiliary
ladder's error classification into ``agent/auxiliary_error_policy.py``, which reads
``agent.error_classifier``'s verdict. That classifier files OpenRouter's "Provider
returned error" and a bare "Bad Gateway" as ``unknown`` (retryable, no fallback), so
without this rung those requests re-hit the dead upstream instead of the fallback
chain. ``auxiliary_error_policy.fallback_label`` calls :func:`is_upstream_server_error`.
"""

from __future__ import annotations

# fork (#51): proxied *upstream*-provider failures that warrant provider fallback.
# OpenRouter (and similar gateways) wrap a failure of the upstream model they
# proxy to as a generic error — most often the literal "Provider returned
# error", a 502/Bad Gateway, or an explicit "overloaded".  These mean the
# upstream model behind the proxy cannot serve the request, so a same-provider
# retry just re-hits the dead upstream; the only recovery is to escalate to the
# configured fallback chain.
#
# Deliberately NARROW: this matches OpenRouter-style upstream wrappers, NOT a
# bare 500 "Internal Server Error" from the endpoint itself.  Generic 5xx are
# intentionally left on the same-provider retry path (see the existing decision
# in test_non_payment_error_not_caught) — falling back on every transient 500
# would needlessly churn models.
#
# Incident 2026-06-18: the FinView "Content Gap Hunter" cron hard-failed with
# RuntimeError("Provider returned error") (owl-alpha free-model saturation)
# because this error class was not fallback-eligible here.
_UPSTREAM_SERVER_ERROR_PATTERNS = (
    "provider returned error",
    "bad gateway",
    "upstream error",
    "overloaded",
    "no instances available",
)
# Deterministic request-validation rejections that some OpenAI-compatible
# gateways surface alongside an upstream-error wrapper.  Re-routing won't fix a
# malformed request, so they must NOT be treated as a transient capacity error.
_REQUEST_VALIDATION_5XX_MARKERS = (
    "unsupported parameter",
    "unsupported_parameter",
    "unknown parameter",
    "unknown_parameter",
    "invalid_request_error",
    "unrecognized request argument",
)


def is_upstream_server_error(exc: Exception) -> bool:
    """Detect proxied upstream-provider failures that warrant provider fallback.

    A saturated or down *upstream* model behind a proxy (OpenRouter's "Provider
    returned error", a Bad Gateway, an explicit "overloaded") cannot serve the
    request regardless of intent — a capacity problem — so we escalate to the
    configured fallback chain rather than re-raise.

    Deliberately narrow (text-only): it does NOT match a bare 500 from the
    endpoint itself (handled by same-provider retry), nor deterministic
    request-validation rejections (which another model attempt can't fix).
    """
    err_lower = str(exc).lower()
    if any(m in err_lower for m in _REQUEST_VALIDATION_5XX_MARKERS):
        return False
    return any(p in err_lower for p in _UPSTREAM_SERVER_ERROR_PATTERNS)
