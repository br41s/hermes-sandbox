# Memory Consolidator — continuous factual-memory learning

**Goal:** close the gap where lessons/corrections stay trapped in past sessions
and never reach the bounded `memory` store. Do it by reusing the existing
`curator` machinery, not a new weekly cron.

## Decided scope (v1)
- **What it learns:** ONLY explicit user corrections + errors resolved after
  multiple attempts. Highest signal, lowest poison risk. (Matches the
  lessons.md / errors.md philosophy in CLAUDE.md.)
- **Autonomy:** propose-and-approve → graduate to auto per the self-remediation
  apprenticeship model (gated → K clean approvals → auto), per class.
- **Topology:** per-profile isolated (mirrors HERMES_HOME/profile isolation).
  No cross-profile leakage in v1.
- **No paid services:** aux-model fork (already used by curator) + local SQLite
  session DB + local `bge-small` embeddings (hf-hub) if dedup needs it.

## Why extend the curator, not a new cron
- Curator already: inactivity-triggered (no daemon), aux-model (no main-cache
  cost), persistent state, forked AIAgent, strict "never auto-delete / only
  archive" invariants. Hermes's proposed weekly cron would run the EXPENSIVE
  model and duplicate this machinery.

## Hard constraint the design must respect
- `memory` store is a **bounded frozen snapshot** (default 2200 chars,
  tools/memory_tool.py:124) injected every turn for prefix-cache economics.
- Therefore the consolidator MUST do **two** ops, not just "add":
  1. promote new corrections/lessons
  2. **merge/evict** to keep the store high-signal when near the cap
  (a learn-only loop hits the wall and silently stops — the exact failure
  Hermes described as "if it fills, I stop saving").

## Blast radius
- A bad write poisons the frozen snapshot injected into EVERY turn of that
  profile → v1 stays propose-and-approve; threat-scanner in memory_tool is the
  second guardrail; apprenticeship gate is the third.

## Integration points (verified in code)
- Trigger: hook a sibling `maybe_run_memory_curator()` at the same idle sites
  (cli.py:12951, gateway/run.py:19239), internally gated by its own interval.
- Data source: `session_search._list_recent_sessions` + scroll over
  `db.list_sessions_rich(...)` to gather turns since `last_run_at`.
- Aux fork: reuse the `_run_llm_review(prompt)` / `_resolve_review_runtime`
  pattern from agent/curator.py with a corrections-extraction prompt.
- Output: proposals delivered to Telegram (existing delivery), approval writes
  via the existing `memory` tool add/replace actions.
- State: per-profile `.memory_curator_state` (last_run_at, run_count,
  per-class trust level for the auto graduation).

## Build slices (smallest shippable first)
1. **Read-only digest.** New module `agent/memory_curator.py`: enumerate
   sessions since last run, aux-fork extracts candidate corrections/errors NOT
   already in memory, emit a Telegram digest. No writes. Ship + observe.
2. **Approve-to-write.** Add inline approve action → writes via memory tool.
   Track per-class approval counts in state.
3. **Consolidation/eviction pass.** When store > threshold, aux-fork proposes
   merges/evictions (umbrella pattern, same as skills curator).
4. **Auto graduation.** Classes with K clean approvals flip to auto-apply
   (still logged + reversible), per profile.

## Verification
- Unit tests mirroring tests/agent/test_curator.py (idle gate, disabled gate,
  fresh-install defer, exception-swallow).
- Dry-run against a real profile's recent sessions; diff proposed vs. existing
  memory; confirm no duplicates and no snapshot bloat past the cap.
- /review on branch, /qa only if a UI/approval flow surfaces.

## Open questions before slice 2+
- Approval UX channel: Telegram inline vs. `hermes memory review` CLI?
- Graduation K value (self-remediation uses K=5).
- Dedup: string-match against existing entries, or embed with bge-small?
