"""Diagnose step ("Ollie"): on a failing case, ask a Hermes sub-agent to read
the implicated source and propose a unified diff.

Guardrail: this NEVER applies the diff. It prints the proposal for human review
(matches Hermes's delegate auto-deny default and Opik's explicit-approval model).
If the ``hermes`` CLI is unavailable it degrades to printing the assembled
diagnosis prompt + source paths, so a human (or Claude Code) can act on it.

Optionally enriches the prompt with the failing Langfuse trace via the public
read API, so the diagnosis sees the full span tree, not just the final output.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MAX_SOURCE_CHARS = 16000


def fetch_langfuse_trace(trace_id: str, *, timeout: int = 15) -> Optional[Dict[str, Any]]:
    """Fetch a trace's span tree via the Langfuse public read API, or None.

    Uses the same env credentials as the Langfuse tracing plugin. The plugin
    only *writes* traces; this is the read side the self-repair loop needs.

    ``GET /traces/{id}`` is deprecated (served until 2026-11-16); v4 has no
    trace-level object, so this fetches the trace's observation rows via
    ``GET /v2/observations?traceId=...`` and reconstructs trace-level
    input/output from the row Langfuse marks ``isRootObservation``. Verified
    against live traces from this plugin: the root ("Hermes turn" chain) can
    carry a non-null ``parentObservationId`` (its physical OTel parent lives
    outside this trace_id), so ``isRootObservation`` — not "no parent" — is
    the only reliable root marker here.
    """
    if not trace_id:
        return None
    public = (os.environ.get("HERMES_LANGFUSE_PUBLIC_KEY") or os.environ.get("LANGFUSE_PUBLIC_KEY") or "").strip()
    secret = (os.environ.get("HERMES_LANGFUSE_SECRET_KEY") or os.environ.get("LANGFUSE_SECRET_KEY") or "").strip()
    base = (os.environ.get("HERMES_LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com").strip().rstrip("/")
    if not (public and secret):
        return None

    params = urllib.parse.urlencode({
        "traceId": trace_id,
        "fields": "core,basic,io,trace_context",
        "limit": 100,
    })
    url = f"{base}/api/public/v2/observations?{params}"
    token = base64.b64encode(f"{public}:{secret}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted Langfuse host)
            payload = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None

    observations = payload.get("data") or []
    if not observations:
        return None
    root = next((o for o in observations if o.get("isRootObservation")), None)
    return {
        "traceId": trace_id,
        "input": root.get("input") if root else None,
        "output": root.get("output") if root else None,
        "observations": observations,
    }


def _read_source(rel_paths: List[str]) -> str:
    chunks: List[str] = []
    for rel in rel_paths:
        path = (_REPO_ROOT / rel).resolve()
        if _REPO_ROOT not in path.parents and path != _REPO_ROOT:
            continue  # path-traversal guard
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if len(text) > _MAX_SOURCE_CHARS:
            text = text[:_MAX_SOURCE_CHARS] + f"\n... [truncated {len(text) - _MAX_SOURCE_CHARS} chars]"
        chunks.append(f"===== {rel} =====\n{text}")
    return "\n\n".join(chunks)


def build_diagnose_prompt(
    case: Dict[str, Any],
    output: str,
    failures: List[str],
    *,
    trace: Optional[Dict[str, Any]] = None,
) -> str:
    sources = _read_source(case.get("source_hints", []))
    trace_block = ""
    if trace is not None:
        trace_json = json.dumps(trace, ensure_ascii=False)[:_MAX_SOURCE_CHARS]
        trace_block = f"\nFAILING LANGFUSE TRACE (span tree):\n{trace_json}\n"

    failure_lines = "\n".join(f"  - {f}" for f in failures)
    return f"""You are debugging the Hermes agent. An eval case failed. Find the root cause \
and propose a fix.

CASE: {case.get('name')}
WHAT THE BEHAVIOUR SHOULD BE:
{case.get('description', '').strip()}

FAILED ASSERTIONS:
{failure_lines}

ACTUAL USER-FACING OUTPUT:
\"\"\"
{output}
\"\"\"
{trace_block}
RELEVANT SOURCE:
{sources}

Respond with:
1. ROOT CAUSE: one paragraph.
2. PROPOSED FIX: a unified diff (```diff fenced block) touching only the lines needed.
Do not run any commands. Do not modify files. Only propose the diff for human review."""


def run_diagnose(
    case: Dict[str, Any],
    output: str,
    failures: List[str],
    *,
    trace_id: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    timeout: int = 300,
) -> str:
    """Produce a diagnosis + proposed diff. Returns the text (also printable)."""
    trace = fetch_langfuse_trace(trace_id) if trace_id else None
    prompt = build_diagnose_prompt(case, output, failures, trace=trace)

    cli = os.environ.get("HERMES_EVAL_CLI") or shutil.which("hermes")
    if not cli:
        return (
            "[diagnose] `hermes` CLI not found — cannot run the sub-agent.\n"
            "Hand the following prompt to a coding agent (e.g. Claude Code):\n\n"
            + prompt
        )

    cmd = [cli, "-z", prompt, "--toolsets", "software-development"]
    if model:
        cmd += ["--model", model]
    if provider:
        cmd += ["--provider", provider]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"[diagnose] sub-agent call failed: {exc}\n\nPrompt was:\n\n{prompt}"
    if proc.returncode != 0:
        return f"[diagnose] sub-agent exited {proc.returncode}: {proc.stderr.strip()}\n\nPrompt was:\n\n{prompt}"
    return proc.stdout.strip() or "[diagnose] sub-agent returned no output."
