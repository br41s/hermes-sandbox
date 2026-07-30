# Upstream merge → NousResearch/hermes-agent `v2026.7.20`

**Status:** merge done on `chore/upstream-merge-v2026.7.20` (PR #144, DRAFT). All 44
conflicts resolved, image builds, CLI works. Gate: **44,028 passed / 121 failed across 31
files — every one classified, none a live regression.** 9 real regressions were found and
fixed. Remaining work is Phase 1 (Cloud Build proof) + Phase 5 (CEO review & deploy).
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

## Phase 1 — Rollback insurance ✅ DONE (PR #142, merged `db9493234`)

- [x] Capture current prod digest as rollback anchor
- [x] `cloudbuild.yaml`: immutable `:sha-<commit>` tag alongside `:latest`, pushed FIRST
- [x] Fail-loud guard if `_COMMIT_SHA` missing (no `:sha-unknown` placeholder)
- [x] Proven live: `:latest` and `:sha-db9493234` both resolve to
      `sha256:bfc2dffdeecd524f57e1ac3c985646be9d73f56d4e16af38b1ba321e4c3128fa`
- [x] `cloudbuild.yaml` is byte-identical on this branch — the merge did not touch it

Deploy command (the `_COMMIT_SHA` substitution is REQUIRED — the build aborts without it):
```bash
gcloud builds submit --substitutions=_COMMIT_SHA="$(git rev-parse --short HEAD)",_GITHUB_TOKEN=<token>
```

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
> ⚠️ **SUPERSEDED — the tests-only mount shown here is no longer enough.** `tests/` was
> just the first of several excluded dirs the suite reads, and the root `HOME` matters
> too. Use the full command in **RESUME HERE** below; it clears ~16 files of phantom
> failures this one leaves in.
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
- [x] cron profile-scoped delivery routing + fail-closed profile resolution — VERIFIED:
      `tests/cron/` is 789 passed and `tests/tools/test_cronjob_tools.py` 88 passed. Both
      needed repair first (the `profile` plumbing was dropped wholesale; test_cronjob_tools
      had a merge-splice fusing two classes) — see the fix list below.
- [x] `container_boot` GITHUB_TOKEN / git-cred reconciliation + auditor identity tripwires —
      VERIFIED: `tests/hermes_cli/test_container_boot.py` 58 passed (one helper needed
      un-splicing first). `tests/cron/test_cron_profile.py` KEPT (upstream deleted it) and
      is green.

## Phase 4 — The merge

Branch: `chore/upstream-merge-v2026.7.20`

- [x] **Tier A** take upstream blind — 13× `web/src/i18n/*`, `website/docs/…/cron.md`, `model-catalog.json`
- [x] **Tier B** mechanical — `.env.example .gitignore Dockerfile pyproject.toml hermes_constants.py models.py service_manager.py`; **regenerate** `uv.lock`, don't merge it
- [x] **Tier C** careful, our logic — `cron/scheduler.py` `cron/jobs.py` `tools/cronjob_tools.py` `hermes_cli/cron.py` `container_boot.py` `skills_hub.py` `skills_tool.py` `skill_manager_tool.py` `approval.py` `auxiliary_client.py` telegram adapter, `image_gen/openrouter` (add/add)
- [x] **Tier D** re-apply intent onto upstream's rewritten code (CEO decision — do NOT hunk-resolve):
  - [x] `hermes_cli/web_server.py` (upstream +16,446/−4,756 over 312 commits)
  - [x] `hermes_cli/main.py` (+5,872/−5,540)
  - [x] `hermes_cli/dashboard_auth/middleware.py` — **security-critical**
  - [x] `hermes_cli/dashboard_auth/public_paths.py` — **RESOLUTION ALREADY DETERMINED (verified
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
- [x] Decide `tests/cron/test_cron_profile.py` (deleted upstream, modified by us) — KEPT
      ours; it still guards per-job profile routing and is green.
- [x] `public_paths.py` union VERIFIED in the built image: exactly 8 entries, both
      `/api/delegate` (ours) and `/api/cron/fire` (upstream) present, and
      `tests/test_dashboard_lockdown_regression.py` passes 9/9 — so the
      unauthenticated surface is exactly what was designed, not widened.
- [ ] **CEO:** review Tier D diffs, dashboard auth especially

## ⛔ RESUME HERE — triage COMPLETE; ready for CEO review + deploy (2026-07-30)

Branch `chore/upstream-merge-v2026.7.20`, **[PR #144](https://github.com/br41s/hermes-sandbox/pull/144) (DRAFT)**, pushed.
All 44 conflicts resolved; image builds; CLI works; **all 31 remaining failing files are
classified and none is a live regression** (14 pre-merge baseline, 15 container/root/network
artifacts of running the suite inside the prod image, 1 upstream's own bug, 1 flaky).

**Gate progression:** 83 failing files → 47 → 35 → 31, **zero newly-broken files at any
step**. Final: **2,151 files, 44,028 passed, 121 failed.**

Next: Phase 1 (commit `cloudbuild.yaml`, prove the `:sha-` tag) → Phase 5 (CEO reviews the
Tier D diffs, then Cloud Build + Zeabur redeploy with the rollback anchor to hand).

### THE GATE COMMAND (changed twice — use exactly this)

```bash
docker build -t hermes-merged:v2026.7.20-final -f Dockerfile .
docker run --rm --entrypoint /bin/bash \
  -v "$PWD/tests:/opt/hermes/tests:ro"   -v "$PWD/docs:/opt/hermes/docs:ro" \
  -v "$PWD/website:/opt/hermes/website:ro" -v "$PWD/acp_registry:/opt/hermes/acp_registry:ro" \
  -v "$PWD/assets:/opt/hermes/assets:ro" -v "$PWD/.github:/opt/hermes/.github:ro" \
  -v "$PWD/.gitignore:/opt/hermes/.gitignore:ro" \
  hermes-merged:v2026.7.20-final \
  -c 'mkdir -p /home/tester && export HOME=/home/tester && cd /opt/hermes && scripts/run_tests.sh'
```

Two harness requirements, both discovered the hard way — **without them the gate
invents ~16 files of failures that are not bugs**:

1. **Mount every dir upstream's `.dockerignore` excludes** but tests read:
   `tests/ docs/ website/ acp_registry/ assets/ .github .gitignore`. Cleared 7 files
   (`test_extract_skills`, `test_generate_skill_docs`, `test_blueprint_catalog`,
   `test_xurl_article_ingestion_docs`, `test_contract_doc_conformance`,
   `test_registry_manifest`, `test_lint_config` — the last was even on the pre-merge
   baseline-18, so the mounted gate is **stricter** than the old one).
2. **`HOME` must have ≥2 path components.** The image runs as root (`HOME=/root`), and
   `approval.py`'s `_home_prefix_fold_regex` deliberately refuses to fold a
   single-component home (anti-clobber guard: `/home/alice` folds, `/home` does not).
   As root, absolute-path writes to `/root/.bashrc` and `/root/.ssh/authorized_keys`
   are NOT flagged dangerous. Setting `HOME=/home/tester` took `test_approval.py`
   from 3 failed to **312 passed**, and also cleared `test_lazy_deps_durable_target`
   (root can write "read-only" dirs).
   > **Follow-up, not a merge regression (upstream design):** if any agent actually
   > runs as root in prod, that guard has a hole — `~/…` and `$HOME/…` forms are still
   > caught, only the literal `/root/…` form slips. Worth a separate look.

ALWAYS compare **filenames**, never raw counts: the corpus grew ~50% (28,917 →
44,149 tests) vs the pre-merge baseline, so totals are not comparable.

### Real regressions found and fixed this session (9)

Every one was invisible to ruff/`py_compile` — all are semantically-fused or
half-dropped merge output that parses fine.

1. **`hermes` CLI was completely broken** (`fix(cli): drop duplicate cron parser`) —
   the merge kept BOTH our inline `cron` parser block in `main.py` and upstream's
   extracted `build_cron_parser()` call, so argparse raised
   `conflicting subparser: cron` on import; even `hermes --help` exited non-zero.
   173 mentions in the gate log; `argparse.ArgumentError` was the #1 signature.
   Our block had also been spliced into the middle of upstream's *status* section.
   Fixed by making upstream's module the single source and porting the five things
   it lacked: `create --profile`, `edit --profile`, `edit --prompt-source`, the
   `edit --progress-ping/--no-progress-ping` group, and `sync-prompt` (#113).
   Kept upstream's new `runs`/`history`. `hermes_cli/cron.py` already dispatched
   all of them — only the parser layer was duplicated.
2. **`tests/conftest.py` splice** (`test(conftest): un-splice pytest_configure`) —
   upstream's Windows `--timeout-method` fallback was fused onto the tail of our
   `_lazy_install_guard` fixture, where `config` is out of scope. The autouse
   fixture raised `NameError: name 'config' is not defined` on teardown: **70
   occurrences across many otherwise-healthy files.**
3. **`.dockerignore` dropped a live cron prompt** (`fix(docker): keep the infographic
   cron prompt`) — upstream excludes `infographic/` as README assets; **we have a
   directory of the same name** holding `infographic-engineer.prompt`. The file
   auto-merged (never conflicted), so it silently vanished from the image.
   `incidents.sweep` resolves `prompt_source` against `/opt/hermes`, so the
   prompt-drift watcher would post a false "prompt source missing" incident to the
   alert thread **every sweep**, and `hermes cron sync-prompt` would fail for that job.
   No test could catch it — `tests/` is excluded from the image too. Re-included via
   `!infographic/*.prompt`; verified the dir ships exactly one file. The other six
   `.prompt` dirs (gap-hunter, onsite-seo, offsite-geo, product-articles,
   onboarding-content, auditor) hit no upstream exclusion.
4. **Skill tools lost dynamic profile resolution** (`fix(skills): restore dynamic
   profile resolution`) — **the regression that once looped the biglobster SEO cron
   on "skill not found in active profile 'default'"** ([[hermes-profile-cron-skill-resolution]]).
   The merge took upstream's `_skills_dir()` (module-level `SKILLS_DIR` frozen at
   import) but dropped our PEP 562 `__getattr__` in `skills_tool.py` and
   `skill_manager_tool.py`; `skills_hub.py`/`skills_sync.py` kept theirs, so only
   two of four regressed. Worse, upstream's `Path(SKILLS_DIR)` body survived while
   our design never assigns that name, so **every** `_skills_dir()` call raised
   `NameError` — `skill_view` and `skill_manage(action="edit")` returned
   `success=False` for every profile. Restored our trio in both.
   Gotcha now documented in the hook: **PEP 562 `__getattr__` fires only for
   attribute access on the module object, never for a global lookup inside the
   module's own functions.**
5. **Cron cwd writer-lock leak** (`fix(cron): drop pre-lock TERMINAL_CWD block`) —
   the merge kept both placements of the workdir env override. The stale copy runs
   *before* `_terminal_cwd_lock.acquire_write()` and outside the protective `try`,
   re-introducing the exact deadlock the regression test guards: an exception in
   that window leaks the writer and **every later cron job blocks forever**. It also
   ran before the `_prior_terminal_cwd` snapshot, so the `finally` restored
   `TERMINAL_CWD` to the job's own workdir — a cross-job cwd leak of the same class
   the isolated-checkout work closed. `tests/cron/` 786→**789 passed**.
6. **Dashboard auth: provider cookie skipped on the cache path**
   (`fix(dashboard-auth): migrate provider cookie on the verified-cache path`) —
   our `_VERIFIED_CACHE` fast path returns before upstream's new legacy-session
   migration, so a cached token never got the provider-hint cookie. The cache must
   skip the JWKS fetch, not change the response. Extracted `_migrate_provider_cookie()`
   and called it from both paths.
7. **`container_boot` test helper clobbered its own fixture**
   (`fix(container-boot): stop the test helper clobbering involuntary_exit`) —
   `_make_profile` kept both sides' `gateway_state.json` writes; upstream's second
   write dropped `involuntary_exit`, so the involuntary-SIGTERM autostart test saw a
   deliberate stop. **Production code was never wrong.** Also removed a duplicate
   `_AUTOSTART_STATES` (identical value, both comment blocks fused).
8. **`model-catalog.json` was stale** (`chore(models): regenerate model-catalog.json`) —
   it is **generated** from `_PROVIDER_MODELS`/`OPENROUTER_MODELS`, the same class as
   `uv.lock` which the plan already said to regenerate. Tier A took upstream's copy
   blind, so it described upstream's lists (`tencent/hy3` lost its `recommended` mark).
   Regenerate with `python scripts/build_model_catalog.py`.

### CEO decision taken (2026-07-30)
- **`plugins/image_gen/openrouter` add/add → KEEP OURS, DEFER.** We wrote that provider
  ourselves (7 commits, added for the Zeabur HF-egress block); upstream independently
  wrote `OpenRouterCompatImageProvider` (526 lines, model chains, aspect ratios,
  reference images, plus a second `nous` provider). Upstream's 37-test file rode in
  with the merge and fails wholesale on imports. **Left as a documented known-red
  bucket; revisit after the merge is deployed and stable.** If it is ever taken,
  upstream honours `OPENROUTER_IMAGE_MODEL`, so our grok pin becomes config not code —
  but that env var MUST be set on Zeabur first or BigLobster images silently switch
  to `openai/gpt-5.4-image-2`.

### Remaining failures — ALL 31 CLASSIFIED (2026-07-30)

Nothing is unexplained. One was a real regression (fixed, last entry below); everything else
is pre-merge baseline, a container/root artifact of running the suite inside the prod image,
upstream's own breakage, or flake. **14 + 6 + 3 + 3 + 1 + 1 + 1 + 1 + 1 = 31.**

**1 — FLAKY, not a regression.** `tools/test_local_interrupt_cleanup` appears and disappears
between otherwise-identical gate runs (present in run 5, absent in run 6, present in run 7).
Passes 3/3 in isolation; it is timing-sensitive under the 20-worker parallel load. Ignore a
single occurrence; only investigate if it becomes consistent.

**1 — CEO-deferred:** `plugins/image_gen/test_openrouter_compat_provider` (see above).

**1 — environmental, unfixable in-container:** `test_setup_temporary_outputs` imports
`setuptools`, which the lean prod image does not ship.

**14 pre-merge baseline** — already red before the merge, not regressions:
`gateway/test_restart_drain` `gateway/test_restart_notification` `hermes_cli/test_cmd_update`
`hermes_cli/test_gateway` `hermes_cli/test_gateway_service` `hermes_cli/test_gateway_wsl`
`hermes_cli/test_startup_plugin_gating` `hermes_cli/test_update_yes_flag`
`plugins/image_gen/test_huggingface_provider` `test_live_system_guard_self_test`
`test_run_tests_parallel` `tools/test_mcp_stability` `tools/test_voice_mode`
`tools/test_windows_native_support`

**5 baseline files are now GREEN** — the merge is a net improvement on the suite:
`test_biglobster_site_checkouts` `test_lint_config` `tools/test_local_background_child_hang`
`tools/test_web_providers` `tools/test_web_tools_config`

**6 — "this is a docker install, so `hermes update` is inert" (BY DESIGN).**
`banner.py:325` returns `None` when `detect_install_method() == "docker"`, and
`web_server.py` short-circuits `/api/status`'s `can_update_hermes` the same way. The image
bakes a `docker` stamp into `/opt/hermes/.install_method`, so every update test no-ops.
`gateway/test_update_command` `gateway/test_update_streaming` `hermes_cli/test_update_check`
`hermes_cli/test_update_concurrent_quarantine` `hermes_cli/test_pip_install_detection`
`hermes_cli/test_web_server`
PROVEN: mount a file containing `git` over `/opt/hermes/.install_method` and all six pass
(plus the three baseline update files drop from ~48 failures to 29).
```bash
printf 'git' > /tmp/install_method_git
docker run ... -v /tmp/install_method_git:/opt/hermes/.install_method:ro ...
```

**3 — root-user artifacts.** The suite runs as root, which ignores the permissions these
tests rely on: `hermes_cli/test_migrate_xai` (reads an "unreadable" config),
`tools/test_search_error_guard` (`chmod 000` still readable, so the "Permission denied"
diagnostic never appears), `tools/test_lazy_deps_durable_target` (writes a read-only dir).
PROVEN: all pass under `--user 10000:10000` (the image's `hermes` user) with
`--tmpfs /opt/hermes/.pytest-cache --tmpfs /opt/hermes/.pytest_cache`.

**3 — container-shape artifacts** (the test's premise is a host install):
- `agent/test_copilot_acp_client` — `hermes_constants.get_subprocess_home()` line 818:
  `if profile_home and is_container(): return profile_home`. In a container HOME is
  deliberately the profile home, so "preserves real HOME" cannot hold. Documented behaviour
  of `terminal.home_mode: auto`.
- `gateway/test_restart_service_detection` — expects the detached path when no external
  supervisor marker is set, but the image genuinely ships s6 (`/etc/s6-overlay`), so
  `via_service=True` is CORRECT here. Related to [[hermes-orphan-gateway-rootfix]].
- `gateway/test_whatsapp_bridge_pidfile` — needs to kill a real stale listener; the
  `_live_system_guard` autouse fixture blocks real `os.kill`/subprocess and this upstream
  test carries no bypass marker. The guard exists on BOTH sides, so not a divergence.

**1 — network guard.** `hermes_cli/test_auth_provider_gate::test_provider_not_in_registry_but_in_models_dev`
is a NEW upstream test needing the live models.dev catalog. Offline, `get_provider("openrouter")`
degrades to a stub with EMPTY `api_key_env_vars`, so the env-var check finds nothing.
Verified: identical call returns True with network, False under `--network none`.

**1 — UPSTREAM'S OWN BUG, red in upstream too, nothing for us to fix.**
`hermes_cli/test_opencode_go_validation_fallback::test_opencode_zen_known_model_accepted`
asserts `kimi-k2` is in the opencode-zen catalog. Upstream v2026.7.20 RETIRED that id
(its catalog now has `kimi-k2.5`, `kimi-k2.6`, `kimi-k2.7-code`) but never updated the
test. Confirmed directly against the tag: catalog lacks `kimi-k2`, test still asserts it.
Our `models.py` correctly took upstream's newer catalog.

**1 — REAL REGRESSION, FIXED:** `tools/test_cron_approval_mode` — see commit
`fix(approval): close cron-deny fail-open on content-level threats` in the list above.
This is the one that mattered: **every one of our agents runs as a cron**, and
`check_all_command_guards` was returning `approved=True` for a tirith "block", and
approving on ImportError even with `security.tirith_fail_open: false`.

### Lesson (carried forward and confirmed again)
Ruff F821 catches undefined *names* only. It does NOT catch: a dropped *parameter*
still passed by keyword; a stale test whose assumptions no longer match legitimate
upstream behaviour; a line-level splice producing valid-but-fused code; a *duplicated*
definition where the later one wins; or a file silently dropped from the image by an
auto-merged `.dockerignore`. **Only running the real suite finds these — budget time
for a full gate after every batch of Tier C/D resolutions.**

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
