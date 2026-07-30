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

## ⛔ RESUME HERE — merge built, NOT verified (2026-07-30)

Branch `chore/upstream-merge-v2026.7.20`, **[PR #144](https://github.com/br41s/hermes-sandbox/pull/144) (DRAFT)**.
All 44 conflicts resolved; image BUILDS; dashboard lockdown suite (9 tests) passes.
**The gate run is RED with real bugs from the resolutions.** Do not merge or deploy.

### Do these in order

1. **Fix undefined names — the resolutions kept a *use* and dropped its *assignment*.**
   `py_compile` and the AST duplicate-sweep both PASS on these; only F821 catches them:
   ```
   ruff check --select F821 cron/ hermes_cli/ tools/ agent/ gateway/
   ```
   Known instances (counts = failing tests):
   - `normalized_profile` (166) — `cron/jobs.py:1309` uses it in the job dict; **no assignment
     exists anywhere in the file**. Restore it from our pre-merge `cron/jobs.py`.
   - `mirror_enabled` (42) — `cron/scheduler.py:1631`. Defined in upstream's H4
     `mirror_delivery` setup block, which was dropped when our side of that hunk was taken.
   - `config` (67) — same class, scheduler delivery path.
   - `SKILLS_DIR` (1) — skills module.

2. **Restore `pytest-asyncio` to the image** — it is MISSING from the merged image (baseline
   has 1.3.0), which alone accounts for **321 of 419** failing files. It is still listed in the
   `dev` *extra* in `pyproject.toml`, but the Dockerfile's `uv sync` extras don't include `dev`.
   Add it to `[dependency-groups] dev` (alongside `pytest` / `pytest-timeout`, which ARE picked
   up), then `uv lock`.

3. **Rebuild + re-run the gate WITH THE MOUNT** (see the Phase 2 note — `tests/` is
   `.dockerignore`d now), and compare the **pass count**:
   ```
   docker run --rm --entrypoint /bin/bash -v "$PWD/tests:/opt/hermes/tests:ro" \
     hermes-merged:v2026.7.20 -c 'cd /opt/hermes && scripts/run_tests.sh'
   ```
   Target: **28,720 ✓ / 55 ✗ across 18 files**. A new failing filename = regression.

### Last measured (invalid — both causes above active)
419 failing files: 321 asyncio-plugin, 13 pre-existing, **~85 real regressions**, concentrated
in `tests/cron/` — exactly the heaviest resolution area (`scheduler.py`, `jobs.py`,
`cronjob_tools.py`).

### Lesson for the rest of this merge
Run **`ruff --select F821` after every resolution**, not `py_compile`. Taking one side's
consumer while dropping the other side's producer is invisible to syntax checks, and is the
mirror image of the duplicate-definition trap that `ast.parse` caught three times.

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
