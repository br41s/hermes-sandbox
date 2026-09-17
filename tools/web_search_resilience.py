"""Transient-outage resilience for ``web_search``.

Why this module exists
----------------------
``web_search`` used to make exactly one ``provider.search()`` call and return
whatever came back. When Exa answered ``503 "Exa is temporarily over capacity.
Please retry with exponential backoff"``, the tool surfaced that verbatim and
the *agent loop* did the retrying — five separate tool calls in ~2 seconds,
with no wait between them. Each one incremented the per-turn failure counter in
:mod:`agent.tool_guardrails`, so ``same_tool_failure_halt`` (8 failures) fired
and the whole cron run was lost to a blip a few seconds of waiting would have
ridden out (session ``cron_4a0ebe8779ca_20260916_232408``, 2026-09-16).

The fix has to live *inside* one tool call. A retry issued between tool calls
is indistinguishable from a retry loop, so the guardrail is right to count it;
only an in-call retry converts N failures into one success at zero guardrail
cost. The guardrail is not weakened here — it still halts a genuinely stuck
tool after 8 real failures.

Two layers, in order:

1. **Backoff** — retry the configured provider on *transient* failures with
   jittered exponential delay (:func:`agent.retry_utils.jittered_backoff`),
   bounded by a shared wall-clock budget so one tool call cannot stall the
   single-thread cron pool. Auth/validation failures are never retried: they
   cannot recover and retrying only burns wall clock.

2. **Fallback** — if the provider is still failing, try the next available
   registered search provider. Backoff alone still loses the run when the
   outage outlives the budget.

Directionality matters (issue #174)
-----------------------------------
Rented tenants run ``web.search_backend: ddgs`` and are deliberately NOT given
``EXA_API_KEY`` — tenant searches must never bill BigLobster's Exa account.
The fallback chain is therefore built from ``is_available()`` only, which on a
tenant reports False for Exa, so ``ddgs -> exa`` can never happen. ``ddgs``
itself is the documented free last resort in the other direction: it needs no
API key and lazy-installs at its own first search, exactly as it already does
on a freshly provisioned tenant.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


# ── Tuning ──────────────────────────────────────────────────────────────────
# Per-provider attempt cap. 3 rides out a typical capacity blip without
# turning one tool call into a retry storm.
DEFAULT_MAX_ATTEMPTS = 3

# Shared wall-clock ceiling for ALL sleeping in a single web_search call,
# across every provider in the chain. The tool executor abandons a whole
# concurrent batch at HERMES_CONCURRENT_TOOL_TIMEOUT_S (default 420s,
# agent/tool_executor.py), and cron jobs with profile/workdir run on a
# single-thread pool where one slow tool blocks every other agent — so this
# stays an order of magnitude below the batch deadline.
DEFAULT_TOTAL_WAIT_BUDGET_S = 30.0

_BASE_DELAY_S = 1.0
_MAX_DELAY_S = 8.0

# Sleep is sliced so an interrupt is observed promptly rather than after the
# full delay. web_search is a sync tool and runs on a worker thread
# (DaemonThreadPoolExecutor), so blocking here never stalls the event loop.
_SLEEP_SLICE_S = 0.25


# ── Failure classification ──────────────────────────────────────────────────
TRANSIENT = "transient"
PERMANENT = "permanent"
INTERRUPTED = "interrupted"

# Retry these: the upstream is up but momentarily refusing work.
_TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 507, 520, 521, 522, 523, 524, 529})

# Never retry these: the request itself is wrong, or we are not authorised.
# Retrying burns the wall-clock budget and, before this module existed, the
# guardrail budget too.
_PERMANENT_STATUS = frozenset({400, 401, 402, 403, 404, 405, 409, 410, 413, 422, 451})

# Codes are quoted in wildly different shapes across SDKs ("status code 503",
# "HTTP 429", "status=502", "Server error (504)"), so match narrowly rather
# than grabbing any 3-digit run — a query or a result count would otherwise
# be read as a status.
_STATUS_PATTERNS = (
    re.compile(r"status[\s_]*code[\s:=]*\(?\s*(\d{3})\b", re.I),
    re.compile(r"\bhttp[\s/]?(?:\d\.\d\s+)?(\d{3})\b", re.I),
    re.compile(r"\bstatus[\s:=]+(\d{3})\b", re.I),
    re.compile(r"\((\d{3})\)"),
)

_TRANSIENT_PHRASES = (
    "over capacity",
    "overloaded",
    "temporarily unavailable",
    "service unavailable",
    "temporarily over",
    "try again",
    "retry with exponential backoff",
    "rate limit",
    "ratelimit",
    "too many requests",
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "connection refused",
    "connection error",
    "server disconnected",
    "remote end closed connection",
    "bad gateway",
    "gateway timeout",
    "temporary failure in name resolution",
    "eof occurred",
)

# Checked BEFORE the transient phrases: an auth failure whose body happens to
# say "try again later" must still be classified permanent.
_PERMANENT_PHRASES = (
    "api key",
    "api_key",
    "apikey",
    "unauthorized",
    "forbidden",
    "invalid key",
    "invalid token",
    "authentication",
    "not installed",
    "no web search provider",
    "does not support search",
    "insufficient credit",
    "insufficient_quota",
    "quota exceeded",
    "payment required",
    "is not set",
)


def classify_search_failure(error_text: Any) -> str:
    """Classify a provider failure as transient, permanent, or interrupted.

    Providers flatten every failure into ``{"success": False, "error": str}``
    (see :class:`agent.web_search_provider.WebSearchProvider`), so this reads
    text rather than exception types. Anything unrecognised is treated as
    :data:`PERMANENT` — an unknown error is more likely a deterministic bug
    than a blip, and the caller still falls back to another provider, so the
    conservative choice costs coverage but never costs the run.
    """
    text = ("" if error_text is None else str(error_text)).strip().lower()
    if not text:
        return PERMANENT
    if text == "interrupted" or "interrupted" in text.split():
        return INTERRUPTED

    codes = set()
    for pattern in _STATUS_PATTERNS:
        for match in pattern.findall(text):
            try:
                codes.add(int(match))
            except (TypeError, ValueError):
                continue

    # Permanent first: an unambiguous auth/validation code must win even when
    # the body also carries retry-ish prose.
    if codes & _PERMANENT_STATUS:
        return PERMANENT
    if codes & _TRANSIENT_STATUS:
        return TRANSIENT

    if any(phrase in text for phrase in _PERMANENT_PHRASES):
        return PERMANENT
    if any(phrase in text for phrase in _TRANSIENT_PHRASES):
        return TRANSIENT
    return PERMANENT


# ── Interruptible sleep ─────────────────────────────────────────────────────
def _default_is_interrupted() -> bool:
    try:
        from tools.interrupt import is_interrupted

        return bool(is_interrupted())
    except Exception:  # noqa: BLE001 — never let the interrupt probe break a retry
        return False


def interruptible_sleep(
    seconds: float,
    *,
    is_interrupted: Optional[Callable[[], bool]] = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> float:
    """Sleep up to *seconds*, returning early (and reporting actual elapsed
    time) if the agent is interrupted. Returns seconds actually slept."""
    if seconds <= 0:
        return 0.0
    probe = is_interrupted or _default_is_interrupted
    start = monotonic()
    deadline = start + seconds
    while True:
        now = monotonic()
        remaining = deadline - now
        if remaining <= 0:
            break
        if probe():
            break
        sleep(min(_SLEEP_SLICE_S, remaining))
    return max(0.0, monotonic() - start)


# ── Outcome types ───────────────────────────────────────────────────────────
@dataclass
class ProviderAttempt:
    """What one provider in the chain did."""

    provider: str
    attempts: int = 0
    kind: Optional[str] = None
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        # Deliberately no key literally named "error"/"failed": the guardrail's
        # fallback classifier (agent/tool_guardrails.classify_tool_failure)
        # flags any result containing '"error"' in its first 500 chars, which
        # would mark a SUCCESSFUL degraded search as a failure.
        return {
            "provider": self.provider,
            "attempts": self.attempts,
            "classified": self.kind or "",
            "detail": self.detail[:400],
        }


@dataclass
class SearchOutcome:
    response: Dict[str, Any]
    provider_used: Optional[str] = None
    fell_back: bool = False
    total_wait_s: float = 0.0
    trail: List[ProviderAttempt] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return bool(self.response.get("success"))


def _failure_text(response: Any) -> str:
    if isinstance(response, dict):
        return str(response.get("error") or "").strip() or "provider returned success=False with no error text"
    return f"provider returned a non-dict response: {type(response).__name__}"


def _provider_name(provider: Any) -> str:
    try:
        return str(provider.name)
    except Exception:  # noqa: BLE001
        return type(provider).__name__


# ── Driver ──────────────────────────────────────────────────────────────────
def search_with_resilience(
    primary: Any,
    query: str,
    limit: int,
    *,
    fallbacks: Sequence[Any] = (),
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    total_wait_budget_s: float = DEFAULT_TOTAL_WAIT_BUDGET_S,
    is_interrupted: Optional[Callable[[], bool]] = None,
    sleep_fn: Optional[Callable[[float], float]] = None,
    backoff_fn: Optional[Callable[[int], float]] = None,
) -> SearchOutcome:
    """Run ``primary.search(query, limit)`` with backoff, then fall back.

    **Only transient failures are retried or failed over.** Everything else is
    passed through untouched — a raised exception is re-raised so the caller's
    existing error envelope still owns it, and a failure dict is returned
    verbatim. That restraint is deliberate and load-bearing:

    - A missing API key, a malformed query or an unrecognised error is not an
      outage. Quietly answering it from a different backend would mask a
      misconfiguration the operator needs to see, and would replace a precise
      error message ("FIRECRAWL_API_KEY is not set") with plausible results
      from somewhere else.
    - ``ddgs`` lazy-installs itself on first search. Failing over on *any*
      error means a deterministic bug triggers a pip install and a live
      DuckDuckGo query. That is not hypothetical: an earlier draft of this
      module did exactly that during a unit-test run.

    Every provider in ``[primary, *fallbacks]`` gets up to ``max_attempts``
    tries, and ALL of them share the single ``total_wait_budget_s`` sleep
    budget — the budget, not the attempt count, is what bounds the call.
    An empty result set is a successful answer and never triggers a fallback;
    conflating "nobody published anything" with "the provider is down" is the
    failure mode this whole module exists to prevent.

    ``sleep_fn`` / ``backoff_fn`` / ``is_interrupted`` are injection points
    for tests; production uses the real clock.
    """
    probe = is_interrupted or _default_is_interrupted

    def _sleep(seconds: float) -> float:
        if sleep_fn is not None:
            return float(sleep_fn(seconds))
        return interruptible_sleep(seconds, is_interrupted=probe)

    def _backoff(attempt: int) -> float:
        if backoff_fn is not None:
            return float(backoff_fn(attempt))
        from agent.retry_utils import jittered_backoff

        return jittered_backoff(attempt, base_delay=_BASE_DELAY_S, max_delay=_MAX_DELAY_S)

    def _interrupted(trail: List[ProviderAttempt], waited: float) -> SearchOutcome:
        return SearchOutcome(
            response={"success": False, "error": "Interrupted"},
            trail=trail,
            total_wait_s=waited,
        )

    chain = [p for p in [primary, *fallbacks] if p is not None]
    trail: List[ProviderAttempt] = []
    budget_left = max(0.0, float(total_wait_budget_s))
    total_waited = 0.0
    attempt_cap = max(1, int(max_attempts))
    saw_transient = False
    last_response: Optional[Dict[str, Any]] = None

    for index, provider in enumerate(chain):
        name = _provider_name(provider)
        record = ProviderAttempt(provider=name)
        trail.append(record)
        stop_chain = False

        while record.attempts < attempt_cap:
            if probe():
                record.kind = INTERRUPTED
                record.detail = "interrupted before attempt"
                return _interrupted(trail, total_waited)

            record.attempts += 1
            raised: Optional[BaseException] = None
            try:
                response = provider.search(query, limit)
            except Exception as exc:  # noqa: BLE001 — classified below, re-raised if not transient
                raised = exc
                response = {"success": False, "error": f"{name} search raised: {exc}"}

            if raised is None and isinstance(response, dict) and response.get("success"):
                if index > 0:
                    logger.warning(
                        "web_search fell back to %s after %s failed "
                        "(%d attempt(s), %.1fs waited)",
                        name, _provider_name(chain[0]), trail[0].attempts, total_waited,
                    )
                return SearchOutcome(
                    response=response,
                    provider_used=name,
                    fell_back=index > 0,
                    total_wait_s=total_waited,
                    trail=trail,
                )

            detail = _failure_text(response)
            kind = classify_search_failure(detail)
            record.kind = kind
            record.detail = detail
            # Normalise a non-dict response (a misbehaving provider) so it is
            # still handed back verbatim-shaped rather than being reported as
            # an outage it is not.
            last_response = response if isinstance(response, dict) else {
                "success": False, "error": detail,
            }

            if kind == INTERRUPTED:
                return _interrupted(trail, total_waited)

            if kind != TRANSIENT:
                # Not an outage. Hand the failure straight back in its
                # original shape — re-raising keeps the caller's existing
                # sanitised envelope ("Error searching web: ...") intact.
                logger.info(
                    "web_search: %s failed (not transient, no retry or fallback): %s",
                    name, detail[:200],
                )
                if raised is not None:
                    raise raised
                stop_chain = True
                break

            saw_transient = True
            if record.attempts >= attempt_cap:
                break

            delay = min(_backoff(record.attempts), budget_left)
            if delay <= 0:
                logger.info("web_search: wait budget exhausted while retrying %s", name)
                break
            logger.info(
                "web_search: %s transient failure (attempt %d/%d), backing off %.1fs: %s",
                name, record.attempts, attempt_cap, delay, detail[:200],
            )
            slept = _sleep(delay)
            budget_left = max(0.0, budget_left - slept)
            total_waited += slept
            if probe():
                return _interrupted(trail, total_waited)

        if stop_chain:
            break

    if not saw_transient and last_response is not None:
        # Nothing transient ever happened, so nothing was retried or failed
        # over — preserve the provider's response byte-for-byte.
        return SearchOutcome(
            response=last_response, total_wait_s=total_waited, trail=trail
        )

    return SearchOutcome(
        response=build_unavailable_response(trail),
        total_wait_s=total_waited,
        trail=trail,
    )


def build_unavailable_response(trail: Sequence[ProviderAttempt]) -> Dict[str, Any]:
    """Build the typed "search is down" payload.

    The wording is aimed squarely at the model reading it. A run that cannot
    search must not quietly conclude there were no sources — on 2026-09-16 the
    agent got that right by luck and honesty; this makes it structural.
    ``error_kind`` gives programmatic callers the same signal without parsing
    prose.
    """
    if not trail:
        summary = "no search provider was reachable"
    else:
        summary = "; ".join(
            f"{r.provider} failed after {r.attempts} attempt(s) ({r.detail[:160]})"
            for r in trail
        )
    return {
        "success": False,
        "error": (
            "Web search is UNAVAILABLE — every configured provider failed. "
            f"{summary}. This is a provider outage, NOT an empty result set: "
            "do not report 'no sources found' or draw any conclusion from it. "
            "Retrying immediately will not help (backoff and a fallback provider "
            "were already tried). Say the search backend is down and stop."
        ),
        "error_kind": "search_provider_unavailable",
        "providers_tried": [r.as_dict() for r in trail],
    }


# ── Fallback chain resolution ───────────────────────────────────────────────
# Two fallbacks is the cap. The chain is bounded by the shared wait budget
# anyway; the cap keeps the worst case legible and stops a long provider list
# turning one tool call into a tour of every backend.
_MAX_FALLBACKS = 2

# The free, keyless last resort. Its ``is_available()`` only probes whether
# the ``ddgs`` package is importable (it must stay I/O-free), but its
# ``search()`` lazy-installs the package on first use — which is precisely how
# a freshly provisioned rented tenant already gets its very first web search
# (docker/cont-init.d/03-biglobster-config forces web.search_backend: ddgs).
# So an "unavailable" ddgs is still a working fallback, and it is appended
# explicitly rather than being filtered out by the availability walk.
_KEYLESS_LAST_RESORT = "ddgs"


def resolve_search_fallbacks(primary_name: str, *, max_fallbacks: int = _MAX_FALLBACKS) -> List[Any]:
    """Return search providers to try after *primary_name* fails.

    Ordered by the registry's own ``_LEGACY_PREFERENCE`` and filtered by
    ``is_available()``, so a provider the operator has no credentials for is
    never attempted.

    That availability filter is load-bearing for billing, not just tidiness:
    rented tenants are deliberately denied ``EXA_API_KEY``
    (docker/cont-init.d/03-biglobster-config, issue #174) so that their
    searches cannot be billed to BigLobster's Exa account. Exa therefore
    reports unavailable on a tenant and can never be selected as a fallback
    there. Do not "helpfully" relax this to walk unavailable providers — the
    explicit ``ddgs`` append below is the one deliberate exception, and it is
    safe precisely because ddgs needs no key and bills nobody.
    """
    try:
        from agent.web_search_registry import _LEGACY_PREFERENCE, get_provider, list_providers
    except Exception as exc:  # noqa: BLE001 — no registry, no fallback
        logger.debug("fallback resolution unavailable: %s", exc)
        return []

    primary = (primary_name or "").strip()
    chosen: List[Any] = []
    seen = {primary}

    def _consider(provider: Any, *, require_available: bool) -> None:
        if provider is None or len(chosen) >= max_fallbacks:
            return
        name = _provider_name(provider)
        if name in seen:
            return
        try:
            if not provider.supports_search():
                return
            if require_available and not provider.is_available():
                return
        except Exception as exc:  # noqa: BLE001 — a broken provider is skipped
            logger.debug("provider %s probe raised: %s", name, exc)
            return
        seen.add(name)
        chosen.append(provider)

    try:
        registered = {_provider_name(p): p for p in list_providers()}
    except Exception as exc:  # noqa: BLE001
        logger.debug("provider listing failed: %s", exc)
        registered = {}

    for name in _LEGACY_PREFERENCE:
        _consider(registered.get(name), require_available=True)

    # Anything registered but outside the legacy preference order (custom
    # plugin providers), still availability-gated.
    for name, provider in sorted(registered.items()):
        _consider(provider, require_available=True)

    if not chosen:
        try:
            _consider(get_provider(_KEYLESS_LAST_RESORT), require_available=False)
        except Exception as exc:  # noqa: BLE001
            logger.debug("keyless last-resort lookup failed: %s", exc)

    return chosen
