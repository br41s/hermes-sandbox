"""Memory Curator — continuous factual-memory learning (slice 1: read-only digest).

Sibling of ``agent/curator.py``. Where the skills curator maintains procedural
memory (agent-created skills), this maintains *factual* memory: it surfaces
lessons that are trapped in past sessions and never reached the bounded
``memory`` store.

Slice 1 is deliberately **read-only**. It never writes to memory. It:
  1. enumerates recent sessions for the active profile (since the last run),
  2. reads the current MEMORY.md so the extractor can dedupe,
  3. spawns an auxiliary-model fork that extracts ONLY (a) explicit user
     corrections and (b) errors resolved after multiple attempts that are NOT
     already captured in memory,
  4. writes a markdown digest to disk and hands a one-line summary to a
     delivery callback.

Shares the curator's design contract:
  - inactivity-triggered (piggy-backs on the same idle hook, no new daemon)
  - auxiliary client (never touches the main session's prompt cache)
  - persistent state in ``.memory_curator_state``
  - never raises to the caller; every failure is swallowed and logged

Config lives under ``memory_curator.*`` in config.yaml. Unlike the skills
curator, this defaults **off** — it is opt-in because it makes aux-model calls
on a live instance. Enable with::

    memory_curator:
      enabled: true

Later slices add approve-to-write, consolidation/eviction, and auto-graduation
(see tasks/memory-consolidator.md). None of that lives here.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_HOURS = 24 * 7   # weekly, same cadence as the skills curator
DEFAULT_MIN_IDLE_HOURS = 2
DEFAULT_LOOKBACK_DAYS = 7          # only mine sessions active within this window
DEFAULT_MAX_SESSIONS = 20         # cap transcripts fed to the extractor
DEFAULT_PER_SESSION_CHARS = 6000  # per-session transcript budget (chars)

# Session sources that are noise for lesson mining (mirror session_search).
_HIDDEN_SESSION_SOURCES = ("cron", "curator", "memory_curator", "flush", "compression")


# ---------------------------------------------------------------------------
# .memory_curator_state — persistent scheduler + status
# ---------------------------------------------------------------------------

def _state_file() -> Path:
    return get_hermes_home() / "memory-curator" / ".memory_curator_state"


def _default_state() -> Dict[str, Any]:
    return {
        "last_run_at": None,
        "last_run_summary": None,
        "last_digest_path": None,
        "paused": False,
        "run_count": 0,
    }


def load_state() -> Dict[str, Any]:
    path = _state_file()
    if not path.exists():
        return _default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            base = _default_state()
            base.update({k: v for k, v in data.items() if k in base or k.startswith("_")})
            return base
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("Failed to read memory-curator state: %s", e)
    return _default_state()


def save_state(data: Dict[str, Any]) -> None:
    path = _state_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=".memory_curator_state_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.debug("Failed to save memory-curator state: %s", e, exc_info=True)


def set_paused(paused: bool) -> None:
    state = load_state()
    state["paused"] = bool(paused)
    save_state(state)


def is_paused() -> bool:
    return bool(load_state().get("paused"))


# ---------------------------------------------------------------------------
# Config access
# ---------------------------------------------------------------------------

def _load_config() -> Dict[str, Any]:
    """Read ``memory_curator.*`` from config.yaml. Tolerates a missing file."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
    except Exception as e:
        logger.debug("Failed to load config for memory-curator: %s", e)
        return {}
    if not isinstance(cfg, dict):
        return {}
    sub = cfg.get("memory_curator") or {}
    return sub if isinstance(sub, dict) else {}


def is_enabled() -> bool:
    """Default OFF — opt-in because it makes aux-model calls on a live instance."""
    return bool(_load_config().get("enabled", False))


def _int_cfg(key: str, default: int) -> int:
    try:
        return int(_load_config().get(key, default))
    except (TypeError, ValueError):
        return default


def get_interval_hours() -> int:
    return _int_cfg("interval_hours", DEFAULT_INTERVAL_HOURS)


def get_min_idle_hours() -> float:
    try:
        return float(_load_config().get("min_idle_hours", DEFAULT_MIN_IDLE_HOURS))
    except (TypeError, ValueError):
        return DEFAULT_MIN_IDLE_HOURS


def get_lookback_days() -> int:
    return _int_cfg("lookback_days", DEFAULT_LOOKBACK_DAYS)


def get_max_sessions() -> int:
    return _int_cfg("max_sessions", DEFAULT_MAX_SESSIONS)


# ---------------------------------------------------------------------------
# Idle / interval check
# ---------------------------------------------------------------------------

def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def should_run_now(now: Optional[datetime] = None) -> bool:
    """Return True if a digest pass should run immediately.

    Gates: enabled, not paused, and ``last_run_at`` present AND older than
    ``interval_hours``. First-run defers one full interval (seed only), so a
    fresh install doesn't fire on the first background tick. The explicit
    ``run_memory_digest(force=True)`` path bypasses this.
    """
    if not is_enabled() or is_paused():
        return False

    state = load_state()
    last = _parse_iso(state.get("last_run_at"))
    if now is None:
        now = datetime.now(timezone.utc)
    if last is None:
        try:
            state["last_run_at"] = now.isoformat()
            state["last_run_summary"] = (
                "deferred first run — seeded; will run after one interval"
            )
            save_state(state)
        except Exception as e:  # pragma: no cover — best-effort persistence
            logger.debug("Failed to seed memory-curator last_run_at: %s", e)
        return False

    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last) >= timedelta(hours=get_interval_hours())


# ---------------------------------------------------------------------------
# Data gathering (pure I/O, no LLM)
# ---------------------------------------------------------------------------

def _recent_sessions(db, since: datetime, limit: int) -> List[Dict[str, Any]]:
    """Root sessions active since ``since``, most-recent first, noise excluded."""
    try:
        rows = db.list_sessions_rich(
            limit=limit + 10,
            exclude_sources=list(_HIDDEN_SESSION_SOURCES),
            order_by_last_active=True,
        )
    except Exception as e:
        logger.debug("memory-curator: list_sessions_rich failed: %s", e)
        return []

    out: List[Dict[str, Any]] = []
    for s in rows:
        if s.get("parent_session_id"):
            continue  # skip child / delegation sessions
        last_active = _parse_iso(str(s.get("last_active") or "")) or _parse_iso(
            str(s.get("started_at") or "")
        )
        if last_active is not None:
            if last_active.tzinfo is None:
                last_active = last_active.replace(tzinfo=timezone.utc)
            if last_active < since:
                continue
        out.append(s)
        if len(out) >= limit:
            break
    return out


def _session_transcript(db, session_id: str, char_budget: int) -> str:
    """Compact user/assistant transcript for one session, capped at char_budget."""
    try:
        msgs = db.get_messages_as_conversation(session_id)
    except Exception as e:
        logger.debug("memory-curator: get_messages_as_conversation(%s) failed: %s",
                     session_id, e)
        return ""

    lines: List[str] = []
    for m in msgs:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue  # tool spam and system noise are not lesson signal
        content = m.get("content")
        if not isinstance(content, str):
            continue  # skip multimodal / structured parts in slice 1
        content = content.strip()
        if not content:
            continue
        lines.append(f"{role.upper()}: {content}")

    text = "\n".join(lines)
    if len(text) > char_budget:
        # Keep the tail — corrections and resolutions tend to land late.
        text = "…(truncated)…\n" + text[-char_budget:]
    return text


def _read_current_memory() -> str:
    """Current MEMORY.md text, for dedupe context. Empty string if absent."""
    try:
        from tools.memory_tool import get_memory_dir
        path = get_memory_dir() / "MEMORY.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
    except Exception as e:
        logger.debug("memory-curator: could not read MEMORY.md: %s", e)
    return ""


# ---------------------------------------------------------------------------
# Extraction prompt + aux-model fork
# ---------------------------------------------------------------------------

_DIGEST_INSTRUCTIONS = (
    "You are the MEMORY CONSOLIDATOR for an autonomous agent. Your job is to "
    "read recent conversation transcripts and surface durable lessons that are "
    "NOT already captured in the agent's persistent memory.\n\n"
    "SCOPE — extract ONLY two kinds of item:\n"
    "  1. Explicit user CORRECTIONS: the user told the agent it was wrong, or "
    "to do something differently (a preference, a rule, a constraint).\n"
    "  2. RESOLVED ERRORS: the agent made a mistake that took multiple attempts "
    "to fix — capture what failed and what finally worked.\n\n"
    "HARD RULES:\n"
    "  - Do NOT propose anything already present in CURRENT MEMORY below.\n"
    "  - Do NOT extract task progress, one-off facts, or transient TODOs.\n"
    "  - Do NOT call any tools. Do NOT write anything. This is a proposal only.\n"
    "  - If there is nothing new worth remembering, say exactly: NOTHING NEW.\n\n"
    "OUTPUT — markdown, one section per item:\n"
    "  ### <short title>\n"
    "  - **Lesson:** <the durable rule or fact>\n"
    "  - **Evidence:** <session id + a one-line paraphrase of what happened>\n"
    "  - **Suggested memory entry:** <the exact text you'd add to memory, ≤200 chars>\n"
)


def _build_extraction_prompt(current_memory: str, transcripts: List[Dict[str, str]]) -> str:
    parts = [_DIGEST_INSTRUCTIONS, "\n===== CURRENT MEMORY (do not duplicate) =====\n"]
    parts.append(current_memory.strip() or "(memory is empty)")
    parts.append("\n\n===== RECENT SESSIONS =====\n")
    for t in transcripts:
        parts.append(f"\n----- session {t['session_id']} — {t.get('title') or 'untitled'} -----\n")
        parts.append(t["text"])
    parts.append("\n\n===== END. Produce the digest now. =====\n")
    return "".join(parts)


def _run_extraction(prompt: str) -> Dict[str, Any]:
    """Spawn an auxiliary AIAgent fork to produce the digest. Never raises.

    Mirrors ``agent.curator._run_llm_review`` but uses this task's own aux slot
    (``auxiliary.memory_curator``), falling back to the curator slot, then the
    main chat model. The fork runs read-only: no memory, no context files.
    """
    import contextlib

    result: Dict[str, Any] = {"final": "", "summary": "", "model": "", "provider": "", "error": None}
    try:
        from run_agent import AIAgent
    except Exception as e:
        result["error"] = f"AIAgent import failed: {e}"
        result["summary"] = result["error"]
        return result

    api_key = base_url = api_mode = resolved_provider = None
    model_name = ""
    try:
        from hermes_cli.config import load_config
        from hermes_cli.runtime_provider import resolve_runtime_provider
        cfg = load_config()
        provider, model_name = _resolve_extraction_runtime(cfg)
        rp = resolve_runtime_provider(requested=provider, target_model=model_name)
        api_key = rp.get("api_key")
        base_url = rp.get("base_url")
        api_mode = rp.get("api_mode")
        resolved_provider = rp.get("provider") or provider
    except Exception as e:
        logger.debug("memory-curator provider resolution failed: %s", e, exc_info=True)

    result["model"] = model_name
    result["provider"] = resolved_provider or ""

    agent = None
    try:
        agent = AIAgent(
            model=model_name,
            provider=resolved_provider,
            api_key=api_key,
            base_url=base_url,
            api_mode=api_mode,
            max_iterations=8,          # a digest is prose, not a tool sweep
            quiet_mode=True,
            platform="memory_curator",
            skip_context_files=True,
            skip_memory=True,
        )
        agent._memory_nudge_interval = 0
        agent._skill_nudge_interval = 0
        with open(os.devnull, "w", encoding="utf-8") as devnull, \
                contextlib.redirect_stdout(devnull), \
                contextlib.redirect_stderr(devnull):
            conv = agent.run_conversation(user_message=prompt)
        final = ""
        if isinstance(conv, dict):
            final = str(conv.get("final_response") or "").strip()
        result["final"] = final
        result["summary"] = (final[:240] + "…") if len(final) > 240 else (final or "no digest")
    except Exception as e:
        result["error"] = f"error: {e}"
        result["summary"] = result["error"]
    finally:
        if agent is not None:
            try:
                agent.close()
            except Exception:
                pass
    return result


def _resolve_extraction_runtime(cfg: Dict[str, Any]) -> tuple[str, str]:
    """Pick (provider, model): memory_curator aux slot → curator slot → main."""
    aux = cfg.get("auxiliary", {}) if isinstance(cfg.get("auxiliary"), dict) else {}
    for slot in ("memory_curator", "curator"):
        s = aux.get(slot, {}) if isinstance(aux.get(slot), dict) else {}
        provider = (s.get("provider") or "").strip()
        model = (s.get("model") or "").strip()
        if provider and provider != "auto" and model:
            return provider, model
    main = cfg.get("model", {}) if isinstance(cfg.get("model"), dict) else {}
    return (main.get("provider") or "auto"), (main.get("default") or main.get("model") or "")


# ---------------------------------------------------------------------------
# Digest persistence
# ---------------------------------------------------------------------------

def _digest_dir() -> Path:
    return get_hermes_home() / "memory-curator"


def _write_digest(body: str, meta: Dict[str, Any]) -> Optional[str]:
    """Persist the digest markdown; also refresh ``latest.md``. Returns path."""
    try:
        root = _digest_dir()
        root.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        header = (
            f"# Memory digest — {ts}\n\n"
            f"- sessions scanned: {meta.get('sessions', 0)}\n"
            f"- model: {meta.get('provider', '?')}/{meta.get('model', '?')}\n\n"
            "> Read-only proposals. Nothing was written to memory.\n\n---\n\n"
        )
        content = header + body.strip() + "\n"
        path = root / f"digest-{ts}.md"
        path.write_text(content, encoding="utf-8")
        (root / "latest.md").write_text(content, encoding="utf-8")
        return str(path)
    except Exception as e:
        logger.debug("memory-curator: failed to write digest: %s", e, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_memory_digest(
    *,
    on_digest: Optional[Callable[[str], None]] = None,
    now: Optional[datetime] = None,
    force: bool = False,
) -> Optional[Dict[str, Any]]:
    """Run one read-only digest pass. Returns a result dict, or None if nothing
    to do. Never raises. ``force=True`` bypasses the interval gate (CLI/tests).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if not force and not should_run_now(now):
        return None

    try:
        from hermes_state import SessionDB
        db = SessionDB()
    except Exception as e:
        logger.debug("memory-curator: SessionDB unavailable: %s", e)
        return None

    since = now - timedelta(days=get_lookback_days())
    sessions = _recent_sessions(db, since, get_max_sessions())
    transcripts: List[Dict[str, str]] = []
    for s in sessions:
        text = _session_transcript(db, s.get("id", ""), DEFAULT_PER_SESSION_CHARS)
        if text:
            transcripts.append(
                {"session_id": s.get("id", ""), "title": s.get("title") or "", "text": text}
            )

    summary: str
    digest_path: Optional[str] = None
    if not transcripts:
        summary = "no recent sessions to mine"
        body = "NOTHING NEW — no sessions in the lookback window."
        digest_path = _write_digest(body, {"sessions": 0})
    else:
        prompt = _build_extraction_prompt(_read_current_memory(), transcripts)
        res = _run_extraction(prompt)
        body = res.get("final") or res.get("summary") or "NOTHING NEW"
        digest_path = _write_digest(
            body,
            {"sessions": len(transcripts), "model": res.get("model"),
             "provider": res.get("provider")},
        )
        nothing = body.strip().upper().startswith("NOTHING NEW")
        summary = (
            f"scanned {len(transcripts)} session(s) — "
            + ("no new lessons" if nothing else "new lessons proposed")
            + (f" → {digest_path}" if digest_path and not nothing else "")
        )

    state = load_state()
    state["last_run_at"] = now.isoformat()
    state["last_run_summary"] = summary
    state["last_digest_path"] = digest_path
    state["run_count"] = int(state.get("run_count", 0)) + 1
    save_state(state)

    if on_digest:
        try:
            on_digest(summary)
        except Exception as e:
            logger.debug("memory-curator on_digest callback failed: %s", e)

    return {"summary": summary, "digest_path": digest_path, "sessions": len(transcripts)}


def maybe_run_memory_curator(
    *,
    idle_for_seconds: Optional[float] = None,
    on_digest: Optional[Callable[[str], None]] = None,
    background: bool = True,
) -> Optional[Dict[str, Any]]:
    """Best-effort: run a digest pass if all gates pass. Never raises.

    Gated by ``should_run_now`` (enabled + interval) and, when the caller
    supplies a measurement, ``min_idle_hours``. When ``background`` is True the
    LLM pass runs in a daemon thread so it never blocks the caller's tick.
    """
    try:
        if not should_run_now():
            return None
        if idle_for_seconds is not None:
            if idle_for_seconds < get_min_idle_hours() * 3600.0:
                return None
        if background:
            t = threading.Thread(
                target=lambda: run_memory_digest(on_digest=on_digest, force=True),
                name="memory-curator",
                daemon=True,
            )
            t.start()
            return {"summary": "memory-curator started", "background": True}
        return run_memory_digest(on_digest=on_digest, force=True)
    except Exception as e:
        logger.debug("maybe_run_memory_curator failed: %s", e, exc_info=True)
        return None
