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
1. **Read-only digest.** ✅ DONE (PR #130). Module `agent/memory_curator.py`:
   enumerate sessions since last run, aux-fork extracts candidate
   corrections/errors NOT already in memory, persist a digest. No writes.
   CLI `hermes memory-curator run/status/show` added in PR #131.
2. **Approve-to-write.** ✅ DONE (this PR). Aux-fork now emits structured JSON
   proposals → `proposals.json`. `hermes memory-curator apply <id>|--all`
   writes via `MemoryStore.add` (dedup/cap/scan-guarded); each write recorded
   in `applied.jsonl`; `revert` undoes the last write. Per-target apply counts
   in state (`applied_by_target`) seed slice 4's graduation. Human-gated: the
   digest pass never writes on its own.
3. **Consolidation/eviction pass.**
   - 3a **Eviction** ✅ DONE (this PR). `run_consolidation` reads current
     entries and the aux-fork proposes evictions (transient / duplicate /
     obsolete) as `action:"evict"` proposals. `apply` routes evict →
     `MemoryStore.remove`; `revert` re-adds. Invented removal targets are
     filtered (entry must match an existing entry). CLI: `hermes
     memory-curator consolidate`. On-demand only (not scheduled).
   - 3b **Merge** ✅ DONE (same PR). `action:"merge"` proposals carry
     `sources` (>=2 existing entries) + `entry` (the replacement). `apply`
     pre-validates all sources exist, removes them, then adds the merged entry,
     rolling back the removals if the add fails. `revert` removes the merged
     entry and re-adds every source. The consolidation prompt proposes both
     evict and merge; merges with a missing/insufficient source are filtered.
4. **Auto graduation.** ✅ DONE. Each successful apply bumps a per-class
   (`action:target`) graduation count in state; a revert of that class resets it
   to 0 (trust withdrawn → demoted to gated). A class is graduated at
   `graduation_k` (default 5) clean approvals AND only for graduatable actions
   (default `["add"]` — evict/merge never auto-apply). When the master switch
   `auto_apply` is on (default OFF), `run_memory_digest` auto-applies graduated
   proposals (ledger-marked `auto`, still reversible); everything else stays
   proposed. Closes the continuous-learning loop: after K clean approvals the
   weekly digest auto-adds lessons with no human in the loop.

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
