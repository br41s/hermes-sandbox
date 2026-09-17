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
import math
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

# Wall-clock ceiling on RETRY SCHEDULING. Checked before each attempt, so it
# bounds how much total time this function is willing to keep spending — it
# CANNOT interrupt a call already in flight. Real worst case is therefore
# ``DEFAULT_TOTAL_DEADLINE_S + (one provider's own timeout)``; providers own
# their per-call timeouts (ddgs self-caps at 30s, exa's SDK does not, which is
# a pre-existing gap this module does not close).
#
# DEFAULT_TOTAL_WAIT_BUDGET_S alone is not enough because it bounds only
# sleeping, while a slow-but-failing chain burns wall clock inside
# provider.search() itself. With one-shot fallbacks the realistic worst case is
# ~30s of sleeping + one primary call + one fallback call, well inside the
# tool executor's 420s batch deadline and far short of holding the
# single-thread cron pool for minutes.
DEFAULT_TOTAL_DEADLINE_S = 90.0

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
# ``\bhttp\w*`` (not ``\bhttp\b``) is deliberate: the single commonest real
# shape is requests/httpx's ``HTTPError 503 Server Error``, where the code
# follows the word ``HTTPError``. An earlier anchored version missed it and
# classified the module's own reason-for-existing as PERMANENT.
#
# There is NO unanchored ``\((\d{3})\)`` pattern: because a permanent code
# wins unconditionally below, any bare 3-digit run in the text could veto a
# retry — an error echoing a user query like "what does HTTP 404 mean" would
# turn a real 503 outage into a no-retry. Parenthesised codes are reached via
# the phrase table instead ("server error", "bad gateway", ...).
_STATUS_PATTERNS = (
    re.compile(r"status[\s_]*code[\s:=]*\(?\s*(\d{3})\b", re.I),
    re.compile(r"\bhttp\w*[\s:/-]{0,3}(\d{3})\b", re.I),
    re.compile(r"\bstatus[\s:=]+\(?(\d{3})\b", re.I),
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
    "server error",
    "internal server error",
    "upstream connect error",
    "temporarily",
    "capacity",
    "backoff",
    "temporary failure in name resolution",
    "eof occurred",
    "connection interrupted",
    "interrupted system call",
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
    text rather than exception types.

    **Earliest evidence wins.** An error message leads with its own nature and
    trails with context — a URL, a JSON body, an echoed query. Scanning for
    "any permanent signal anywhere" therefore lets trailing context veto the
    real verdict: a genuine ``over capacity`` outage whose message happens to
    echo a user query containing "HTTP 404" would be read as permanent, and a
    permanent verdict skips BOTH retry and fallback, losing the run. Taking
    the leftmost signal keeps an auth 401 authoritative over a trailing
    ``upstream 503`` while refusing to let incidental digits win.

    Ties break to PERMANENT: not retrying a genuine auth failure is cheaper
    than hammering one. Unrecognised text is PERMANENT for the same reason —
    an unknown error is likelier a deterministic bug than a blip, and burning
    the wall-clock budget on it helps nobody.
    """
    text = ("" if error_text is None else str(error_text)).strip().lower()
    if not text:
        return PERMANENT

    # EXACT match only. Providers return the literal sentinel
    # ``{"success": False, "error": "Interrupted"}`` on a user abort. Substring
    # or token matching also catches "connection interrupted by peer" and
    # EINTR's "Interrupted system call" — both TRANSIENT network errors that
    # would then be laundered into an abort, skipping retry AND fallback.
    if text.rstrip(".!") == "interrupted":
        return INTERRUPTED

    best_pos: Optional[int] = None
    best_kind = PERMANENT

    def _offer(pos: int, kind: str) -> None:
        nonlocal best_pos, best_kind
        if pos < 0:
            return
        if best_pos is None or pos < best_pos:
            best_pos, best_kind = pos, kind
        elif pos == best_pos and kind == PERMANENT:
            best_kind = PERMANENT  # tie -> don't retry

    for pattern in _STATUS_PATTERNS:
        for match in pattern.finditer(text):
            try:
                code = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if code in _PERMANENT_STATUS:
                _offer(match.start(), PERMANENT)
            elif code in _TRANSIENT_STATUS:
                _offer(match.start(), TRANSIENT)

    for phrase in _PERMANENT_PHRASES:
        _offer(text.find(phrase), PERMANENT)
    for phrase in _TRANSIENT_PHRASES:
        _offer(text.find(phrase), TRANSIENT)

    return best_kind if best_pos is not None else PERMANENT


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
    total_deadline_s: float = DEFAULT_TOTAL_DEADLINE_S,
    is_interrupted: Optional[Callable[[], bool]] = None,
    sleep_fn: Optional[Callable[[float], float]] = None,
    backoff_fn: Optional[Callable[[int], float]] = None,
    monotonic: Callable[[], float] = time.monotonic,
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
    budget. ``total_wait_budget_s`` caps how long we SLEEP; ``total_deadline_s``
    caps how long we keep SCHEDULING new attempts. Neither can interrupt a call
    already in flight — providers own their per-call timeouts.

    The primary gets ``max_attempts`` tries; each fallback gets exactly one.
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
    deadline_hit = False
    last_response: Optional[Dict[str, Any]] = None
    # math.isfinite guards NaN: ``float("nan") > 0`` is False, which would
    # silently disable the deadline entirely with no log line.
    _dl = float(total_deadline_s or 0.0)
    deadline = monotonic() + _dl if math.isfinite(_dl) and _dl > 0 else None

    for index, provider in enumerate(chain):
        name = _provider_name(provider)
        record = ProviderAttempt(provider=name)
        trail.append(record)
        stop_chain = False

        # The primary gets the full retry budget; each fallback gets ONE shot.
        # A fallback is a last resort, not a second place to hammer. ddgs in
        # particular re-runs its lazy pip-install on every search() and leaks a
        # worker thread per timeout (plugins/web/ddgs/provider.py), so retrying
        # it multiplies both. It also removes a silent docstring/behaviour
        # mismatch: the shared wait budget already capped later providers at one
        # attempt once it was exhausted, just non-deterministically.
        provider_cap = attempt_cap if index == 0 else 1

        while record.attempts < provider_cap:
            if deadline is not None and monotonic() >= deadline:
                logger.info(
                    "web_search: total deadline reached, abandoning %s after %d attempt(s)",
                    name, record.attempts,
                )
                deadline_hit = True
                break
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
                # Re-raise ONLY on the pristine pass-through path: the primary,
                # with nothing transient seen yet. Re-raising from a fallback
                # would replace a real outage report with the fallback's own
                # exception and discard the whole trail, so the agent (and
                # Langfuse) would never learn the primary was down.
                if raised is not None and index == 0 and not saw_transient:
                    raise raised
                stop_chain = True
                break

            saw_transient = True
            if record.attempts >= provider_cap:
                break

            delay = min(_backoff(record.attempts), budget_left)
            if deadline is not None:
                delay = min(delay, max(0.0, deadline - monotonic()))
            if delay <= 0:
                logger.info("web_search: wait budget exhausted while retrying %s", name)
                break
            logger.info(
                "web_search: %s transient failure (attempt %d/%d), backing off %.1fs: %s",
                name, record.attempts, provider_cap, delay, detail[:200],
            )
            slept = _sleep(delay)
            budget_left = max(0.0, budget_left - slept)
            total_waited += slept
            if probe():
                return _interrupted(trail, total_waited)

        if stop_chain or deadline_hit:
            break

    if not saw_transient and not deadline_hit and last_response is not None:
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
# The ONLY automatic fallback is the keyless provider. This is a billing
# boundary, not a preference.
#
# The obvious design — walk registered providers filtered by ``is_available()``
# — is unsafe here, and its unsafety is invisible from this file. Rented
# tenants run ``web.search_backend: ddgs`` and are denied ``EXA_API_KEY`` so
# their searches cannot be billed to BigLobster's Exa account (issue #174).
# But that denial only strips the key from the tenant's ``.env`` FILE
# (docker/cont-init.d/03-biglobster-config), while ``exa.is_available()``
# resolves through ``hermes_cli.config.get_env_value``, which reads
# ``os.environ`` FIRST — and the cron scheduler's profile context
# (``cron/scheduler.py:_job_profile_context``) only adds and restores env
# keys, never deletes one the parent process already had. So on a tenant run
# ``exa.is_available()`` returns True and an availability walk resolves
# ``ddgs -> exa``: every ddgs blip silently bills BigLobster. Verified by
# reproduction, not by reading alone.
#
# Selecting only a keyless provider makes that class of leak structurally
# impossible regardless of what leaks into ``os.environ``. A paid provider is
# never chosen automatically; an operator who wants exa->tavily failover
# configures it deliberately.
#
# Trade-off, accepted: an install holding several paid keys falls back to a
# free scraper rather than to its second paid backend, so degraded results may
# rank worse than the primary would have. Losing the run is worse, and the
# degraded payload says which backend answered.
_KEYLESS_FALLBACKS = ("ddgs",)


def resolve_search_fallbacks(primary_name: str) -> List[Any]:
    """Return the keyless provider(s) to try after *primary_name* fails.

    Never returns a provider that requires an API key — see the comment above;
    this is the #174 billing boundary. A tenant already on ``ddgs`` gets an
    empty list (backoff only, no fallback), which is the correct and safe
    outcome: there is nothing free left to try.

    ``is_available()`` is deliberately NOT consulted. For ``ddgs`` it only
    probes whether the package is importable, while its ``search()``
    lazy-installs on first use — which is exactly how a freshly provisioned
    tenant gets its first web search today.
    """
    try:
        from agent.web_search_registry import get_provider
    except Exception as exc:  # noqa: BLE001 — no registry, no fallback
        logger.debug("fallback resolution unavailable: %s", exc)
        return []

    primary = (primary_name or "").strip()
    chosen: List[Any] = []
    for name in _KEYLESS_FALLBACKS:
        if name == primary:
            continue
        try:
            provider = get_provider(name)
            if provider is None or not provider.supports_search():
                continue
        except Exception as exc:  # noqa: BLE001 — a broken provider is skipped
            logger.debug("provider %s probe raised: %s", name, exc)
            continue
        chosen.append(provider)
    return chosen
