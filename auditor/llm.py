"""Auditor review model — resolve the per-tier model from env and call it.

Two knobs, set as plain env vars (Zeabur env vars, inherited by the agent
process and also stamped into the auditor profile's .env by cont-init §1b), so
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
import json
import os
import subprocess
import sys
import urllib.request
from typing import List, Optional, Tuple

# Known-present, cheap fallbacks (from docker/config.yaml). Override via env.
# `deepseek/deepseek-v4.1-flash` has no dated snapshot on OpenRouter yet (as of
# 2026-09-11) — it's the only slug for this model. Once OpenRouter ships dated
# snapshots for it, re-pin to the dated one: an undated alias with siblings
# has previously resolved to the oldest (priciest) snapshot, not the newest.
SYSTEM_MODEL_DEFAULT = "deepseek/deepseek-v4.1-flash"
CONTENT_MODEL_DEFAULT = "openrouter/owl-alpha"

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


def resolve_model(tier: str) -> str:
    """Model id for a tier, env-first. Unknown tier => system (fail-safe, like
    tiers.classify — the important gate must never silently fall to the cheap one)."""
    if tier == "content":
        return os.environ.get("HERMES_AUDITOR_CONTENT_MODEL", "").strip() or CONTENT_MODEL_DEFAULT
    return os.environ.get("HERMES_AUDITOR_SYSTEM_MODEL", "").strip() or SYSTEM_MODEL_DEFAULT


def _build_request(
    model: str,
    messages: List[dict],
    api_key: str,
    *,
    session_id: Optional[str] = None,
) -> urllib.request.Request:
    payload: dict = {"model": model, "messages": messages, "temperature": 0}
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
    # breaking the review gate. Only deepseek/* benefits — owl-alpha is single
    # OpenRouter-native backend and needs no pinning.
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


def review(tier: str, user_content: str, *, system_msg: Optional[str] = None,
           timeout: int = 120) -> str:
    """Run a one-shot review at the tier's model. Returns the assistant text.

    Raises RuntimeError on missing key / HTTP / parse failure — the orchestrator
    sees the error and escalates rather than merging blind.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set — cannot run review.")
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
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 — surface any failure to the caller
        raise RuntimeError(f"auditor.llm review failed (model={model}): {e}") from e
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"auditor.llm: unexpected response shape from {model}: {e}") from e


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Auditor review model caller (LLM-as-judge).")
    ap.add_argument("--tier", choices=["system", "content"], required=True)
    ap.add_argument("--show-model", action="store_true",
                    help="print the resolved model id and exit (verify env vars took effect)")
    ap.add_argument("--repo", help="owner/name — fetch the PR's diff instead of reading stdin")
    ap.add_argument("--number", type=int, help="PR number; requires --repo")
    args = ap.parse_args(argv)

    if args.show_model:
        print(resolve_model(args.tier))
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

    print(review(args.tier, user_content))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
