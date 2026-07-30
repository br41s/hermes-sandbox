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
    echo "<rubric + PR diff>" | python -m auditor.llm --tier system
    python -m auditor.llm --tier system --show-model   # print resolved id only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import List, Optional

# Known-present, cheap fallbacks (from docker/config.yaml). Override via env.
SYSTEM_MODEL_DEFAULT = "deepseek/deepseek-v4-flash"
CONTENT_MODEL_DEFAULT = "openrouter/owl-alpha"

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_DEFAULT_SYSTEM_MSG = (
    "You are a rigorous senior software engineer performing a pre-merge review. "
    "Follow the reviewer instructions in the message exactly. Be concrete: cite "
    "file:line, state why an issue matters, and suggest the fix. Do not invent "
    "problems; if the change is sound, say so plainly."
)


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
    args = ap.parse_args(argv)

    if args.show_model:
        print(resolve_model(args.tier))
        return 0

    user_content = sys.stdin.read()
    if not user_content.strip():
        print("auditor.llm: no review content on stdin", file=sys.stderr)
        return 2
    print(review(args.tier, user_content))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
