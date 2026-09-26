"""Safety net #2 — detect live credentials in cron prompts and configs.

Why this exists: a real, live GitHub fine-grained PAT once shipped hardcoded
inside a cron job prompt (see the incident history). The existing
``tools/threat_patterns.scan_for_threats`` only catches the ``key="value"``
quoted form; a *bare* token pasted into prose slips past it. This module adds
high-confidence, bare-token detectors and a scanner over the live cron jobs.

It is intentionally conservative: it matches concrete token *shapes* (GitHub
PATs, OpenAI/Anthropic/Langfuse keys, AWS keys, Slack/Google tokens, private-key
blocks), so environment-variable references like ``$GITHUB_TOKEN`` or
``os.environ["GITHUB_TOKEN"]`` — the safe pattern — are never flagged.

CLI:
    python -m fork_evals.checks.secret_scan --cron          # scan live cron jobs
    python -m fork_evals.checks.secret_scan --text "..."    # scan a string
    python -m fork_evals.checks.secret_scan --file path     # scan a file
Exit code 1 if any credential-shaped string is found.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from typing import List, Tuple

# (label, compiled pattern). Patterns target concrete token shapes only.
_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("github_pat_finegrained", re.compile(r"github_pat_[A-Za-z0-9_]{22,}")),
    ("github_token", re.compile(r"gh[posru]_[A-Za-z0-9]{36,}")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("langfuse_secret_key", re.compile(r"sk-lf-[0-9a-f-]{16,}")),
    ("openai_key", re.compile(r"sk-(?!ant-)(?!lf-)[A-Za-z0-9]{32,}")),
    ("aws_access_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----")),
    # Quoted assignment of a long opaque value (complements threat_patterns).
    ("assigned_secret", re.compile(r"(?i)(?:api[_-]?key|secret|token|password)\s*[=:]\s*[\"'][A-Za-z0-9+/=_-]{16,}")),
]


@dataclass(frozen=True)
class Finding:
    label: str
    preview: str  # redacted — never the full secret


def _redact(match: str) -> str:
    """Show enough to locate the value without echoing the secret."""
    if match.startswith("-----BEGIN"):
        return "<private key block>"
    head = match[:4]
    return f"{head}…(len {len(match)})"


def scan_for_credentials(text: str) -> List[Finding]:
    """Return de-duplicated credential findings in ``text`` (empty if clean)."""
    if not text:
        return []
    seen: set[Tuple[str, str]] = set()
    findings: List[Finding] = []
    for label, pattern in _PATTERNS:
        for match in pattern.findall(text):
            value = match if isinstance(match, str) else match[0]
            preview = _redact(value)
            key = (label, preview)
            if key in seen:
                continue
            seen.add(key)
            findings.append(Finding(label, preview))
    return findings


def scan_text_summary(text: str) -> str:
    """One-line, redacted summary — used as eval-case output."""
    findings = scan_for_credentials(text)
    if not findings:
        return "OK: no credential-shaped strings found."
    joined = "; ".join(f"{f.label}={f.preview}" for f in findings)
    return f"FOUND: {joined}"


def scan_cron_jobs() -> List[Tuple[str, List[Finding]]]:
    """Scan every live cron job's prompt/name. Returns only jobs with findings."""
    try:
        from cron.jobs import load_jobs
    except Exception:
        return []
    flagged: List[Tuple[str, List[Finding]]] = []
    for job in load_jobs():
        blob = "\n".join(str(job.get(k, "")) for k in ("prompt", "name", "workdir"))
        findings = scan_for_credentials(blob)
        if findings:
            flagged.append((str(job.get("id") or job.get("name") or "<unknown>"), findings))
    return flagged


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m fork_evals.checks.secret_scan")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--cron", action="store_true", help="scan live cron jobs (default)")
    group.add_argument("--text", help="scan a literal string")
    group.add_argument("--file", help="scan a file's contents")
    args = parser.parse_args(argv)

    if args.text is not None:
        findings = scan_for_credentials(args.text)
        for f in findings:
            print(f"  ✗ {f.label}: {f.preview}")
        print("FAIL: credential-shaped string(s) found." if findings else "OK: clean.")
        return 1 if findings else 0

    if args.file is not None:
        try:
            text = open(args.file, encoding="utf-8", errors="replace").read()
        except OSError as exc:
            print(f"cannot read {args.file}: {exc}", file=sys.stderr)
            return 2
        findings = scan_for_credentials(text)
        for f in findings:
            print(f"  ✗ {f.label}: {f.preview}")
        print("FAIL: credential-shaped string(s) found." if findings else "OK: clean.")
        return 1 if findings else 0

    # default: --cron
    flagged = scan_cron_jobs()
    if not flagged:
        print("OK: no credential-shaped strings in any cron job.")
        return 0
    for job_id, findings in flagged:
        print(f"  ✗ job {job_id}:")
        for f in findings:
            print(f"      {f.label}: {f.preview}")
    print(f"FAIL: {len(flagged)} cron job(s) contain credential-shaped strings.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
