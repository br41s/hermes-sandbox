"""``cronjob(action="sync_prompt")``: push a job's repo ``.prompt`` file into its live prompt.

Fork-only. ``tools/cronjob_tools.py`` keeps the action reachable exactly as
before (same arguments, same JSON) and delegates here, so the upstream file
carries a call site instead of the whole body.

``prompt_source`` only records a path for incidents.sweep's drift *detector*
(see ``prompt_drift_incidents``) — it never copies content on its own, so a
repo-side prompt fix silently never reaches the running job until someone
re-types it via ``update(prompt=...)``. This action closes that loop with one
call. ``scripts/sync_prompt_drift.py`` is the batch version and shares
:func:`prompt_sha`, so a baseline written by either is recognised by both.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional

# cron/fork_ext/prompt_sync.py -> repo root; prompt_source paths are relative to it.
REPO_ROOT = Path(__file__).resolve().parents[2]


def prompt_sha(text: str) -> str:
    """The ``prompt_synced_sha`` baseline for *text* (whitespace-trimmed sha256)."""
    return hashlib.sha256(text.strip().encode()).hexdigest()


def sync_prompt(job: Dict[str, Any], prompt_source: Optional[str], force: bool) -> str:
    """Sync *job*'s live prompt from its repo file; returns the tool's JSON string."""
    # Late-bound so monkeypatches of tools.cronjob_tools (update_job, the
    # prompt scanner) apply here exactly as they did when this lived inline.
    from tools import cronjob_tools as ct

    source = prompt_source if prompt_source is not None else job.get("prompt_source")
    if not source:
        return ct.tool_error(
            "Job has no prompt_source and none was provided. Set one with "
            "cronjob(action='update', prompt_source='<repo/path.prompt>') or "
            "pass prompt_source directly to this call.",
            success=False,
        )
    file_path = REPO_ROOT / source
    try:
        file_text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        return ct.tool_error(f"Could not read prompt_source '{source}': {exc}", success=False)
    scan_error = ct._scan_cron_prompt(file_text)
    if scan_error:
        return ct.tool_error(scan_error, success=False)
    live_text = job.get("prompt") or ""
    if file_text.strip() == live_text.strip():
        return json.dumps(
            {
                "success": True,
                "changed": False,
                "message": f"Job '{job['name']}' prompt already matches {source} — nothing to sync.",
                "job": ct._format_job(job),
            },
            indent=2,
        )

    # Clobber guard. This action only ever pushes repo -> live, so a fix
    # applied ONLY to the live job (the emergency path: a job is failing
    # in production and someone edits its prompt in place) is silently
    # destroyed by the next sync. That is not hypothetical for this repo
    # — the Gap Hunter's output-limit fix reached the live job days
    # before it reached the .prompt file.
    #
    # `prompt_synced_sha` records what this action last wrote. If the
    # live prompt no longer hashes to it, someone changed the live side
    # since, and their edit is what a sync would overwrite. Refuse and
    # make them look, rather than deciding for them which side wins.
    #
    # First sync of a job has no baseline and cannot be judged, so it
    # proceeds and records one — but says so, because that is exactly
    # the case an automated caller must not run unattended.
    live_sha = prompt_sha(live_text)
    baseline = job.get("prompt_synced_sha")
    if baseline and live_sha != baseline and not force:
        return ct.tool_error(
            f"Refusing to sync '{job['name']}': the live prompt has been "
            f"edited since the last sync, so this would overwrite that "
            f"edit with {source}.\n"
            f"  live sha256:     {live_sha[:12]}\n"
            f"  last synced sha: {baseline[:12]}\n"
            "Diff the two before deciding. If the live edit is the fix, "
            "port it INTO the repo file and sync that. If the repo is "
            "genuinely newer, re-run with force=True.",
            success=False,
        )

    updates: Dict[str, Any] = {
        "prompt": file_text,
        "prompt_synced_sha": prompt_sha(file_text),
    }
    if job.get("prompt_source") != source:
        updates["prompt_source"] = source
    updated = ct.update_job(job["id"], updates)
    result = {
        "success": True,
        "changed": True,
        "message": f"Job '{job['name']}' prompt synced from {source} ({len(file_text)} chars).",
        "job": ct._format_job(updated),
    }
    if not baseline:
        result["warning"] = (
            "No previous sync was recorded for this job, so a live-only "
            "edit could not have been detected — this sync was not "
            "verified against a baseline. A baseline is recorded now; "
            "future syncs are guarded. Automated callers should treat a "
            "missing baseline as needing human review."
        )
    elif force and live_sha != baseline:
        result["warning"] = (
            f"force=True overrode the clobber guard: the live prompt "
            f"({live_sha[:12]}) had been edited since the last sync "
            f"({baseline[:12]}) and that edit is now gone."
        )
    return json.dumps(result, indent=2)
