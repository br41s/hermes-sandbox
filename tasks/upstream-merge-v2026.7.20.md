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
- [ ] Triage `tests/test_biglobster_site_checkouts.py` — the only failure in OUR custom code

> NEVER boot the image with its real entrypoint (`/init`) + prod env — it starts a
> SECOND Telegram poller and steals updates from prod. Always `--entrypoint` override.

## Phase 3 — Pin behaviour before merging

- [ ] Characterization tests for Tier C/D so a silent regression fails loudly:
  - [ ] cron profile-scoped delivery routing + fail-closed profile resolution
  - [ ] `container_boot` GITHUB_TOKEN / git-cred reconciliation + auditor identity tripwires
  - [ ] dashboard auth gate (the cryptominer lockdown) — unauth request must 401/redirect

## Phase 4 — The merge

Branch: `chore/upstream-merge-v2026.7.20`

- [ ] **Tier A** take upstream blind — 13× `web/src/i18n/*`, `website/docs/…/cron.md`, `model-catalog.json`
- [ ] **Tier B** mechanical — `.env.example .gitignore Dockerfile pyproject.toml hermes_constants.py models.py service_manager.py`; **regenerate** `uv.lock`, don't merge it
- [ ] **Tier C** careful, our logic — `cron/scheduler.py` `cron/jobs.py` `tools/cronjob_tools.py` `hermes_cli/cron.py` `container_boot.py` `skills_hub.py` `skills_tool.py` `skill_manager_tool.py` `approval.py` `auxiliary_client.py` telegram adapter, `image_gen/openrouter` (add/add)
- [ ] **Tier D** re-apply intent onto upstream's rewritten code (CEO decision — do NOT hunk-resolve):
  - [ ] `hermes_cli/web_server.py` (upstream +16,446/−4,756 over 312 commits)
  - [ ] `hermes_cli/main.py` (+5,872/−5,540)
  - [ ] `hermes_cli/dashboard_auth/middleware.py` + `public_paths.py` — **security-critical**
- [ ] Decide `tests/cron/test_cron_profile.py` (deleted upstream, modified by us)
- [ ] **CEO:** review Tier D diffs, dashboard auth especially

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
