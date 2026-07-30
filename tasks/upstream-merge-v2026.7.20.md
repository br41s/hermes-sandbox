# Upstream merge → NousResearch/hermes-agent `v2026.7.20`

**Status:** planning complete, Phase 0 done, Phase 1 in progress. Nothing committed.
**Target:** tag `v2026.7.20` (2026-07-20), NOT `upstream/main`.
**Base:** `origin/main` = `a986f905f` — verified identical to what prod runs.

## Measured cost

| Metric | Value |
|---|---|
| Fork point | `c6501c0f4`, 2026-06-02 |
| Upstream ahead | 9,255 commits / 6,636 files (`v0.15.1` → `v0.19.0`) |
| We are ahead | 271 commits / 231 files |
| **Real conflicts** | **44 files** (41 content, 2 add/add, 1 modify/delete), ~150 hunks |
| Conflict-free custom dirs | `auditor/ remediation/ incidents/ evals/ gap-hunter/ onsite-seo/ offsite-geo/ infographic/ product-articles/ onboarding-content/ scripts/ tasks/ docs/` |
| Gain | 188 security commits; 78 perf (mostly desktop = N/A) |
| Rollback anchor | `ghcr.io/br41s/hermes-sandbox@sha256:65a07ab03203fb1bb67be8e116aabaae1d9c71ed31d377f458b21324a7ef01e1` |

Reproduce the conflict count (writes nothing):
`git merge-tree --write-tree origin/main v2026.7.20`

---

## Phase 0 — Username migration ✅ DONE (PR #141)

- [x] `braisntext` → `br41s` across ~60 refs, verified atomic
- [x] `auditor/tiers.py:162` key ↔ `docker/profiles/*/repos.txt` agree (silent content-tier misclassification avoided)
- [x] `cloudbuild.yaml` login + image refs on `br41s`
- [x] Confirmed old GHCR namespace **denies** — the old config was genuinely broken

## Phase 1 — Rollback insurance (IN PROGRESS)

- [x] Capture current prod digest as rollback anchor
- [x] `cloudbuild.yaml`: add immutable `:sha-<commit>` tag alongside `:latest` — **uncommitted**
- [x] Fail-loud guard if `_COMMIT_SHA` missing (no `:sha-unknown` placeholder)
- [ ] **CEO:** review + commit `cloudbuild.yaml`
- [ ] **CEO:** run one Cloud Build to prove the new substitution works and the `:sha-` tag lands
- [ ] Verify both tags exist in GHCR

> **ORDERING TRAP (hit 2026-07-30).** `gcloud builds submit` uploads the CURRENT
> DIRECTORY, not a git ref. The edit lives in the worktree and is uncommitted, so
> running the submit from the main checkout `/Users/brais/VSCODE/hermes-sandbox`
> sends the OLD template and fails with:
> `INVALID_ARGUMENT: key "_COMMIT_SHA" in the substitution data is not matched in the template`
> Harmless — rejected at validation, nothing built or pushed. The commit MUST reach
> the directory you submit from before the new substitution can work.

## Phase 2 — Local verification harness ✅ DONE

- [x] OrbStack running; buildx symlinked into `~/.docker/cli-plugins/`
- [x] Baseline image `hermes-baseline:pre-merge` built (arm64, 1.27 GB)
- [x] Confirmed image ships pytest 9.0.2 + full `tests/` → run the suite IN the container
- [x] Python 3.13.5 satisfies upstream's new `requires-python >=3.11,<3.14`
- [x] **PRE-MERGE BASELINE SETTLED — this is the merge gate:**
      **28,719 ✓ / 56 ✗ across 19 files** (of 28,917), with default network.
      Run it exactly this way both before and after the merge:
      `docker run --rm --entrypoint /bin/bash <image> -c 'cd /opt/hermes && scripts/run_tests.sh'`
      Do NOT pass `--network none` — it inflates failures ~5x (72 files / 294 tests)
      by breaking provider/DNS tests that are otherwise green.
      Known-failing files (a NEW name here = merge regression):
      `gateway/test_restart_drain` `gateway/test_restart_notification`
      `hermes_cli/test_cmd_update`(17) `hermes_cli/test_gateway`(4)
      `hermes_cli/test_gateway_wsl`(2) `hermes_cli/test_gateway_service`(10)
      `hermes_cli/test_startup_plugin_gating` `hermes_cli/test_update_yes_flag`(2)
      `plugins/image_gen/test_huggingface_provider` `test_biglobster_site_checkouts`
      `test_lint_config`(3) `test_live_system_guard_self_test`(4)
      `test_run_tests_parallel` `tools/test_local_background_child_hang`
      `tools/test_windows_native_support` `tools/test_mcp_stability`
      `tools/test_voice_mode`(3) `tools/test_web_providers` `tools/test_web_tools_config`
- [x] Triage `tests/test_biglobster_site_checkouts.py` — the only failure in OUR custom
      code. **Stale test, correct code:** `db554111b` (#61) added a THIRD checkout
      (`biglobster-infographic`) to the section-6b loop but never updated the test,
      which pinned the entire `for ckdir in ...; do` line as an exact string. Red ever
      since. Fixed by asserting per-checkout-name against the loop line, so a fourth
      cron fails on the real invariant instead of silently rotting the test.
      **Gate is therefore 28,720 ✓ / 55 ✗ across 18 files.**

> NEVER boot the image with its real entrypoint (`/init`) + prod env — it starts a
> SECOND Telegram poller and steals updates from prod. Always `--entrypoint` override.

> **THE GATE NOW NEEDS A MOUNT (changed by this merge).** Upstream added `tests/` to
> `.dockerignore` (line 77); we had no such rule and it **auto-merged silently — the file
> never conflicted**. The image therefore ships ZERO test files, and `scripts/run_tests.sh`
> exits **0** with `No test files to run`. That is a PASSING exit code for a suite that
> never ran — gotcha #1 from [[hermes-test-suite-gotchas]] arriving by a new route.
> Decision (CEO, 2026-07-30): KEEP upstream's exclusion (lean prod image) and mount the
> tests for gate runs:
> ```
> docker run --rm --entrypoint /bin/bash -v "$PWD/tests:/opt/hermes/tests:ro" \
>   <image> -c 'cd /opt/hermes && scripts/run_tests.sh'
> ```
> ALWAYS check the pass COUNT, never just the exit code or the failure count: 18 failing
> files dropping to 0 is an absence, not an improvement.

## Phase 3 — Pin behaviour before merging

Characterization tests for Tier C/D so a silent regression fails loudly:

- [x] **dashboard auth gate — ALREADY COVERED, write nothing new.**
      `tests/test_dashboard_lockdown_regression.py:53` asserts
      `frozenset(PUBLIC_API_PATHS) == EXPECTED_PUBLIC` — **exact set equality**, so any
      widening OR narrowing fails. Plus `audit_public_allowlist()` flags unexpected paths
      and `SENSITIVE_MARKERS` substrings, and `gate_decision()` tests pin `/api/mcp/*`,
      `/api/exec`, `/api/secrets`, `/api/config`, `/` as GATED.
      **MERGE ACTION — two files must be updated IN LOCKSTEP** or the suite goes red:
      1. `hermes_cli/dashboard_auth/public_paths.py` → union to 8 entries
      2. `evals/checks/dashboard_gate.py:23` `EXPECTED_PUBLIC` → add `"/api/cron/fire"`
      (`evals/` has NO upstream conflict, so git will not prompt — this is a manual step.)
      `/api/cron/fire` trips no `SENSITIVE_MARKERS` entry, so only the "unexpected" rule
      applies. This red test is the DESIGNED behaviour: "any drift must be a conscious
      change here." Do not silence it — update it deliberately.
- [ ] cron profile-scoped delivery routing + fail-closed profile resolution — existing
      coverage in `tests/cron/test_scheduler.py` + `tests/tools/test_cronjob_tools.py`;
      BOTH are in the 44-conflict set, so verify our assertions survive the merge
- [ ] `container_boot` GITHUB_TOKEN / git-cred reconciliation + auditor identity tripwires —
      existing `tests/hermes_cli/test_container_boot.py` (also a conflict file) +
      `tests/cron/test_cron_profile.py` (**deleted upstream** — decide before resolving)

## Phase 4 — The merge

Branch: `chore/upstream-merge-v2026.7.20`

- [ ] **Tier A** take upstream blind — 13× `web/src/i18n/*`, `website/docs/…/cron.md`, `model-catalog.json`
- [ ] **Tier B** mechanical — `.env.example .gitignore Dockerfile pyproject.toml hermes_constants.py models.py service_manager.py`; **regenerate** `uv.lock`, don't merge it
- [ ] **Tier C** careful, our logic — `cron/scheduler.py` `cron/jobs.py` `tools/cronjob_tools.py` `hermes_cli/cron.py` `container_boot.py` `skills_hub.py` `skills_tool.py` `skill_manager_tool.py` `approval.py` `auxiliary_client.py` telegram adapter, `image_gen/openrouter` (add/add)
- [ ] **Tier D** re-apply intent onto upstream's rewritten code (CEO decision — do NOT hunk-resolve):
  - [ ] `hermes_cli/web_server.py` (upstream +16,446/−4,756 over 312 commits)
  - [ ] `hermes_cli/main.py` (+5,872/−5,540)
  - [ ] `hermes_cli/dashboard_auth/middleware.py` — **security-critical**
  - [ ] `hermes_cli/dashboard_auth/public_paths.py` — **RESOLUTION ALREADY DETERMINED (verified
        2026-07-30): UNION both 7th entries, do NOT take upstream wholesale.**
        Both sides share 6 read-only paths, then diverge:
        - upstream adds `"/api/cron/fire"` (Chronos managed-cron webhook)
        - we add `"/api/delegate"` (BigLobster COO → Hermes, auth'd by `HERMES_CALLBACK_SECRET`)
        Taking upstream wholesale DROPS `/api/delegate` → the BigLobster orchestrator starts
        getting 302/401 with no error on our side. Silent breakage.
        Taking upstream's entry is SAFE — verified `plugins/cron_providers/chronos/verify.py`
        `verify_nas_fire_token()` fails CLOSED: `if not token or not expected_audience: return None`
        and `if not jwks_or_key: ... refusing token; return None`. We set no `cron.chronos.*`
        config, so `expected_audience` is `""` → every request 401s. Inert endpoint, not a
        public cron trigger.
        **Add a characterization test asserting the exact allowlist** — this file is the one
        place where a careless merge silently widens the unauthenticated attack surface.
- [ ] Decide `tests/cron/test_cron_profile.py` (deleted upstream, modified by us)
- [ ] **CEO:** review Tier D diffs, dashboard auth especially

## ⛔ RESUME HERE — gate down to 63 files / 216 tests failed, NOT fully triaged (2026-07-30)

Branch `chore/upstream-merge-v2026.7.20`, **[PR #144](https://github.com/br41s/hermes-sandbox/pull/144) (DRAFT)**.
All 44 conflicts resolved; image BUILDS. The three RESUME-HERE punch-list items below are
DONE, plus several additional real regressions found and fixed via the same process. Gate
went from an invalid 419-failing-files run down to **63 files / 216 tests failed** (of
2,151 files / ~44,039 tests — the corpus itself grew ~50% vs the 28,917 pre-merge baseline,
so raw totals aren't directly comparable; compare **filenames**, not counts). **Still not
fully triaged against the baseline-18 list — do not merge or deploy.**

### Punch-list items — ALL DONE
1. ✅ **F821 undefined names fixed.** `mirror_enabled`/`mirror_text` (cron/scheduler.py —
   threaded through `_send_to_targets` as new params, computed in `_deliver_result`),
   `normalized_profile`/`_normalize_profile` (cron/jobs.py — the whole function had been
   dropped, not just the call), `SKILLS_DIR` (tools/skills_sync.py → `_skills_dir()`, a
   security-critical rmtree scope guard that was raising NameError on every call). The
   remaining 5 ruff F821 hits (`RateLimitState`, `Path` in whatsapp_common.py,
   `DashboardOAuthFlow`, `uvicorn`, `PatchResult`) are all pre-existing quoted-string forward
   references never evaluated at runtime (confirmed no `TYPE_CHECKING` needed) — harmless,
   left alone.
2. ✅ **pytest-asyncio restored** — added to `[dependency-groups] dev` in pyproject.toml +
   `uv lock`. This alone fixed 321 of the original 419 failing files.
3. ✅ **Rebuilt + re-gated repeatedly** with the tests/ mount, comparing failing filenames
   each round.

### Additional regressions found and fixed along the way (same "kept a use, dropped a
producer" failure mode, surfaced by running the actual test suite rather than just ruff)
- **`profile` cron parameter dropped wholesale** — not just `_normalize_profile`, but the
  entire per-job profile plumbing: `create_job`'s parameter, `update_job`'s validation block,
  and in `tools/cronjob_tools.py` the `cronjob()` param + create/update call sites + JSON
  schema entry + registry lambda passthrough, and in `hermes_cli/cron.py` the CLI
  create/edit/list plumbing. Restored all of it from pre-merge; verified against
  `tests/cron/test_cron_profile.py`.
- **`tests/tools/test_cronjob_tools.py` had a genuine merge-splice corruption** — two
  unrelated test classes' bodies got fused: `TestProfileRoutingGapWarning`'s 5 tests + fixture
  were misplaced into `TestLocalDeliveryNotice`, and the tail of one test got concatenated
  with the *body* of upstream's separate session-reset fixture (a stray top-level `yield` that
  made the whole file fail to collect — pytest doesn't allow `yield` in a plain test). Rebuilt
  both classes to their correct, complete forms.
- **`cron/scheduler.py` `_deliver_result` was missing upstream's #43014 fix** — `deliver=origin`
  (or auto-detect) with no resolvable origin/home-channel used to hard-error on every run for
  CLI-created jobs; upstream fixed this to treat it as local (no error). The merge resolution
  kept our old unconditional-error version. Restored upstream's version verbatim.
  (`tests/cron/test_scheduler.py::TestDeliverOriginUnresolvableIsLocal`, now fully green.)
- **`tick()`'s sequential-vs-parallel job partition only checked `workdir`, not `profile`** —
  profile jobs were running on the parallel pool again, exactly the race the sequential pool
  exists to prevent. Restored the `workdir OR profile` partition from pre-merge.
- **`tests/hermes_cli/test_dashboard_auth_session_cache.py`** — stale test predates a new
  upstream feature (provider-hint cookie tagged onto `call_next`'s response); the test's
  `_call_next` stub returned a bare string, which doesn't have `.set_cookie`. Fixed by making
  the sentinel a `str` subclass with a no-op `set_cookie` (preserves every existing
  `out == "PASS:..."` assertion unchanged).
- **`tests/cron/test_cron_profile.py`** — two more stale-test issues unrelated to the profile
  regression above: (a) `dotenv.load_dotenv` patch target was wrong (`env_loader.py` does
  `from dotenv import load_dotenv` at module-import time, so patching the `dotenv` package
  attribute never touches it — classic "patch where it's used" trap; repointed both tests at
  `env_loader._load_dotenv_with_fallback` and made sure the profile's `.env` file actually
  exists so the loader's `.exists()` guard fires); (b) `fake_run_job` stub didn't accept the
  new `defer_agent_teardown` kwarg `tick()` now always passes; (c) the "sequential" assertion
  hard-coded `== MainThread`, which broke when upstream's dispatch rewrite moved sequential
  jobs off the calling thread onto a persistent single-worker `cron-seq` pool (still strictly
  serialized — just not inline). Rewrote to assert on pool identity (`cron-seq` vs
  `cron-parallel` thread-name prefixes), which is the actual invariant.

### NOT yet triaged — 63 failing files, ~35–40 of which are NOT on the pre-merge baseline-18
list and haven't been individually checked. Sampled a few (`test_container_boot.py`,
`test_dashboard_auth_401_reauth.py::test_valid_legacy_session_is_migrated_with_provider_hint`)
and found at least one more real, unresolved issue: a legacy session (valid AT cookie, no
provider-hint cookie yet) should get the provider-hint cookie set on response but doesn't —
not yet root-caused. The rest of the 63 have not been individually classified as
"pre-existing/environmental" vs "new regression." `docs/relay-connector-contract.md` also
appears to be missing from the built image (`test_contract_doc_conformance.py`) — separate
from conflict-resolution quality, likely a `.dockerignore`/COPY scoping issue, not investigated.

### Lesson for the rest of this merge
Ruff F821 only catches undefined *names* — it does NOT catch a dropped *parameter* whose
call sites still pass it by keyword into `**kwargs`-free functions (the `profile` regression),
nor a stale test whose assumptions no longer match legitimate upstream behavior changes, nor
a line-level merge splice that produces syntactically valid but semantically fused code (the
`test_cronjob_tools.py` corruption — `ast.parse`/`py_compile` both passed on it, and it wasn't
even F821-flagged; only "does the file collect" caught it). Running the real test suite after
every batch of resolutions in a Tier C/D file remains the only reliable check — plan time for
it, don't rely on static analysis alone.

## Phase 5 — Verify + deploy

- [ ] Rebuild image from merge branch; suite in-container vs Phase 2 baseline (no NEW failures)
- [ ] Boot locally with dummy env; confirm cont-init runs, dashboard denies unauth
- [ ] **CEO:** approve merge to `main`
- [ ] **CEO:** Cloud Build + Zeabur redeploy, rollback anchor to hand
- [ ] Post-deploy: gateway responds, a cron job runs, auditor reviews a PR

## Phase 6 — Make it private (AFTER the merge, separate change)

MIT permits it; `hermes-auditor` is already a collaborator on all 7 repos; no branch
protection to lose. Repo alone is theatre — the image bakes in the whole source tree.

- [ ] **CEO:** add GHCR pull credentials (PAT w/ `read:packages`) to the Zeabur service
- [ ] **CEO:** flip the **package** to private → redeploy to prove the pull still works
- [ ] **CEO:** flip the **repo** to private
- [ ] Verify auditor still opens/reviews PRs
- [ ] Note: forward-looking only — history is already public/indexed

## Deferred / not doing

- `upstream/main` HEAD (unreleased, untagged) — using the stable tag instead
- Cherry-picking 188 security commits individually — more effort than the merge
- Verifying `apps/ ui-tui/ tui_gateway/` — we run container-only (gateway + cron + dashboard)
- amd64 parity build locally (prod is amd64, Mac is arm64) — Cloud Build is the real check
