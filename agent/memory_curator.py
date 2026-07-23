"""Memory Curator — continuous factual-memory learning (slice 1: read-only digest).

Sibling of ``agent/curator.py``. Where the skills curator maintains procedural
memory (agent-created skills), this maintains *factual* memory: it surfaces
lessons that are trapped in past sessions and never reached the bounded
``memory`` store.

The digest pass is read-only. It:
  1. enumerates recent sessions for the active profile (since the last run),
  2. reads the current MEMORY.md so the extractor can dedupe,
  3. spawns an auxiliary-model fork that extracts ONLY (a) explicit user
     corrections and (b) errors resolved after multiple attempts that are NOT
     already captured in memory, as structured JSON proposals,
  4. persists proposals.json + a markdown digest and hands a one-line summary
     to a delivery callback.

Slice 2 adds the **write path**: ``apply_proposals`` writes approved proposals
to memory via the memory tool (dedup/cap/scan-guarded), recording each write in
a reversible ledger; ``revert_last`` undoes the most recent write. Writes are
human-gated — the digest pass never writes on its own.

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
import re
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
DEFAULT_GRADUATION_K = 5          # clean approvals before a class may auto-apply
DEFAULT_GRADUATABLE_ACTIONS = ("add",)  # only adds auto-apply; evict/merge never

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
        "pending_proposals": 0,
        "applied_by_target": {},
        "graduation": {},
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


# -- Auto-graduation (slice 4) -------------------------------------------------

def is_auto_apply_enabled() -> bool:
    """Master switch for auto-applying graduated classes. Default OFF.

    Even when a class has graduated, nothing is auto-written unless this is on —
    graduation is computed and surfaced regardless, but auto-apply stays a
    deliberate, separate opt-in because it writes with no human in the loop.
    """
    return bool(_load_config().get("auto_apply", False))


def get_graduation_k() -> int:
    """Clean human approvals a class needs before it may auto-apply."""
    return _int_cfg("graduation_k", DEFAULT_GRADUATION_K)


def get_graduatable_actions() -> List[str]:
    """Actions eligible for auto-apply. Default: adds only — auto-evicting or
    auto-merging (deleting existing memory) stays human-gated."""
    cfg = _load_config().get("graduatable_actions", DEFAULT_GRADUATABLE_ACTIONS)
    if not isinstance(cfg, list):
        return list(DEFAULT_GRADUATABLE_ACTIONS)
    return [str(a).strip().lower() for a in cfg if str(a).strip()]


def get_telegram_chat_id() -> str:
    """Telegram chat to notify about pending memory decisions. Empty = no notify."""
    return str(_load_config().get("telegram_chat_id", "") or "").strip()


def get_telegram_thread_id() -> str:
    """Optional Telegram topic/thread id within the chat. Empty = no thread."""
    return str(_load_config().get("telegram_thread_id", "") or "").strip()


def _class_key(action: str, target: str) -> str:
    return f"{action}:{target}"


def is_graduated(action: str, target: str) -> bool:
    """True if this class has enough clean approvals AND is auto-apply eligible."""
    if action not in get_graduatable_actions():
        return False
    grad = load_state().get("graduation") or {}
    if not isinstance(grad, dict):
        return False
    return int(grad.get(_class_key(action, target), 0)) >= get_graduation_k()


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


def _current_entries(target: str) -> List[str]:
    """Return the individual entries of a memory store (split on the § delimiter)."""
    try:
        from tools.memory_tool import get_memory_dir, ENTRY_DELIMITER
        fname = "USER.md" if target == "user" else "MEMORY.md"
        path = get_memory_dir() / fname
        if not path.exists():
            return []
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return []
        return [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]
    except Exception as e:
        logger.debug("memory-curator: could not read %s entries: %s", target, e)
        return []


# ---------------------------------------------------------------------------
# Extraction prompt + aux-model fork
# ---------------------------------------------------------------------------

_DIGEST_INSTRUCTIONS = (
    "You are the MEMORY CONSOLIDATOR for an autonomous agent. Read recent "
    "conversation transcripts and surface durable lessons NOT already captured "
    "in the agent's persistent memory.\n\n"
    "SCOPE — extract ONLY two kinds of item:\n"
    "  1. Explicit user CORRECTIONS: the user told the agent it was wrong, or "
    "to do something differently (a preference, a rule, a constraint).\n"
    "  2. RESOLVED ERRORS: a mistake that took multiple attempts to fix — "
    "capture what failed and what finally worked.\n\n"
    "HARD RULES:\n"
    "  - Do NOT propose anything already present in CURRENT MEMORY below.\n"
    "  - Do NOT extract task progress, one-off facts, or transient TODOs.\n"
    "  - Do NOT call any tools. This is a proposal only.\n\n"
    "OUTPUT — respond with a SINGLE json fenced code block and nothing else:\n"
    "```json\n"
    "{\"proposals\": [\n"
    "  {\n"
    "    \"target\": \"memory\",\n"
    "    \"title\": \"<short title>\",\n"
    "    \"lesson\": \"<the durable rule or fact>\",\n"
    "    \"evidence\": \"<session id + one-line paraphrase>\",\n"
    "    \"entry\": \"<exact text to add to memory, <=200 chars>\"\n"
    "  }\n"
    "]}\n"
    "```\n"
    "Use target \"memory\" for agent notes/conventions and \"user\" for facts or "
    "preferences about the user. If nothing new is worth remembering, return "
    "{\"proposals\": []}.\n"
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


_CONSOLIDATION_INSTRUCTIONS = (
    "You are the MEMORY CONSOLIDATOR for an autonomous agent. Its persistent "
    "memory is a BOUNDED store — when it fills, the agent stops learning. Your "
    "job is to propose entries to REMOVE so it stays high-signal.\n\n"
    "Two kinds of proposal:\n\n"
    "A) EVICT — remove ONE entry that is clearly:\n"
    "  1. TRANSIENT: task progress or one-off state, not a durable fact "
    "(e.g. 'PR #241 status', 'waiting for confirmation').\n"
    "  2. DUPLICATE: it repeats another entry — keep the fullest, evict the rest.\n"
    "  3. OBSOLETE: clearly superseded or no longer true.\n\n"
    "B) MERGE — fold TWO OR MORE overlapping entries into one tighter entry. "
    "List the exact existing entries in \"sources\" and the replacement in "
    "\"entry\". The merged entry MUST preserve every distinct fact from the "
    "sources — losing information is worse than leaving them unmerged.\n\n"
    "HARD RULES:\n"
    "  - NEVER evict/merge away a durable preference, rule, credential, path, or "
    "convention unless it is fully preserved elsewhere (or in the merged entry).\n"
    "  - When in doubt, KEEP it. Prefer proposing nothing over a risky change.\n"
    "  - Every \"entry\" (evict) and every string in \"sources\" (merge) MUST be "
    "the exact full text of an existing entry.\n"
    "  - Do NOT call any tools. This is a proposal only.\n\n"
    "OUTPUT — a SINGLE json fenced block and nothing else:\n"
    "```json\n"
    "{\"proposals\": [\n"
    "  {\"action\": \"evict\", \"target\": \"user\", \"title\": \"<label>\",\n"
    "   \"reason\": \"<transient | duplicate of ... | obsolete: why>\",\n"
    "   \"entry\": \"<exact full text of the entry to remove>\"},\n"
    "  {\"action\": \"merge\", \"target\": \"user\", \"title\": \"<label>\",\n"
    "   \"reason\": \"<why these overlap>\",\n"
    "   \"sources\": [\"<exact entry 1>\", \"<exact entry 2>\"],\n"
    "   \"entry\": \"<the merged replacement, preserving all facts, <=200 chars>\"}\n"
    "]}\n"
    "```\n"
    "If nothing should change, return {\"proposals\": []}.\n"
)


def _build_consolidation_prompt(mem_entries: List[str], user_entries: List[str]) -> str:
    parts = [_CONSOLIDATION_INSTRUCTIONS]
    for target, entries in (("memory", mem_entries), ("user", user_entries)):
        parts.append(f"\n===== CURRENT {target.upper()} ENTRIES (target: {target}) =====\n")
        if not entries:
            parts.append("(none)\n")
        for e in entries:
            parts.append(f"\n--- entry ---\n{e}\n")
    parts.append("\n\n===== END. Produce the eviction proposals now. =====\n")
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
# Proposal parsing + persistence
# ---------------------------------------------------------------------------

def _digest_dir() -> Path:
    return get_hermes_home() / "memory-curator"


def _proposals_path() -> Path:
    return _digest_dir() / "proposals.json"


def _ledger_path() -> Path:
    return _digest_dir() / "applied.jsonl"


def _parse_proposals(text: str) -> List[Dict[str, Any]]:
    """Extract a proposals list from the LLM response. Returns [] on any failure.

    Prefers a ```json fenced block; falls back to the outermost {...} span.
    Each proposal is normalized and assigned a stable id (p1, p2, …).
    """
    if not text:
        return []
    raw = None
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        raw = m.group(1).strip()
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            raw = text[start:end + 1]
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    items = data.get("proposals") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        entry = str(it.get("entry", "")).strip()
        if not entry:
            continue
        target = str(it.get("target", "memory")).strip().lower()
        if target not in ("memory", "user"):
            target = "memory"
        action = str(it.get("action", "add")).strip().lower()
        if action not in ("add", "evict", "merge"):
            action = "add"
        sources = it.get("sources", [])
        if not isinstance(sources, list):
            sources = []
        sources = [str(s).strip() for s in sources if str(s).strip()]
        out.append({
            "id": f"p{len(out) + 1}",
            "action": action,
            "target": target,
            "title": str(it.get("title", "")).strip(),
            "lesson": str(it.get("lesson", "")).strip(),
            "evidence": str(it.get("evidence", "")).strip(),
            "reason": str(it.get("reason", "")).strip(),
            "sources": sources,
            "entry": entry,
            "applied": False,
        })
    return out


def load_proposals() -> Dict[str, Any]:
    """Load the last run's proposals (source of truth for apply). Never raises."""
    path = _proposals_path()
    if not path.exists():
        return {"ts": None, "meta": {}, "proposals": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("proposals"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"ts": None, "meta": {}, "proposals": []}


def _render_digest_md(proposals: List[Dict[str, Any]], meta: Dict[str, Any], ts: str) -> str:
    consolidation = meta.get("mode") == "consolidation"
    title = "Memory consolidation" if consolidation else "Memory digest"
    scope = (f"- entries scanned: {meta.get('entries', 0)}" if consolidation
             else f"- sessions scanned: {meta.get('sessions', 0)}")
    lines = [
        f"# {title} — {ts}",
        "",
        scope,
        f"- model: {meta.get('provider', '?')}/{meta.get('model', '?')}",
        f"- proposals: {len(proposals)}",
        "",
        "> Proposals only. Apply with `hermes memory-curator apply <id>` "
        "(or `apply --all`); undo the last write with `revert`. "
        "Nothing is changed until you apply.",
        "",
        "---",
        "",
    ]
    if not proposals:
        lines.append(
            "NOTHING TO EVICT — memory looks lean." if consolidation
            else "NOTHING NEW — no unsaved lessons found."
        )
        return "\n".join(lines) + "\n"
    for p in proposals:
        mark = " ✅ applied" if p.get("applied") else ""
        action = p.get("action", "add")
        tag = {"evict": "🗑 EVICT ", "merge": "🔀 MERGE "}.get(action, "")
        lines.append(f"### [{p['id']}] {tag}{p.get('title') or p['entry'][:60]}{mark}")
        if p.get("lesson"):
            lines.append(f"- **Lesson:** {p['lesson']}")
        if p.get("reason"):
            lines.append(f"- **Reason:** {p['reason']}")
        if p.get("evidence"):
            lines.append(f"- **Evidence:** {p['evidence']}")
        lines.append(f"- **Target:** `{p['target']}`")
        if action == "merge":
            for s in p.get("sources", []):
                lines.append(f"- **Merge source:** {s}")
            lines.append(f"- **Into:** {p['entry']}")
        else:
            verb = "Remove" if action == "evict" else "Entry"
            lines.append(f"- **{verb}:** {p['entry']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def _save_proposals(data: Dict[str, Any]) -> None:
    """Persist proposals.json and re-render latest.md (to reflect applied flags)."""
    try:
        _digest_dir().mkdir(parents=True, exist_ok=True)
        _proposals_path().write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        md = _render_digest_md(
            data.get("proposals", []), data.get("meta", {}), data.get("ts") or ""
        )
        (_digest_dir() / "latest.md").write_text(md, encoding="utf-8")
    except Exception as e:
        logger.debug("memory-curator: failed to save proposals: %s", e)


def _persist_run(proposals: List[Dict[str, Any]], meta: Dict[str, Any]) -> Optional[str]:
    """Write proposals.json + a timestamped digest + latest.md. Returns digest path."""
    try:
        root = _digest_dir()
        root.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        _proposals_path().write_text(
            json.dumps({"ts": ts, "meta": meta, "proposals": proposals},
                       indent=2, ensure_ascii=False), encoding="utf-8")
        md = _render_digest_md(proposals, meta, ts)
        path = root / f"digest-{ts}.md"
        path.write_text(md, encoding="utf-8")
        (root / "latest.md").write_text(md, encoding="utf-8")
        return str(path)
    except Exception as e:
        logger.debug("memory-curator: failed to persist run: %s", e, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Apply / revert — the write path (slice 2). Human-gated, reversible.
# ---------------------------------------------------------------------------

def _memory_limits() -> tuple[int, int]:
    """(memory_char_limit, user_char_limit) from config; code defaults otherwise."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        mem = cfg.get("memory", {}) if isinstance(cfg.get("memory"), dict) else {}
    except Exception:
        mem = {}

    def _i(key: str, default: int) -> int:
        try:
            return int(mem.get(key, default))
        except (TypeError, ValueError):
            return default

    return _i("memory_char_limit", 2200), _i("user_char_limit", 1375)


def _memory_store():
    from tools.memory_tool import MemoryStore
    mlim, ulim = _memory_limits()
    return MemoryStore(memory_char_limit=mlim, user_char_limit=ulim)


def _append_ledger(entry: Dict[str, Any]) -> None:
    try:
        _digest_dir().mkdir(parents=True, exist_ok=True)
        with open(_ledger_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug("memory-curator: ledger append failed: %s", e)


def _apply_merge(store, target: str, sources: List[str], merged: str) -> tuple[bool, str, List[str]]:
    """Remove all sources then add the merged entry, rolling back on failure.

    Returns (success, error, removed_sources). Pre-validates that every source
    matches an existing entry before mutating, so a bad merge is rejected rather
    than half-applied. If any step fails, already-removed sources are re-added.
    """
    if not sources:
        return False, "merge has no sources", []
    existing = _current_entries(target)
    for s in sources:
        if not any(s in e or e in s for e in existing):
            return False, f"source not found: {s[:50]}", []
    removed: List[str] = []
    for s in sources:
        r = store.remove(target, s)
        if r.get("success"):
            removed.append(s)
        else:
            for rs in removed:
                store.add(target, rs)
            return False, f"remove failed ({r.get('error', '?')}), rolled back", []
    r = store.add(target, merged)
    if not r.get("success"):
        for rs in removed:
            store.add(target, rs)
        return False, f"merged-entry add failed ({r.get('error', '?')}), rolled back", []
    return True, "", removed


def apply_proposals(ids: Optional[List[str]] = None, *, apply_all: bool = False,
                    auto: bool = False) -> Dict[str, Any]:
    """Apply approved proposals to memory via the memory tool. Never raises.

    ``add`` → ``MemoryStore.add`` (dedup/cap/scan-guarded); ``evict`` →
    ``MemoryStore.remove``; ``merge`` → remove sources then add the replacement
    (with rollback). A bad, oversized, or unmatched op is reported, not silently
    applied. Each applied op is recorded in a JSONL ledger with its action (and,
    for merge, its sources) so ``revert_last`` can invert it.

    Every successful apply bumps the per-class (``action:target``) graduation
    count. ``auto=True`` marks the ledger record as machine-applied (used by the
    slice-4 auto-apply path); it does not change what is written.
    """
    data = load_proposals()
    proposals = data.get("proposals", [])
    if not proposals:
        return {"applied": [], "skipped": [], "errors": ["no proposals — run a digest first"]}

    wanted = None if apply_all else set(ids or [])
    if not apply_all and not wanted:
        return {"applied": [], "skipped": [], "errors": ["no proposal ids given (use --all)"]}

    store = _memory_store()
    state = load_state()
    counts = state.get("applied_by_target") or {}
    if not isinstance(counts, dict):
        counts = {}
    grad = state.get("graduation") or {}
    if not isinstance(grad, dict):
        grad = {}

    applied: List[str] = []
    skipped: List[str] = []
    errors: List[str] = []
    for p in proposals:
        if not apply_all and p["id"] not in wanted:
            continue
        if p.get("applied"):
            skipped.append(f"{p['id']} (already applied)")
            continue
        action = p.get("action", "add")
        ledger_rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "id": p["id"], "action": action,
            "target": p["target"], "entry": p["entry"],
        }
        if auto:
            ledger_rec["auto"] = True
        if action == "merge":
            ok, err, removed = _apply_merge(store, p["target"], p.get("sources", []), p["entry"])
            if not ok:
                errors.append(f"{p['id']}: {err}")
                continue
            ledger_rec["sources"] = removed
        elif action == "evict":
            res = store.remove(p["target"], p["entry"])
            if not res.get("success"):
                errors.append(f"{p['id']}: {res.get('error', 'apply failed')}")
                continue
        else:  # add
            res = store.add(p["target"], p["entry"])
            if not res.get("success"):
                errors.append(f"{p['id']}: {res.get('error', 'apply failed')}")
                continue
            # Count adds only — the graduation signal (slice 4) is about
            # lessons written, not evictions or merges.
            counts[p["target"]] = int(counts.get(p["target"], 0)) + 1

        p["applied"] = True
        applied.append(p["id"])
        _append_ledger(ledger_rec)
        # Build trust for this class — clean approvals graduate it (slice 4).
        ck = _class_key(action, p["target"])
        grad[ck] = int(grad.get(ck, 0)) + 1

    if not apply_all and wanted:
        missing = wanted - {p["id"] for p in proposals}
        for mid in sorted(missing):
            errors.append(f"{mid}: unknown proposal id")

    _save_proposals(data)
    state["applied_by_target"] = counts
    state["graduation"] = grad
    save_state(state)
    return {"applied": applied, "skipped": skipped, "errors": errors}


def revert_last() -> Dict[str, Any]:
    """Invert the most recently applied op. Never raises.

    Undo an ``add`` by removing the entry, an ``evict`` by re-adding it, and a
    ``merge`` by removing the merged entry and re-adding its sources. Ledger
    records without an ``action`` (pre-eviction) default to ``add``.
    """
    path = _ledger_path()
    if not path.exists():
        return {"reverted": None, "error": "no applied entries to revert"}
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError as e:
        return {"reverted": None, "error": f"ledger unreadable: {e}"}

    for i in range(len(lines) - 1, -1, -1):
        try:
            rec = json.loads(lines[i])
        except json.JSONDecodeError:
            continue
        if rec.get("reverted"):
            continue
        action = rec.get("action", "add")
        store = _memory_store()
        if action == "merge":
            # undo merge = remove the merged entry, re-add the sources
            res = store.remove(rec["target"], rec["entry"])
            if not res.get("success"):
                return {"reverted": None, "error": res.get("error", "revert failed")}
            for s in rec.get("sources", []):
                store.add(rec["target"], s)
        elif action == "evict":
            res = store.add(rec["target"], rec["entry"])       # undo evict = re-add
            if not res.get("success"):
                return {"reverted": None, "error": res.get("error", "revert failed")}
        else:
            res = store.remove(rec["target"], rec["entry"])    # undo add = remove
            if not res.get("success"):
                return {"reverted": None, "error": res.get("error", "revert failed")}
        rec["reverted"] = True
        lines[i] = json.dumps(rec, ensure_ascii=False)
        try:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as e:
            logger.debug("memory-curator: ledger rewrite failed: %s", e)

        # Make revert a true inverse of apply: clear the proposal's applied
        # flag so it can be re-applied, and decrement the per-target count.
        # Match on entry text (the real identity) rather than id, since ids
        # are recycled per digest run — a new run may have overwritten
        # proposals.json with different lessons sharing the same id.
        data = load_proposals()
        for prop in data.get("proposals", []):
            if prop.get("applied") and prop.get("entry") == rec.get("entry"):
                prop["applied"] = False
                break
        _save_proposals(data)

        st = load_state()
        counts = st.get("applied_by_target") or {}
        if not isinstance(counts, dict):
            counts = {}
        tgt = rec.get("target")
        # Only adds bump the count on apply, so only adds decrement it here.
        if action == "add" and tgt and int(counts.get(tgt, 0)) > 0:
            counts[tgt] = int(counts[tgt]) - 1
        st["applied_by_target"] = counts
        # Withdraw trust: a revert resets the class's graduation to 0, demoting
        # it back to human-gated. Auto-apply of this class stops until it earns
        # K clean approvals again.
        grad = st.get("graduation") or {}
        if isinstance(grad, dict) and tgt:
            grad[_class_key(action, tgt)] = 0
            st["graduation"] = grad
        save_state(st)

        return {"reverted": rec.get("id"), "target": rec.get("target")}
    return {"reverted": None, "error": "nothing to revert"}


# ---------------------------------------------------------------------------
# Telegram notification — a heads-up that memory decisions are waiting
# ---------------------------------------------------------------------------

def _http_post_json(url: str, payload: Dict[str, Any], timeout: float = 15.0) -> int:
    """POST JSON and return the HTTP status. Stdlib only; monkeypatched in tests."""
    import urllib.request
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 (https api)
        return int(getattr(resp, "status", 0) or resp.getcode())


def _build_telegram_message(proposals: List[Dict[str, Any]], auto_applied: List[str],
                            mode: str) -> str:
    """Compose the notification body. ``mode`` is 'digest' or 'consolidation'."""
    kind = "consolidación" if mode == "consolidation" else "aprendizaje"
    n = len(proposals)
    lines = [f"🧠 Memory curator — {n} propuesta(s) de {kind}"]
    for p in proposals[:8]:
        tag = {"evict": "🗑", "merge": "🔀"}.get(p.get("action", "add"), "➕")
        title = p.get("title") or (p.get("entry", "")[:60])
        mark = " ✅auto" if p.get("id") in auto_applied else ""
        lines.append(f"{tag} [{p.get('id')}] {title}{mark}")
    if n > 8:
        lines.append(f"…y {n - 8} más")
    if auto_applied:
        lines.append(f"\n{len(auto_applied)} auto-aplicada(s) — deshacer: hermes memory-curator revert")
    lines.append("\nRevisa: hermes memory-curator show")
    if len(auto_applied) < n:
        lines.append("Aplica:  hermes memory-curator apply --all")
    return "\n".join(lines)


def _notify_telegram(text: str) -> bool:
    """Send ``text`` to the configured Telegram chat/thread. Never raises.

    No-op (returns False) unless both a bot token (``TELEGRAM_BOT_TOKEN``) and a
    ``memory_curator.telegram_chat_id`` are configured — so it stays silent until
    the user opts in. The explicit chat/thread config sidesteps the per-profile
    delivery-routing hazard.
    """
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = get_telegram_chat_id()
    if not token or not chat_id:
        return False
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text,
                               "disable_web_page_preview": True}
    try:
        thread_id = get_telegram_thread_id()
        if thread_id:
            # Bot API types message_thread_id as Integer (unlike chat_id, which
            # may be a string). int() inside the try so a non-numeric config
            # fails gracefully instead of raising out of a "never raises" fn.
            payload["message_thread_id"] = int(thread_id)
        status = _http_post_json(
            f"https://api.telegram.org/bot{token}/sendMessage", payload
        )
        if status != 200:
            logger.debug("memory-curator telegram notify HTTP %s", status)
        return status == 200
    except Exception as e:
        logger.debug("memory-curator telegram notify failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_memory_digest(
    *,
    on_digest: Optional[Callable[[str], None]] = None,
    now: Optional[datetime] = None,
    force: bool = False,
    notify: bool = False,
) -> Optional[Dict[str, Any]]:
    """Run one read-only digest pass. Returns a result dict, or None if nothing
    to do. Never raises. ``force=True`` bypasses the interval gate (CLI/tests).

    ``notify=True`` sends a Telegram heads-up when there are proposals (the
    scheduled tick sets this; manual CLI runs don't, to avoid spam). Silent
    unless a bot token + ``telegram_chat_id`` are configured.
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

    if not transcripts:
        proposals: List[Dict[str, Any]] = []
        meta = {"sessions": 0}
        summary = "no recent sessions to mine"
    else:
        prompt = _build_extraction_prompt(_read_current_memory(), transcripts)
        res = _run_extraction(prompt)
        proposals = _parse_proposals(res.get("final") or "")
        meta = {"sessions": len(transcripts), "model": res.get("model"),
                "provider": res.get("provider")}
        n = len(proposals)
        summary = (
            f"scanned {len(transcripts)} session(s) — "
            + (f"{n} proposal(s) — review: hermes memory-curator show"
               if n else "no new lessons")
        )

    digest_path = _persist_run(proposals, meta)

    state = load_state()
    state["last_run_at"] = now.isoformat()
    state["last_run_summary"] = summary
    state["last_digest_path"] = digest_path
    state["pending_proposals"] = len(proposals)
    state["run_count"] = int(state.get("run_count", 0)) + 1
    save_state(state)

    # Auto-apply graduated classes (slice 4). Off unless the master switch is on;
    # only classes with K clean approvals and a graduatable action qualify. The
    # rest stay proposed for human review. A later revert demotes the class.
    auto_applied: List[str] = []
    if proposals and is_auto_apply_enabled():
        auto_ids = [p["id"] for p in proposals
                    if is_graduated(p.get("action", "add"), p["target"])]
        if auto_ids:
            rep = apply_proposals(auto_ids, auto=True)
            auto_applied = rep.get("applied", [])
            if auto_applied:
                pend = max(0, len(proposals) - len(auto_applied))
                summary += f" ({len(auto_applied)} auto-applied, {pend} pending)"
                st = load_state()
                st["last_run_summary"] = summary
                st["pending_proposals"] = pend
                save_state(st)

    if notify and proposals:
        _notify_telegram(_build_telegram_message(proposals, auto_applied, "digest"))

    if on_digest:
        try:
            on_digest(summary)
        except Exception as e:
            logger.debug("memory-curator on_digest callback failed: %s", e)

    return {"summary": summary, "digest_path": digest_path,
            "sessions": len(transcripts), "proposals": len(proposals),
            "auto_applied": auto_applied}


def run_consolidation(*, on_digest: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Propose evictions to keep the bounded store high-signal (slice 3).

    Read-only: reads the current entries, asks the aux fork which are transient,
    duplicate, or obsolete, and persists eviction proposals. Nothing is removed
    until ``apply``. On-demand only (not scheduled). Never raises.
    """
    now = datetime.now(timezone.utc)
    mem_entries = _current_entries("memory")
    user_entries = _current_entries("user")
    existing = mem_entries + user_entries

    if not existing:
        meta = {"mode": "consolidation", "entries": 0}
        digest_path = _persist_run([], meta)
        summary = "memory is empty — nothing to consolidate"
        proposals: List[Dict[str, Any]] = []
    else:
        prompt = _build_consolidation_prompt(mem_entries, user_entries)
        res = _run_extraction(prompt)
        parsed = _parse_proposals(res.get("final") or "")

        # Never let the model invent a removal target: an evict's entry, and
        # every source of a merge, must match an existing entry. Merges need
        # >=2 sources and a replacement. Reindex ids after filtering.
        def _matches(text: str) -> bool:
            return any(text in e or e in text for e in existing)

        proposals = []
        for p in parsed:
            if p.get("action") == "merge":
                srcs = p.get("sources", [])
                if len(srcs) >= 2 and p.get("entry") and all(_matches(s) for s in srcs):
                    proposals.append(p)
            else:
                p["action"] = "evict"  # consolidation non-merge = evict
                if _matches(p["entry"]):
                    proposals.append(p)
        for i, p in enumerate(proposals, 1):
            p["id"] = f"p{i}"
        meta = {"mode": "consolidation", "entries": len(existing),
                "model": res.get("model"), "provider": res.get("provider")}
        digest_path = _persist_run(proposals, meta)
        n = len(proposals)
        summary = (
            f"scanned {len(existing)} entr(ies) — "
            + (f"{n} change(s) proposed — review: hermes memory-curator show"
               if n else "nothing to consolidate")
        )

    state = load_state()
    state["last_run_at"] = now.isoformat()
    state["last_run_summary"] = summary
    state["last_digest_path"] = digest_path
    state["pending_proposals"] = len(proposals)
    state["run_count"] = int(state.get("run_count", 0)) + 1
    save_state(state)

    if on_digest:
        try:
            on_digest(summary)
        except Exception as e:
            logger.debug("memory-curator on_digest callback failed: %s", e)

    return {"summary": summary, "digest_path": digest_path, "proposals": len(proposals)}


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
                target=lambda: run_memory_digest(
                    on_digest=on_digest, force=True, notify=True),
                name="memory-curator",
                daemon=True,
            )
            t.start()
            return {"summary": "memory-curator started", "background": True}
        return run_memory_digest(on_digest=on_digest, force=True, notify=True)
    except Exception as e:
        logger.debug("maybe_run_memory_curator failed: %s", e, exc_info=True)
        return None
