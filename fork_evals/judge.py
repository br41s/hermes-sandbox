"""Judges for eval assertions, with a deterministic fallback.

Three modes:

* **TypeSafe judge** (``use_typesafe=True``): asks TypeSafe's System One API a
  single Noul (yes/no) question and reads back the *probability* that the
  assertion holds. Nothing has to be parsed out of prose, so there is no
  "unparseable verdict" state, and a judge that is genuinely torn can say so
  instead of being rounded to a boolean. Requires ``TYPESAFE_API_KEY``.
* **LLM judge** (``use_llm=True``): shells out to the ``hermes -z`` oneshot CLI
  and asks the configured model to return PASS/FAIL for the assertion against
  the produced output. Mirrors a real user turn (respects the operator's model /
  provider config). Requires the ``hermes`` CLI on PATH and provider creds.
* **Deterministic** (default): evaluates the structural ``check`` block from the
  case (``must_contain`` / ``must_not_contain`` / ``must_be_nonempty``). This is
  the path used in hermetic / CI runs where no model call is allowed.

Both model paths degrade to the deterministic path on any error, so a missing
key never turns a green suite red for the wrong reason. The degradation is
always visible in ``JudgeResult.mode``, and — unlike the LLM path — the TypeSafe
path carries the *cause* into the reason, because "the judge silently never ran"
is a failure this repo has already paid for once.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class JudgeResult:
    passed: bool
    reason: str
    mode: str  # "deterministic" | "llm" | "llm->deterministic"


def judge(
    output: str,
    assertion: Dict[str, Any],
    *,
    use_typesafe: bool = False,
    use_llm: bool = False,
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> JudgeResult:
    """Evaluate a single assertion against ``output``."""
    text = (assertion.get("text") or "").strip()
    check = assertion.get("check") or {}

    if use_typesafe:
        try:
            return _typesafe_judge(output, text)
        except _TypeSafeUnavailable as exc:
            det = _deterministic(output, check)
            return JudgeResult(
                det.passed,
                f"(TypeSafe judge unavailable: {exc}) {det.reason}",
                "typesafe->deterministic",
            )

    if use_llm:
        result = _llm_judge(output, text, model=model, provider=provider)
        if result is not None:
            return result
        det = _deterministic(output, check)
        return JudgeResult(det.passed, f"(LLM judge unavailable) {det.reason}", "llm->deterministic")

    return _deterministic(output, check)


def _deterministic(output: str, check: Dict[str, Any]) -> JudgeResult:
    if not check:
        return JudgeResult(
            False,
            "no deterministic `check` defined for this assertion; rerun with --llm-judge",
            "deterministic",
        )

    haystack = (output or "").lower()

    if check.get("must_be_nonempty") and not (output or "").strip():
        return JudgeResult(False, "output is empty", "deterministic")

    missing = [s for s in check.get("must_contain", []) if s.lower() not in haystack]
    if missing:
        return JudgeResult(False, f"missing required substrings: {missing}", "deterministic")

    present = [s for s in check.get("must_not_contain", []) if s.lower() in haystack]
    if present:
        return JudgeResult(False, f"contains forbidden substrings: {present}", "deterministic")

    return JudgeResult(True, "all deterministic checks passed", "deterministic")


_JUDGE_PROMPT = """You are a strict, literal test judge. Decide whether the AGENT OUTPUT \
satisfies the ASSERTION. Do not be lenient.

ASSERTION:
{assertion}

AGENT OUTPUT:
\"\"\"
{output}
\"\"\"

Reply with exactly PASS or FAIL on the first line, then one short line explaining why."""


def _llm_judge(
    output: str,
    assertion_text: str,
    *,
    model: Optional[str],
    provider: Optional[str],
    timeout: int = 120,
) -> Optional[JudgeResult]:
    """Return a JudgeResult from the model, or None if the CLI is unavailable."""
    cli = os.environ.get("HERMES_EVAL_CLI") or shutil.which("hermes")
    if not cli:
        return None

    prompt = _JUDGE_PROMPT.format(assertion=assertion_text, output=output)
    cmd = [cli, "-z", prompt]
    if model:
        cmd += ["--model", model]
    if provider:
        cmd += ["--provider", provider]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None

    verdict = (proc.stdout or "").strip()
    if not verdict:
        return None
    first = verdict.splitlines()[0].strip().upper()
    reason = verdict.splitlines()[1].strip() if len(verdict.splitlines()) > 1 else verdict
    if first.startswith("PASS"):
        return JudgeResult(True, reason, "llm")
    if first.startswith("FAIL"):
        return JudgeResult(False, reason, "llm")
    return None  # unparseable -> caller falls back to deterministic


# --- TypeSafe System One judge ----------------------------------------------
# A Noul answers with the probability that the assertion holds, so the verdict
# is a number instead of a keyword that has to survive a round trip through
# prose. Two thresholds rather than one: outside the band the verdict is clear,
# inside it the judge is genuinely torn. Collapsing that middle into a boolean
# is exactly how an ambiguous assertion becomes a confident green, so the band
# fails the assertion and names itself as the reason.
NOUL_PASS_ABOVE = 0.75
NOUL_FAIL_BELOW = 0.25

_TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
_TYPESAFE_MODEL = "jev-latest"
_TYPESAFE_TIMEOUT = 30

_NOUL_INSTRUCTIONS = (
    "Judge strictly and literally: does `agent_output` satisfy `assertion`? "
    "Judge only what the assertion states. Output that is merely plausible, "
    "well written, or close to the intended behaviour does not satisfy it."
)
_NOUL_CRITERIA = {
    "true": "agent_output satisfies everything the assertion requires.",
    "false": "agent_output misses, contradicts, or only partly meets the assertion.",
}


class _TypeSafeUnavailable(RuntimeError):
    """No verdict was produced. Carries the cause so the fallback can name it."""


def _typesafe_judge(
    output: str,
    assertion_text: str,
    *,
    timeout: int = _TYPESAFE_TIMEOUT,
) -> JudgeResult:
    """Return the System One verdict, or raise :class:`_TypeSafeUnavailable`."""
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        raise _TypeSafeUnavailable("TYPESAFE_API_KEY is not set")

    body = json.dumps({
        "state": {"agent_output": output, "assertion": assertion_text},
        "model": _TYPESAFE_MODEL,
        "questions": {
            "satisfied": {
                "type": "noul",
                "instructions": _NOUL_INSTRUCTIONS,
                "criteria": _NOUL_CRITERIA,
            }
        },
    }).encode("utf-8")
    request = urllib.request.Request(
        _TYPESAFE_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        noul = float(payload["answers"]["satisfied"]["noul"])
    except Exception as exc:  # noqa: BLE001 — everything degrades, but with its cause
        raise _TypeSafeUnavailable(f"{type(exc).__name__}: {exc}") from exc

    if noul >= NOUL_PASS_ABOVE:
        return JudgeResult(True, f"satisfied (noul={noul:.2f})", "typesafe")
    if noul <= NOUL_FAIL_BELOW:
        return JudgeResult(False, f"not satisfied (noul={noul:.2f})", "typesafe")
    return JudgeResult(
        False,
        f"uncertain (noul={noul:.2f}, inside the undecided band "
        f"{NOUL_FAIL_BELOW}-{NOUL_PASS_ABOVE}) — the assertion or the output is "
        f"ambiguous, which is not the same as the behaviour being wrong",
        "typesafe",
    )
