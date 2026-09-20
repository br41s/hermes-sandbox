"""Auditor review model — resolve the per-tier model from env and call it.

Two knobs, set as plain env vars (Zeabur env vars, inherited by the agent
process and also stamped into the auditor profile's .env by cont-init §1c), so
the CEO can swap review models WITHOUT a redeploy — change the var, the next
cron run picks it up:

  HERMES_AUDITOR_SYSTEM_MODEL   — system-tier PRs (deep review; the real gate).
  HERMES_AUDITOR_CONTENT_MODEL  — content-tier PRs (light review; cheap).

Why a helper and not the agent's own model: an agent can't change its own model
mid-loop, and the delegate tool doesn't expose `model` to the LLM. So the cheap
orchestrator agent tiers each PR (see auditor/tiers.py) and calls this as an
LLM-as-judge step — the chosen model gets the rubric + diff and returns the
review. OpenRouter is OpenAI-compatible; we POST chat/completions over stdlib
urllib (no new deps; mirrors incidents/sweep.py's Langfuse call).

Defaults are known-present, CHEAP ids — deliberately conservative so a missing
env var degrades loudly-but-safely rather than silently spending. Set
HERMES_AUDITOR_SYSTEM_MODEL to your real strong reviewer.

Every env read here goes through ``_env_value``, NOT ``os.environ`` — the
credential this module needs is stripped from the very subprocess cron runs it
in. Read that function before changing any of them.

Exit codes (``main``): 0 reviewed · 2 bad arguments · 3 could not fetch the PR
(gate intact, this PR unreadable) · 4 the judge could not run (BROKEN GATE —
auditor.prompt PASO 2d blocks content merges and escalates on this one).

CLI:
    python -m auditor.llm --tier system --repo owner/name --number 247
    python -m auditor.llm --tier system --show-model   # print resolved id only
    echo "<rubric + PR diff>" | python -m auditor.llm --tier system   # local use only

Why --repo/--number and not a pipe: cron runs with ``approvals.cron_mode: deny``
and the Tirith scanner blocks every pipe into an interpreter, with no user
present to approve it. The piped form in the CLI line above is therefore
UNREACHABLE from a cron agent — the judge was never called, and content merges
were decided by the orchestrator model alone. Fetching the diff here keeps the
agent's command a plain argv invocation the gate allows. Do not reintroduce a
stdin-only path into auditor.prompt.
"""
from __future__ import annotations

import argparse
import contextlib
import signal
import threading
import json
import os
import subprocess
import sys
import time
import urllib.request
from typing import List, Optional, Tuple

# Known-present, cheap fallbacks (from docker/config.yaml). Override via env.
# `deepseek/deepseek-v4.1-flash` has no dated snapshot on OpenRouter yet (as of
# 2026-09-11) — it's the only slug for this model. Once OpenRouter ships dated
# snapshots for it, re-pin to the dated one: an undated alias with siblings
# has previously resolved to the oldest (priciest) snapshot, not the newest.
SYSTEM_MODEL_DEFAULT = "deepseek/deepseek-v4.1-flash"
# Was `openrouter/owl-alpha` until 2026-09-20, by which point that model no
# longer existed on OpenRouter (confirmed absent from the live /models list,
# 447 entries). A dead id is the one thing a default here must never be: the
# contract above is "degrades loudly-but-safely", and a 404 from the judge
# degrades to a BROKEN GATE (exit 4), not to a safe one. It went unnoticed
# because production sets HERMES_AUDITOR_CONTENT_MODEL, so the default never
# fired — and it only became load-bearing when langmap.json moved translation
# PRs into this tier.
#
# v4.1-flash is the standing default across Hermes (CEO, 2026-09-20), so both
# tiers fall back to it. That deliberately collapses the cheap/strong split at
# DEFAULT level only: the split still exists wherever it matters, because
# production sets HERMES_AUDITOR_CONTENT_MODEL explicitly. A default's job here
# is to be live and predictable, not to be the cheapest id available.
# (deepseek-v4-flash-0731 is ~4x cheaper at $0.04/$0.08 per M vs $0.15/$0.60
# if the content tier ever needs to economise — it is the dated slug, per the
# rule in BIGLOBSTER_SETUP.md that an undated alias resolves to the oldest,
# priciest snapshot.)
CONTENT_MODEL_DEFAULT = "deepseek/deepseek-v4.1-flash"

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_DEFAULT_SYSTEM_MSG = (
    "You are a rigorous senior software engineer performing a pre-merge review. "
    "Follow the reviewer instructions in the message exactly. Be concrete: cite "
    "file:line, state why an issue matters, and suggest the fix. Do not invent "
    "problems; if the change is sound, say so plainly."
)


# The judge's rubric. It lives here, not in auditor.prompt, because the agent can
# no longer pipe one in — see the module docstring. auditor.prompt keeps its own
# "WHAT TO REVIEW" copy for the agent's own read; if you change the criteria,
# change both. They are two consumers of one standard, and drift between them
# means the judge and the orchestrator grade differently.
_REVIEW_RUBRIC = """\
Review this pull request and return a verdict of BLOCK or APPROVE.

Judge only these, and spend your judgement nowhere else:
  - Correctness: does it do what it claims? Wrong logic, off-by-one, bad
    condition, unhandled None/empty/error path.
  - Security: injected input, leaked secret, widened permission, removed auth
    check, a token or key in the diff.
  - Regressions: a guard, invariant, or test this change breaks or removes. A
    deleted safety check is a blocker even when the replacement looks fine.
  - Blast radius: what else runs this code, and what happens when it is wrong.
  - Race / ordering: concurrent writes, mutated shared state, assumed sequencing.
  - Missing tests for any of the above.

Cite file:line. Do not invent problems; if the change is sound, say APPROVE
plainly. Style preferences are not blockers.
"""

# A diff large enough to blow the context window is itself a review signal, but
# truncating silently is not: the judge would grade a fragment while believing it
# saw the whole change. Cap, and say so in the text the model reads.
_MAX_DIFF_CHARS = 200_000

# --- generation bounds -----------------------------------------------------
# The judge model is a REASONING model (deepseek-v4.1-flash advertises
# `reasoning`, `reasoning_effort` and a 384 000-token max completion on
# OpenRouter). Sending no `max_tokens` and no `reasoning_effort` let it think
# for as long as it liked before emitting a single byte, and the call was not
# streamed — so time-to-first-byte WAS time-to-last-byte and total latency had
# no ceiling at all.
#
# That, not prompt size, is what broke the gate. The old rationale in this file
# blamed a bigger prompt for exceeding 300s, and biglobster#550 disproved it:
# 3 files, +195/-3, a ~20 KB payload, and it still burned 100% of the 420s
# bound. Latency tracked reasoning-token count, which was unbounded, so the
# observed spread (300s, 370s, 420s, 590s) was a distribution with no right
# edge — no fixed deadline can close that, it can only pick a failure rate.
#
# Cap the generation and the tail disappears. Keep the wall-clock deadline as
# the outer backstop, not as the primary control.
#
# CORRECTION (2026-09-20, FinView#266): the cap counts REASONING and verdict
# together, and `reasoning_effort: low` is not a share of it. OpenRouter's model
# record for deepseek-v4.1-flash advertises efforts max/high/low and no
# `supports_max_tokens`, so "low" is DeepSeek's own qualitative level — the
# "~20% of max_tokens" mapping in OpenRouter's docs applies only to models that
# take a token budget. #266 was 2 files, +41/-1, ~5 KB of judge input, and still
# ended finish_reason=length at 8000; the gate was down on 3 of 3 PRs that day.
# The request now asks for the usage frame and report_judge_usage logs the
# reasoning/verdict split on every exit path. Read THAT line before moving this
# number: mostly reasoning means the effort level is the lever, mostly verdict
# means the cap is.
#
# MEASURED (2026-09-20, the #266 request replayed in-container, temperature 0):
#   low, cap 8000   -> 8231 reasoning tokens, verdict never started, 358s, length
#   low, cap 32000  -> 21361 reasoning + 507 verdict tokens, APPROVE, 812s
#   reasoning off   -> 0 reasoning + 905 verdict tokens, BLOCK, 39s
# Throughput was ~27 tok/s in every variant, so the cap and the deadline below
# are one budget seen from two sides: a "low" call needs ~22k tokens AND ~13
# minutes, past the 600s foreground ceiling of the agent's terminal tool. The
# gate cannot afford that per PR on a single-thread cron pool.
#
# DECISION (2026-09-20, Brais): reasoning OFF. A verdict-only call is ~1k tokens
# and ~40s; the cap below is 8x the measured verdict and fits the deadline at
# the measured rate. The cost is judgement: the one off-reasoning sample
# BLOCKed #266 on a reading the human reviewer rejected. The verdict is one
# input to the auditor agent, not the merge decision, so that is acceptable.
JUDGE_MAX_TOKENS_DEFAULT = 8000
# "off" sends `reasoning: {"enabled": false}`. It must be explicit: this model
# reasons by DEFAULT at effort "high" (OpenRouter's record: default_enabled
# true, default_effort high), so merely omitting the field — which is what
# "off" used to do — hands the model the largest budget, not none. Variant C
# above was sent with exactly this payload and returned in 39s.
JUDGE_REASONING_EFFORT_DEFAULT = "off"


def judge_max_tokens() -> int:
    """Hard cap on the judge's completion, in tokens.

    Override with ``HERMES_AUDITOR_JUDGE_MAX_TOKENS``. Floor of 256 so a typo
    cannot cap every verdict into truncation — which fails CLOSED (see
    ``review``), but noisily and for every PR at once.
    """
    raw = (_env_value("HERMES_AUDITOR_JUDGE_MAX_TOKENS") or "").strip()
    if not raw:
        return JUDGE_MAX_TOKENS_DEFAULT
    try:
        return max(256, int(raw))
    except ValueError:
        return JUDGE_MAX_TOKENS_DEFAULT


def judge_reasoning_effort() -> str:
    """Reasoning setting: ``"off"``, ``"low"``, ``"medium"``, ``"high"``, or ``""``.

    Override with ``HERMES_AUDITOR_JUDGE_REASONING_EFFORT``:
      * ``off``  — send ``reasoning: {"enabled": false}``. The default.
      * ``low`` / ``medium`` / ``high`` — send ``reasoning_effort``.
      * ``none`` — send NOTHING, for a model that REJECTS the field (rather
        than ignoring it, which is OpenRouter's usual behaviour). Returned as
        ``""``. ``_env_value`` strips and returns ``""`` for both unset and
        empty, so blank cannot carry this meaning itself.
    Blank or unrecognised falls back to the default, like every other knob in
    this module — an unrecognised value used to mean "omit the field", which
    for this model meant reasoning at its default effort, "high".
    """
    raw = (_env_value("HERMES_AUDITOR_JUDGE_REASONING_EFFORT") or "").strip().lower()
    if raw in ("off", "low", "medium", "high"):
        return raw
    if raw == "none":
        return ""
    return JUDGE_REASONING_EFFORT_DEFAULT


def _gh(args: List[str], *, timeout: int = 120) -> Optional[str]:
    """Run a gh subcommand, returning stdout or None on any failure.

    argv list, never a shell string — so nothing here is a pipe the cron gate
    could refuse. Fail-safe None mirrors auditor.safety._pr_file_statuses: the
    caller turns it into a loud error rather than a blind review.
    """
    try:
        return subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=timeout, check=True
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"auditor.llm: gh {' '.join(args[:3])} failed: {e}", file=sys.stderr)
        return None


def fetch_pr_content(repo: str, number: int) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(review_text, error)`` — rubric + PR metadata + diff, ready to judge.

    Exactly one of the two is None. An unfetchable PR is an error, never an empty
    review: a judge handed a blank diff would approve it.
    """
    meta = _gh(["pr", "view", str(number), "--repo", repo,
                "--json", "title,body,additions,deletions,changedFiles"])
    if meta is None:
        return None, f"could not fetch PR #{number} metadata from {repo}"
    try:
        info = json.loads(meta)
    except json.JSONDecodeError as e:
        return None, f"unreadable PR metadata for {repo}#{number}: {e}"

    diff = _gh(["pr", "diff", str(number), "--repo", repo])
    if diff is None:
        return None, f"could not fetch the diff for {repo}#{number}"
    if not diff.strip():
        return None, f"{repo}#{number} returned an empty diff — refusing to judge nothing"

    note = ""
    if len(diff) > _MAX_DIFF_CHARS:
        diff = diff[:_MAX_DIFF_CHARS]
        note = (f"\n\n[TRUNCATED: the diff exceeds {_MAX_DIFF_CHARS} characters and was cut "
                "here. Judge only what is shown, and say the review is partial.]")

    body = (info.get("body") or "").strip() or "(no description)"
    header = (
        f"PR: {repo}#{number} — {info.get('title') or '(no title)'}\n"
        f"Files changed: {info.get('changedFiles')}  "
        f"(+{info.get('additions')} / -{info.get('deletions')})\n\n"
        f"Description:\n{body}\n"
    )
    return f"{_REVIEW_RUBRIC}\n{header}\n--- DIFF ---\n{diff}{note}", None


def _env_value(name: str) -> str:
    """Resolve *name* from the ACTIVE PROFILE's ``.env``, not just ``os.environ``.

    Why this exists, and why ``os.environ`` alone is wrong here:
    ``OPENROUTER_API_KEY`` is on the terminal backend's provider blocklist
    (``tools/environments/local.py`` — ``_HERMES_PROVIDER_ENV_BLOCKLIST``), so it
    is stripped from EVERY subprocess an agent spawns. The judge is spawned
    exactly that way — the cron orchestrator runs ``python -m auditor.llm`` as a
    terminal command — so ``os.environ.get("OPENROUTER_API_KEY")`` was
    guaranteed empty in production no matter what the container env held. The
    strip is deliberate (a model-authored shell command must not see provider
    keys) and ``tools/env_passthrough`` refuses to re-allow anything on that
    blocklist, so the judge must read the key from disk instead.

    ``hermes_cli.config.get_env_value`` is the per-profile resolver every other
    credential in this codebase uses (see ``tools/bl_site_health_tool.py``):
    ``os.environ`` first, then ``$HERMES_HOME/.env`` — which under the auditor
    profile is ``profiles/auditor/.env``, the file cont-init §1c/§1d writes the
    dedicated auditor key and the review-model knobs into.

    Degrades to ``os.environ`` if ``hermes_cli.config`` cannot be imported (a
    bare checkout, a test harness), mirroring ``tools/tts_tool.get_env_value``.
    """
    try:
        from hermes_cli.config import get_env_value as _get
    except Exception:  # noqa: BLE001 — resolution must never be the thing that breaks
        return (os.environ.get(name) or "").strip()
    try:
        return (_get(name) or "").strip()
    except Exception:  # noqa: BLE001
        return (os.environ.get(name) or "").strip()


# Credential names tried in order. The dedicated auditor key comes FIRST and by
# its own name: it is not on the provider blocklist, so it survives into the
# subprocess env even when the generic OPENROUTER_API_KEY does not, and it keeps
# the auditor's spend isolated from the content fleet's shared weekly cap (the
# whole reason HERMES_AUDITOR_OPENROUTER_API_KEY exists — see the 2026-09-01
# HTTP 402 outage). OPENROUTER_API_KEY is the documented fallback for a
# single-key install.
_API_KEY_VARS = ("HERMES_AUDITOR_OPENROUTER_API_KEY", "OPENROUTER_API_KEY")


def resolve_api_key() -> str:
    """First non-empty credential in ``_API_KEY_VARS``, or ``""`` if none."""
    key, _ = resolve_api_key_source()
    return key


def resolve_api_key_source() -> tuple:
    """``(key, var_name)`` — which variable actually supplied the credential.

    The name matters for diagnosis: ``OPENROUTER_API_KEY`` is on the subprocess
    env blocklist (tools/environments/local.py), so a judge resolving from THAT
    name is reading the profile ``.env`` and would find nothing if it were ever
    run as a bare agent subprocess. Resolving from the dedicated name is the
    healthy path. ``("", "")`` when nothing resolves.
    """
    for name in _API_KEY_VARS:
        value = _env_value(name)
        if value:
            return value, name
    return "", ""


# 420s, not 300s. The 300s bound was set to stop a hang (a judge call with
# timeout=120 ran 300s then 590s on br41s/biglobster#526) and it does that, but
# it also killed legitimate work: on 2026-09-18 the gate failed on 2 of 4 PRs,
# and a hand-run judge call against PR #273 was reproduced exceeding 300s with
# zero bytes of verdict. The other 2 PRs that run SUCCEEDED inside the bound, so
# the model is variable rather than systematically hung — a bigger prompt (this
# repo's own notes record a healthy call at 370s on 2026-09-16) simply takes
# longer than 300s.
#
# 420 clears that observed-healthy 370s with margin while staying well under the
# 590s hang, so the protection this bound exists for still fires.
#
# CORRECTION (2026-09-20, biglobster#550): the "a bigger prompt simply takes
# longer" reading above was WRONG, and raising 300 -> 420 only moved the
# failure rate. #550 was 3 files, +195/-3, a ~20 KB payload — the smallest
# input the gate had seen — and it still burned 100% of 420s. The variable was
# never prompt size; it was reasoning tokens, which the request did not cap.
# The real fix is JUDGE_MAX_TOKENS_DEFAULT + reasoning_effort + streaming (see
# above). This bound stays as the OUTER BACKSTOP for a hung socket, which is
# what it is good at. Do not raise it again to chase a slow call: if calls are
# slow now, the cap or the model is the thing to look at, and the elapsed line
# from report_judge_elapsed is the evidence.
#
# The cost is pool contention, and it is not small: the judge runs once per PR,
# so the worst case multiplies against auditor.pending's DEFAULT_LIMIT of 10 —
# 50 minutes of the single-thread cron pool becomes 70. That pool is what
# starved merge-on-green for 8 and 13 minutes on 2026-09-18. If that ceiling
# starts hurting, lower DEFAULT_LIMIT before raising this again: fewer PRs per
# run is cheaper than a longer per-call bound.
#
# MEASURED (2026-09-20, see JUDGE_MAX_TOKENS_DEFAULT): a HEALTHY low-effort call
# on the smallest diff the gate sees took 812s at ~27 tok/s, so 420 could never
# pass one — every call since #299 shipped died here or at the cap. That is why
# reasoning is now off: a verdict-only call took 39s, and the full 8000-token
# cap at the measured rate is ~300s, inside this bound. If the effort is ever
# turned back on, this number and the cap move together, and so must the
# agent side: the terminal tool's foreground ceiling is 600s
# (TERMINAL_MAX_FOREGROUND_TIMEOUT) and the auditor prompt passes no timeout.
JUDGE_DEADLINE_DEFAULT = 420


def judge_deadline_seconds() -> int:
    """Hard wall-clock bound on one judge call, in seconds.

    Override with ``HERMES_AUDITOR_JUDGE_DEADLINE_SECONDS``. Floor of 10s so a
    typo cannot make every review impossible.
    """
    raw = (_env_value("HERMES_AUDITOR_JUDGE_DEADLINE_SECONDS") or "").strip()
    if not raw:
        return JUDGE_DEADLINE_DEFAULT
    try:
        return max(10, int(raw))
    except ValueError:
        return JUDGE_DEADLINE_DEFAULT


def report_judge_elapsed(started: float, deadline: int, outcome: str) -> None:
    """Emit one judge call's wall-clock cost to stderr, on EVERY exit path.

    stdout carries the verdict, so this cannot go there. stderr lands in the
    auditor agent's tool output, which is what a later diagnosis actually reads.

    Why this exists: nothing recorded how long a judge call took. When the gate
    failed on 2 of 4 PRs on 2026-09-18 the only artefact was ``exit 4``, and the
    durations were unrecoverable — this module is raw ``urllib`` with no
    Langfuse instrumentation, so the trace does not hold them either, and
    ``agent.log`` never saw the subprocess. Answering "is the 300s bound too
    tight, or was the call hung?" meant re-running the judge by hand against a
    live PR. One line here turns that into a grep.

    Logged on SUCCESS too, not just on failure: a call that lands at 280s of a
    300s bound is the only warning that the next one will not, and that is
    exactly the signal a failure-only log cannot give.
    """
    elapsed = time.monotonic() - started
    used = (elapsed / deadline * 100) if deadline else 0.0
    print(
        f"auditor.llm: judge call {outcome} in {elapsed:.1f}s "
        f"(deadline {deadline}s, {used:.0f}% used)",
        file=sys.stderr,
    )


@contextlib.contextmanager
def wall_clock_deadline(seconds: int, what: str):
    """Raise ``TimeoutError`` if the block runs longer than ``seconds``.

    ``urllib.request.urlopen(timeout=...)`` is a PER-SOCKET-READ timeout, not a
    bound on total request time: a response that trickles bytes, or a connection
    the server holds open, resets the clock on every read and never trips it.
    That is how a judge call with ``timeout=120`` hung for 300s and then 590s on
    br41s/biglobster#526 (2026-09-17) and returned no verdict at all.

    A hung judge is worse than a failed one. Exit 4 is already wired to mean
    "the gate is broken" and makes the auditor fail closed and escalate; an
    indefinite hang instead holds the single-thread cron pool and starves every
    other agent while nobody is told anything.

    SIGALRM is main-thread-and-Unix only. Off that path this yields unchanged
    rather than pretending to bound anything — the CLI (the cron path) is
    exactly the supported case.
    """
    if not hasattr(signal, "SIGALRM") or threading.current_thread() is not threading.main_thread():
        yield
        return

    def _fire(_signum, _frame):
        raise TimeoutError(f"{what} exceeded its {seconds}s deadline")

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def resolve_model(tier: str) -> str:
    """Model id for a tier, env-first. Unknown tier => system (fail-safe, like
    tiers.classify — the important gate must never silently fall to the cheap one)."""
    if tier == "content":
        return _env_value("HERMES_AUDITOR_CONTENT_MODEL") or CONTENT_MODEL_DEFAULT
    return _env_value("HERMES_AUDITOR_SYSTEM_MODEL") or SYSTEM_MODEL_DEFAULT


def _build_request(
    model: str,
    messages: List[dict],
    api_key: str,
    *,
    session_id: Optional[str] = None,
    stream: bool = True,
    max_tokens: Optional[int] = None,
    reasoning_effort: Optional[str] = None,
) -> urllib.request.Request:
    payload: dict = {"model": model, "messages": messages, "temperature": 0}
    # Bound the generation. See JUDGE_MAX_TOKENS_DEFAULT: without these the
    # judge is a reasoning model with a 384k-token budget and no ceiling on
    # latency. Resolved here, not at the call site, so every caller (including
    # a future one) gets the bound by default rather than opting in.
    payload["max_tokens"] = judge_max_tokens() if max_tokens is None else max_tokens
    effort = judge_reasoning_effort() if reasoning_effort is None else reasoning_effort
    if effort == "off":
        # Explicit, not omitted: see JUDGE_REASONING_EFFORT_DEFAULT.
        payload["reasoning"] = {"enabled": False}
    elif effort:
        payload["reasoning_effort"] = effort
    # Stream. Not for progressive display — nothing reads this incrementally —
    # but so the socket has traffic on it: `urlopen(timeout=...)` is a
    # PER-READ timeout, so on a non-streamed call the whole generation is one
    # silent wait that the timeout cannot see into, and OpenRouter's keepalive
    # padding (`: OPENROUTER PROCESSING`) kept resetting even that. Streaming
    # turns each token into a read, so a genuine stall trips `timeout` in
    # seconds instead of holding the single-thread cron pool for minutes.
    if stream:
        payload["stream"] = True
    # Ask for the usage frame on the final chunk. It is the only place that
    # says how the completion split between reasoning and verdict — the one
    # number a `length` failure needs (FinView#266: 8000 tokens gone, nothing
    # on record to say whether the model thought them away or wrote long).
    payload["usage"] = {"include": True}
    # OpenRouter sticky-routing key (≤256 chars): pins consecutive auditor
    # reviews to the same upstream backend so the shared system-prompt/rubric
    # prefix stays cache-warm across reviews. Best-effort on OpenRouter's side.
    if session_id:
        payload["session_id"] = session_id[:256]
    # DeepSeek's context cache is backend-local. Without pinning, OpenRouter
    # load-balances deepseek/* across upstream backends with cold caches and
    # re-bills the full prefix (measured ~44% miss / 56% hit on the orchestrator
    # before this). Prefer the DeepSeek upstream so the cache is reused; keep
    # fallbacks ON so a DeepSeek outage degrades to another provider rather than
    # breaking the review gate. Only deepseek/* benefits — a model served from a
    # single OpenRouter-native backend has no cache to keep warm and needs no
    # pinning.
    if model.startswith("deepseek/"):
        payload["provider"] = {"order": ["deepseek"]}
    body = json.dumps(payload).encode("utf-8")
    return urllib.request.Request(
        _OPENROUTER_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # OpenRouter attribution headers (optional but polite / recommended).
            "HTTP-Referer": "https://github.com/br41s/hermes-sandbox",
            "X-Title": "hermes-auditor",
        },
        method="POST",
    )


def _read_stream(resp) -> Tuple[str, Optional[str], Optional[dict]]:
    """Accumulate an OpenRouter SSE stream into ``(text, finish_reason, usage)``.

    Three line shapes arrive on the wire and only one carries content:
      * ``: OPENROUTER PROCESSING`` — keepalive padding. This is the byte
        trickle that made a per-read socket timeout useless on the old
        non-streamed call; ignored here, but each one is still a read, so the
        socket clock only resets while the upstream is genuinely alive.
      * ``data: {json}`` — a chunk. ``delta.content`` is the verdict text;
        ``delta.reasoning`` (reasoning models emit it) is deliberately dropped,
        we grade on the conclusion, not the thinking.
      * ``data: [DONE]`` — end of stream.
    The last content chunk also carries ``usage`` (requested in
    ``_build_request``); it is returned as-is, ``None`` if it never came.

    An error can also arrive mid-stream AFTER a 200 OK (upstream timeout,
    provider fallback exhausted). That is raised, not returned: a partial
    verdict must never reach the orchestrator as if it were a whole one.

    So is a stream that simply STOPS. A connection cut mid-verdict yields
    content, no ``finish_reason`` and no ``[DONE]``, and the text it leaves
    behind looks like an ordinary short review — the one shape that would slip
    past every other check here and read as an approval. Either terminator is
    accepted (not both: a provider may omit one), neither is not.
    """
    chunks: List[str] = []
    finish_reason: Optional[str] = None
    usage: Optional[dict] = None
    saw_done = False
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            saw_done = True
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            # NOT a reassembly point: iterating the response yields whole
            # lines, so this is a genuinely malformed frame, and the only one
            # that matters is a final line cut by a dropped connection. Skip
            # it here; the terminator check below is what catches that.
            continue
        err = event.get("error")
        if isinstance(err, dict):
            raise RuntimeError(f"stream carried an error: {err.get('message') or err}")
        if isinstance(event.get("usage"), dict):
            usage = event["usage"]
        for choice in event.get("choices") or []:
            piece = (choice.get("delta") or {}).get("content")
            if piece:
                chunks.append(piece)
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
    if not saw_done and finish_reason is None:
        raise RuntimeError(
            "stream ended without a terminator — the connection dropped "
            "mid-verdict and the review is PARTIAL"
        )
    return "".join(chunks), finish_reason, usage


def report_judge_usage(model: str, usage: Optional[dict], verdict_chars: int) -> str:
    """Log how the completion split between reasoning and verdict; return the line.

    On every exit path, like ``report_judge_elapsed`` and for the same reason:
    a ``length`` failure without this line is undiagnosable. FinView#266 burned
    the whole 8000-token cap on a ~5 KB diff and nothing recorded whether the
    model thought the budget away or wrote a long verdict — and those two need
    opposite fixes (the effort level vs. the cap). ``n/a`` means the provider
    sent no usage frame, which is itself worth knowing.
    """
    u = usage if isinstance(usage, dict) else {}
    details = u.get("completion_tokens_details") or {}

    def _n(v):
        return v if isinstance(v, int) else "n/a"

    line = (
        f"prompt={_n(u.get('prompt_tokens'))} "
        f"completion={_n(u.get('completion_tokens'))} "
        f"reasoning={_n(details.get('reasoning_tokens'))} "
        f"verdict_chars={verdict_chars}"
    )
    print(f"auditor.llm: judge usage {model} {line}", file=sys.stderr)
    return line


def _liveness_path() -> "Path":
    from pathlib import Path  # local: keep module import cost down
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "incidents" / "judge-liveness.json"


def record_judge_success(now: Optional[str] = None) -> None:
    """Stamp the moment the judge last actually produced a verdict.

    This exists because the two obvious alarms — "the judge errored" and "the
    judge exited non-zero" — are FAILURE detectors, and the judge's real outage
    produced no failure to detect. It shipped on 2026-06-24 invoked through a
    pipe the cron approval gate refused, so for twelve weeks it was never
    invoked at all: nothing raised, nothing exited non-zero, and every content
    PR auto-merged on the cheap orchestrator model alone.

    Only a LIVENESS signal catches that shape. The incident watcher reads this
    file and raises when the judge has not SUCCEEDED recently
    (incidents/sweep.py::judge_liveness_incidents), which is true whether the
    judge is failing or was simply never called.

    Best-effort: a write failure must never fail a review that did run.
    """
    import json as _json
    from datetime import datetime, timezone
    try:
        path = _liveness_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = now or datetime.now(timezone.utc).isoformat()
        path.write_text(_json.dumps({"last_success_at": stamp}) + "\n", encoding="utf-8")
    except Exception:
        pass


def review(tier: str, user_content: str, *, system_msg: Optional[str] = None,
           timeout: int = 120) -> str:
    """Run a one-shot review at the tier's model. Returns the assistant text.

    Raises RuntimeError on missing key / HTTP / parse failure — the orchestrator
    sees the error and escalates rather than merging blind.
    """
    api_key = resolve_api_key()
    if not api_key:
        raise RuntimeError(
            "no OpenRouter credential for the judge — tried "
            + " then ".join(_API_KEY_VARS)
            + ", in os.environ and $HERMES_HOME/.env. The review did NOT run."
        )
    model = resolve_model(tier)
    messages = [
        {"role": "system", "content": system_msg or _DEFAULT_SYSTEM_MSG},
        {"role": "user", "content": user_content},
    ]
    # Stable per-tier session id → all reviews of a tier stick to one backend,
    # keeping the shared system-prompt/rubric prefix cache-warm across PRs.
    req = _build_request(model, messages, api_key, session_id=f"hermes-auditor-{tier}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text, finish_reason, usage = _read_stream(resp)
    except TimeoutError:
        # Either the wall-clock deadline fired or the socket stalled for
        # `timeout` with no chunk. Both are "the judge hung" and both must exit
        # 4; the elapsed line tells them apart (a stall lands near `timeout`, a
        # deadline at 100% of it). Propagate as-is — "it hung" and "it failed"
        # are different operational problems.
        raise
    except Exception as e:  # noqa: BLE001 — surface any failure to the caller
        raise RuntimeError(f"auditor.llm review failed (model={model}): {e}") from e

    # Fail CLOSED on a verdict that is not whole. Both of these used to be
    # impossible to hit and are now reachable because the completion is capped:
    # returning either one would hand the orchestrator something it parses as a
    # review, and a review with no BLOCK in it reads as approval.
    split = report_judge_usage(model, usage, len(text))
    if finish_reason == "length":
        raise RuntimeError(
            f"auditor.llm: {model} hit the {judge_max_tokens()}-token cap before "
            f"finishing its verdict ({split}) — the review is PARTIAL and must "
            f"not be trusted. Mostly reasoning: set "
            f"HERMES_AUDITOR_JUDGE_REASONING_EFFORT=off; mostly verdict: raise "
            f"HERMES_AUDITOR_JUDGE_MAX_TOKENS."
        )
    if not text.strip():
        raise RuntimeError(
            f"auditor.llm: {model} returned an empty verdict "
            f"(finish_reason={finish_reason!r}). The review did NOT run."
        )
    record_judge_success()
    return text


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Auditor review model caller (LLM-as-judge).")
    ap.add_argument("--tier", choices=["system", "content"], required=True)
    ap.add_argument("--show-model", action="store_true",
                    help="print the resolved model id and exit (verify env vars took effect)")
    ap.add_argument("--check", action="store_true",
                    help="report whether the judge COULD run (credential + model) and exit "
                         "0/4, without calling the model. Never prints the credential.")
    ap.add_argument("--repo", help="owner/name — fetch the PR's diff instead of reading stdin")
    ap.add_argument("--number", type=int, help="PR number; requires --repo")
    args = ap.parse_args(argv)

    if args.show_model:
        print(resolve_model(args.tier))
        return 0

    if args.check:
        # A full review is an LLM round-trip with a 120s timeout, which outlives
        # the Zeabur exec gateway — it answers 504 and tells you nothing. This
        # resolves the credential and the model and returns in milliseconds, so
        # "is the gate able to run at all" is answerable from a shell.
        #
        # Prints the VARIABLE NAME and length, never the value: keys have leaked
        # into transcripts in this repo before.
        key, source = resolve_api_key_source()
        model = resolve_model(args.tier)
        print(f"tier:       {args.tier}")
        print(f"model:      {model}")
        if not key:
            print("credential: NOT RESOLVED — tried " + ", ".join(_API_KEY_VARS)
                  + " in os.environ and $HERMES_HOME/.env")
            print("result:     FAIL — the judge cannot run; reviews would be unaided")
            return 4
        print(f"credential: resolved from {source} (len {len(key)})")
        if source == "OPENROUTER_API_KEY":
            print("note:       resolved from the SHARED key, which is on the subprocess "
                  "env blocklist — this works only because it came from the profile .env. "
                  "Set HERMES_AUDITOR_OPENROUTER_API_KEY to isolate auditor spend.")
        print("result:     OK — the judge can run")
        return 0

    if bool(args.repo) != (args.number is not None):
        print("auditor.llm: --repo and --number must be given together", file=sys.stderr)
        return 2

    if args.repo:
        # The cron path. Nothing is read from stdin, so the agent's command stays
        # a plain argv invocation the cron approval gate allows.
        user_content, err = fetch_pr_content(args.repo, args.number)
        if err:
            print(f"auditor.llm: {err}", file=sys.stderr)
            return 3
    else:
        # Local/interactive path only — unreachable from cron, see the docstring.
        user_content = sys.stdin.read()
        if not user_content.strip():
            print("auditor.llm: no review content on stdin", file=sys.stderr)
            return 2

    deadline = judge_deadline_seconds()
    started = time.monotonic()
    try:
        with wall_clock_deadline(deadline, "judge call"):
            print(review(args.tier, user_content))
    except TimeoutError as e:
        report_judge_elapsed(started, deadline, "TIMED OUT")
        print(
            f"auditor.llm: {e}. No verdict was produced — treat the gate as "
            f"BROKEN for this run, not as a degraded review. Check the elapsed "
            f"line above: near the deadline means the model ran long (look at "
            f"HERMES_AUDITOR_JUDGE_MAX_TOKENS and the reasoning budget, NOT at "
            f"the deadline); well under it means the socket stalled upstream.",
            file=sys.stderr,
        )
        return 4
    except RuntimeError as e:
        report_judge_elapsed(started, deadline, "FAILED")
        # Exit 4 == "the judge itself could not run" (no credential, HTTP or
        # parse failure), as distinct from exit 3 == "could not fetch the PR".
        # The orchestrator must treat these differently: an unfetchable PR is a
        # degraded review, a judge that cannot run is a BROKEN GATE. Collapsing
        # both into a traceback+exit 1 is what let a missing credential read as
        # a routine footnote on 2026-09-16. See auditor.prompt PASO 2d.
        print(f"auditor.llm: {e}", file=sys.stderr)
        return 4
    report_judge_elapsed(started, deadline, "OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
