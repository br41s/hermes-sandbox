# Token Optimization — owl-alpha → paid main model

**Goal:** cut token consumption A LOT so a paid model is affordable as the main
model, without losing quality or data.

**Decisions (2026-06-27):**
- Optimize **provider-agnostic first** — no paid model picked yet. Build the
  levers that pay off regardless of provider; pick the model once the real
  per-call breakdown is known.
- **Lossless-first, lossy as opt-in fallback.** Lossless levers ship now.
  Middle-history summarization is gated behind a flag, off by default, with the
  raw trajectory retained on disk.

**Key findings (live loop, evidence-backed):**
- `trajectory_compressor.py` is offline training-data post-processing, NOT the
  live loop. Irrelevant to prod token spend.
- No runtime context compaction in `run_agent.py` — history + tool outputs
  accumulate verbatim. Only `HERMES_MAX_ITERATIONS` caps turn count.
- Prompt caching barely wired: one ephemeral breakpoint on the system message,
  qwen path only (`run_agent.py:4124`). Tool schemas + history prefix uncached.
- 82 tool modules (`tools/*.py`) → full schema block resent every call.
- Langfuse already traces per-call input/output/cache_read/cache_write/reasoning
  tokens + cost in prod since 2026-06-16 (`plugins/observability/langfuse`).

## Phase 0 — Measure (DONE 2026-06-27, via Langfuse prod traces)
- [x] Langfuse pull (30d, 7,529 generations):
      - avg input context/call = **57,872 tok**, p95 = **121,076**
      - avg output/call = **254 tok** → input:output = **228:1** (input is ~99% of cost)
      - total ~436M input tok/month
- [x] Cache behavior (raw observation sample):
      - owl-alpha WARM = **100% hit**, ~3.5k fresh/call (bills ~6% of prefix)
      - deepseek/deepseek-v4-flash = **50% hit**, ~15k fresh/call (mis-caches)
      - 30d uncached avg >> warm sample → cost = cache MISSES (cron cold-starts)
- [x] Paid projection: cache reliability ≈ **5×** saving; tool-gating ≈ **-35%** more.
      Both fully lossless.

**Corrected thesis (data overrode the a-priori guess):** caching is already
wired and excellent when WARM. The bill is cache MISSES on a fat ~58k prefix —
cron cold-starts and poorly-caching routes (deepseek-v4-flash 50%). The game is
`cache-hit-rate × prefix-size`, not "fewer tokens per turn". Output is ~0.4% of
cost; ignore it.

**Paid main model chosen: `deepseek/deepseek-v4-pro`** (DeepSeek = automatic
server-side prefix caching; NO cache_control markers — correctly handled).

## Phase 1 — Lossless cuts (RE-RANKED again after lever-#1 code audit)

### Lever #1 — Cache-hit reliability: LARGELY ALREADY DONE (verified 2026-06-27)
Code audit shows the prefix-stability lever is already implemented and correct:
- [x] System prompt built once/session, cached, rebuilt only on compression
      (`system_prompt.py:347`).
- [x] Date-only timestamp, explicit anti-cache-bust (`system_prompt.py:323`, PR #20451).
- [x] Cache-friendly tier order stable→context→volatile, one block (`:362`).
- [x] Deterministic call-ids (`run_agent.py:2879`).
- [x] DeepSeek correctly gets `(False,False)` no markers (`agent_runtime_helpers.py:1241`).
- [x] Empirical: owl-alpha 100% within-session hit (same builder path).
- The 50% miss was the **Auditor** workload (distinct cron-spaced reviews, TTL
      expiry between runs) — inherent, not a main-path bug.
- [x] **VALIDATED 2026-06-27 via prod Langfuse — main path is cache-SAFE.**
      Root mechanism: OpenRouter sticky routing keyed on top-level `session_id`
      (≤256 chars), confirmed by OpenRouter docs. DeepSeek caching is automatic
      (no cache_control). The OpenRouter profile already emits
      `body["session_id"]` unconditionally
      (`plugins/model-providers/openrouter/__init__.py:46`) → sticky routing →
      warm cache. This is WHY owl-alpha measured 100%. deepseek-v4-pro inherits
      the identical path. **No code change needed for the migration.**
      - Caveat to confirm at switch time: main resolves `provider==openrouter`
        (so the profile activates) and `agent.session_id` is stable per session.
- [x] **Auditor one-shot judge fixed (`auditor/llm.py`, 2026-06-27):** added
      stable per-tier `session_id=hermes-auditor-{tier}` + conditional
      `provider:{order:["deepseek"]}` (deepseek/* only; fallbacks kept ON so a
      DeepSeek outage degrades instead of breaking the gate). Tests added in
      `tests/auditor/test_llm.py`; behavior verified. Backwards-compatible.

- [x] **ORCHESTRATOR (bigger auditor spender) — CONFIG, not code:** the auditor
      orchestrator is a `run_agent.py` cron agent on deepseek-v4-flash. It
      cached erratically (56%) DESPITE the openrouter profile emitting
      session_id → proves OpenRouter session-stickiness is best-effort and
      DeepSeek needs explicit provider pinning. Set its OpenRouter
      `provider.order=["deepseek"]` via cron/env config (`providers_order`).
      **DONE 2026-07-02:** `03-biglobster-config` reconciles
      `provider_routing.order: ["deepseek"]` into the auditor profile
      config.yaml at boot (auditor ONLY — main-profile pinning stays deferred
      per the migration guidance below). Takes effect on next deploy; re-pull
      Langfuse cache-hit% after.

### MIGRATION GUIDANCE (deepseek-v4-pro as main) — SHARPENED
session_id stickiness alone is NOT a guarantee for DeepSeek (orchestrator
evidence). For a reliable cache on the paid main model, ALSO set explicit
provider pinning. Exact config — in `config.yaml` (see cli-config.yaml.example
lines ~112-145):

    provider_routing:
      order: ["deepseek"]

Path: `provider_routing.order` → `cli.py:3189 self._providers_order` →
`agent.providers_order` → `chat_completion_helpers.py:671 _prefs["order"]` →
`extra_body.provider.order` on the OpenRouter request. Fallbacks stay ON; the
"deepseek" slug is a no-op for non-deepseek models, so it's safe to leave set.
Lossless, deterministic. NOT set yet — flip when deepseek-v4-pro goes live
(CEO wants to observe owl-alpha stats post-auditor-fix first).

### Lever #2 — Shrink the prefix (SCOPED 2026-06-27: multi-component, not just tools)
Mechanism `tools/tool_search.py` ALREADY EXISTS (default enabled=auto,
threshold_pct=10) — defers MCP + non-core plugin tools only. 48 tools are
hardcoded core (`toolsets._HERMES_CORE_TOOLS`), never defer.

Measured prefix decomposition of the ~58k (char/4 estimates):
- Tool schemas ~10–20k (~10.5k for 25 tools; 9 browser + 9 kanban + 4 ha +
  computer_use/tts/image_gen are CORE = undeferrable dead weight for text tasks)
- Context files ~5–15k (skip_context_files default False; AGENTS.md 54KB →
  CONTEXT_FILE_MAX_CHARS=20k chars ≈ 5k tok injected EVERY prefix)
- Skills prompt + system text + memory ~20–30k
→ No single component dominates. Tool-gating ≈ 25–35% of prefix only.

Prioritized lossless slices (pick per main-agent's real role):
- [~] **2a. Profile-gate domain tool-clusters — MOSTLY ALREADY DONE.**
      Domain clusters are already `check_fn`-gated (toolsets.py:31): ha_* needs
      HASS_TOKEN, kanban_* needs HERMES_KANBAN_TASK, computer_use needs
      cua-driver, send_message needs gateway. Probe confirmed 0 of these load
      without their env. Only always-on optional cluster = browser (9 tools,
      ~1.2k tok ≈ 2% of prefix). Incremental win is SMALL. (deepseek-v4-pro not
      running yet → can't observe real usage; don't hard-exclude, would break.)
- [ ] **2b. Gate context-file injection** — does the main agent need AGENTS.md
      (~5k tok) in every prefix? Lower CONTEXT_FILE_MAX_CHARS or skip per-profile.
- [ ] **2c. Tune tool_search to `on`** + threshold for deepseek-v4-pro so
      deferrable (MCP/plugin) tools actually defer.
- [ ] **2d. Progressive skills disclosure** if the skills catalog is large.

### Lever #3 — Stale tool-result offloading (secondary)
- [ ] Via `tools/tool_result_storage.py`; caching already absorbs most repeated
      history, so lower priority.

## Phase 2 — Lossy, opt-in (flagged, off by default)
- [ ] Middle-history summarization on long sessions, behind a flag, raw
      trajectory retained on disk.

## Verification
- [ ] Re-pull Langfuse after each lever: input tokens/call + cost/session must
      drop; cache_read ratio must rise once caching is live.
- [ ] Eval-loop regression check (`evals/`) — quality must not regress.
