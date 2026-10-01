# Multiplex stage 3: retire the pre-multiplex profile machinery

Status: **plan only, no code.** Written 2026-09-29 against `main` at `2673af668`, the
commit running in production as `sha-2673af668`. Every `file:line` below refers to
that commit.

Stages 2a/2b are live: one gateway serves every profile, and bound Telegram topics
run in-process through `gateway.profile_routes`. Four pieces of machinery from
before multiplex are still carrying weight:

| Machinery | Where | What it does today |
|---|---|---|
| Fork `profile` field on default-store jobs | `cron/fork_ext/profile_scope.py:170` `_job_profile_context` | runs the job under the profile's home, scope `{**os.environ, **profile .env}` |
| Sequential lane | `cron/fork_ext/dispatch.py:47` `is_sequential` | every job with `profile` or `workdir` runs on one thread |
| `auto_profile` subprocess | `gateway/platforms/base.py:4438`, call site `:4471` | per-turn subprocess for a bound topic that no route matched |
| Multiplex opt-out | `hermes_cli/fork_ext/multiplex.py` | keeps `multiplex_profiles: false` meaning standalone (the rollback lever) |

Stage 3 moves the jobs into their profiles' own stores and then deletes the four,
one deployable step at a time.

---

## 0. What the code says that our docs don't

These change the plan, so they come first.

1. **Profile stores are ticked whether multiplex is on or off.**
   `gateway/run.py:1683` `_cron_tick_profile_homes` calls
   `profiles_to_serve(multiplex=True)` unconditionally ("One host process ticks all
   of them regardless of `gateway.multiplex_profiles`"). `CLAUDE.md` ("only
   multiplex runs those") and the `process_env_scope.py:83` docstring are stale.
   **Consequence:** once a job has moved, pulling the multiplex lever does not stop
   its store from being ticked. With `ROUTE_BOUND_TOPICS=False` its routes are gone
   and its scope still has no Telegram token: `get_secret` stays fail-closed for a
   foreign home even with multiplex off (`agent/secret_scope.py:74-90`, `:217`). So
   preflight will most likely block it as `blocked_config`, with one alert and no
   agent run (`cron/scheduler_preflight.py:318-331`). If it does run, delivery
   fails closed. Either way the multiplex rollback stops being a full rollback
   after step 1. Each moved job's rollback is to move it back.

2. **The sequential lane no longer protects anything.** Nothing writes
   `TERMINAL_CWD` any more. The workdir binds to the run's task id
   (`cron/scheduler.py:2362`, `record_session_cwd`), and cwd readers go through
   `agent/runtime_cwd.py:62` `scope_terminal_cwd`. The profile `.env` is a
   ContextVar scope (#338). `CLAUDE.md`'s "a workdir job still writes
   `os.environ["TERMINAL_CWD"]`" is stale. The lane is policy only, as
   `dispatch.py`'s own docstring already says.

3. **The lane is keyed on the fork `profile` field.** A moved job loses that field,
   so unless it has a `workdir` it drops off the lane and into that profile's
   parallel pool. Moving jobs would widen concurrency by accident. Step 0 closes
   this.

4. **A satellite-store run loses three fork guarantees:**
   - the identity assertion (`profile_scope.py:83` `_assert_own_subprocess_identity`,
     which guards against the 2026-09-12 PR-as-`hermes-auditor` class);
   - the child-process overlay (`hermes_cli/fork_ext/profile_env.py:53`
     `child_env_overlay` is gated on `_IN_PROFILE_RUN`, which only
     `_job_profile_context` sets);
   - Langfuse. `plugins/observability/langfuse/__init__.py:94` reads its keys
     through `get_secret`, the satellite scope is `.env` only, and `INJECT` does not
     carry `HERMES_LANGFUSE_*`. The plugin gets no keys and builds no client, so
     traces stop without an error.

5. **The same Langfuse gap is probably live already for bound topics.** Stage 2b
   turns run under `build_profile_secret_scope(profile home)`, and the Langfuse
   client is built once per home and cached, `_INIT_FAILED` included
   (`langfuse/__init__.py:62`, `:211-221`). **The outcome depends on what builds
   first.** A fork profile cron job has the keys (its scope includes `os.environ`),
   so if one ran first in that home since the last boot, topic turns trace fine. If
   a topic turn ran first, that home's client is disabled until restart, and so are
   its fork cron jobs' traces. Check a biglobster-topic turn since
   `sha-2673af668` via `sessionId=<topic session id>` on
   `/api/public/v2/observations` with `fields=core,basic,io`, printing the HTTP
   status. Then check a biglobster cron run from after that turn. Step 0c fixes
   both.

6. **The incident watcher only reads the default store.** `incidents/sweep.py:1232`
   calls `load_jobs()` with no store scope. Once a job moves, cron failures, silent
   stalls and prompt drift for it stop reaching thread 1904.

7. **Webhook-triggered jobs look up their id in the default store.**
   `gateway/platforms/webhook.py:562` resolves `trigger_cron_job_id` inside
   `_profile_scope(profile)`. That profile comes only from the
   `/p/{profile}/webhooks/{route_name}` URL (`:235`, resolved at `:409`), on a route
   whose `profile` matches (`:433`). A moved auditor job therefore needs new GitHub
   webhook URLs on every repo. Under a multiplex rollback, `/p/<profile>/` answers
   404 (`:416-424`), so the webhooks die and the job falls back to its 6-hourly
   poll. **A pause does not stop a `trigger_cron_job_id` run.**
   `dispatch_job_async` (`cron/fork_ext/dispatch.py:120`) calls `run_one_job`
   directly, and neither it nor `_run_one_job_body` (`scheduler.py:2776`, `:3205`)
   checks enabled/paused. Only the tick's `claim_job_for_fire` does
   (`jobs.py:2710`).

8. **`hermes cron remove` cannot be one half of a move.** `cron/jobs.py:2262`
   `remove_job` deletes `cron/output/<id>/` and clears the job's notepad. Notepad,
   executions and output are all per home (`cron/notepad.py:33`,
   `cron/executions.py:47`, `cron/jobs.py:411`).

9. **New rentals are still created in the old shape.**
   `scripts/provision_bl_client.py:671` calls `create_job(..., profile=canon)` in
   the default store, and so does the webhook path through it
   (`hermes_cli/bl_rental_webhook.py`).

10. **A route authorizes both directions.** `profile_routes` decides inbound routing
    and satellite outbound delivery with the same matcher
    (`gateway/profile_routing.py:74`, used by `cron/scheduler_preflight.py:211`). A
    route that lets profile X deliver to General would also send every General
    message to X. Section 2.3 builds on this.

11. **Moving rentals enforces `TENANT_EXCLUDE` for the first time.** The fork scope
    merges `os.environ` under the profile `.env`, so a rental job today resolves
    BigLobster's `EXA_API_KEY` and `HUGGINGFACE_API_KEY`
    (`profile_scope.py:250-256` says so deliberately). A satellite rental scope does
    not. That is the policy in `boot_reconcile.py:66-86`, but it is a behaviour
    change, so check before moving (section 3).

12. **Kickoff pings stop for every moved job, not only General ones.** Two reasons:
    - `send_to_targets` (`cron/fork_ext/kickoff.py:66`) calls
      `resolve_delivery_transport`, which does a target-less `adapters.get(platform)`
      (`gateway/delivery.py:64`), and `SharedRouteAdapters.get` answers a target-less
      call with a miss (`scheduler_preflight.py:212`).
    - The ping is sent at `scheduler.py:3235`, before the run's secret scope is
      installed at `:3257`. A job without the fork `profile` field reads its home
      target through `get_secret` with no scope, which raises
      `UnscopedSecretError` under multiplex (`secret_scope.py:218`).

    The second reason may already break kickoff for default-store non-profile
    Telegram jobs. `grep "kickoff ping raised"` across the logs listed in section 6.

---

## 1. Inventory: every fork `profile` job

You run these; I cannot reach production. All of them are read-only and print no
values from `.env` or the environment. Run them as `hermes`, never root: root
mutations have flipped `jobs.json` ownership before (memory: cron isolated
checkout). Every command below uses the same prefix as the
`process_env_scope --check` probe. It is written out in full each time, because
zsh does not word-split an unquoted `$VAR`.

**a. Human-readable.** The fork adds a `Profile` row to `cron list`
(`cron/fork_ext/cli.py:56`):

```bash
zeabur service exec --id 6a5ea5074d439e41ee4cd38c -i=false -- /usr/bin/env PATH=/command:/usr/local/bin:/usr/bin:/bin /command/with-contenv s6-setuidgid hermes env PYTHONPATH=/opt/hermes /opt/hermes/.venv/bin/hermes cron list --all
```

**b. One line per job, prompt excluded.** A PAT once sat in a cron prompt, so
prompts are never printed. This covers all jobs, not only profile jobs, so that
`context_from` references in both directions show up:

```bash
zeabur service exec --id 6a5ea5074d439e41ee4cd38c -i=false -- /usr/bin/env PATH=/command:/usr/local/bin:/usr/bin:/bin /command/with-contenv s6-setuidgid hermes env PYTHONPATH=/opt/hermes /opt/hermes/.venv/bin/python -c 'import json;d=json.load(open("/opt/data/cron/jobs.json"));J=d.get("jobs",d) if isinstance(d,dict) else d;K=("id","profile","enabled","state","name","schedule_display","next_run_at","workdir","deliver","failure_deliver","context_from","no_agent","script","prompt_source");[print("|".join(str(j.get(k,"")) for k in K)) for j in J]'
```

**c. Webhook triggers.** Print only the three keys, never the route: routes carry
HMAC secrets.

```bash
zeabur service exec --id 6a5ea5074d439e41ee4cd38c -i=false -- /usr/bin/env PATH=/command:/usr/local/bin:/usr/bin:/bin /command/with-contenv s6-setuidgid hermes env PYTHONPATH=/opt/hermes /opt/hermes/.venv/bin/python -c 'import json;s=json.load(open("/opt/data/webhook_subscriptions.json"));[print(n,r.get("trigger_cron_job_id"),r.get("cron_job"),r.get("profile","default")) for n,r in s.items() if r.get("trigger_cron_job_id") or r.get("cron_job")]'
zeabur service exec --id 6a5ea5074d439e41ee4cd38c -i=false -- grep -n "trigger_cron_job_id\|cron_job:" /opt/data/config.yaml
```

**d. Where a fork profile job's per-home state lives.** This is resolved from code,
so no command is needed.
- **Executions** are in the **default** home's `cron/executions.db`.
  `create_execution` runs in `_submit_with_guard` (`scheduler.py:4168`) and
  `run_one_job` (`:2789`) under the tick's default-home scope, before
  `_job_profile_context` is entered.
- **Output** is in the **default** store's `cron/output/<id>/`, because the
  `use_cron_store` override wins (`jobs.py:153-167`).
- **Notepad** is read at prompt build (`cron/scheduler_prompt.py:292`) inside the
  profile context through `get_hermes_home()`. The notepad the job actually uses
  is therefore **already** `profiles/<p>/cron/notepad.db`.

To confirm on the volume, which is read-only:

```bash
zeabur service exec --id 6a5ea5074d439e41ee4cd38c -i=false -- ls -la /opt/data/profiles/biglobster/cron /opt/data/cron
```

Repeat for each profile in (b).

**Classify each profile job** into this table. It sets the order in section 5.

| Column | Values | Why it matters |
|---|---|---|
| owner | BigLobster-owned (`biglobster`, `finview`, `grow-shop`, `socialagenda`, `bl-site-package`), `auditor`, rental | rentals write to client sites; the auditor gates merges |
| workdir | yes/no | a workdir job stays on the lane regardless (`is_sequential`) |
| deliver lane | `local` / own topic / General / explicit other target | section 2.3 |
| failure_deliver | same classes | same adapter path as `deliver` |
| kickoff ping | on/off | breaks for every moved job until step 0g (fact 12) |
| webhook-triggered | route name(s) | fact 7 |
| context_from | ids in and out | output is per store: a reference across stores finds nothing |
| next_run cadence | interval / cron / once | section 2.2: intervals keep their phase only if `next_run_at` is copied |

How a bare `deliver: telegram` resolves today, for the "deliver lane" column: the
fork reads the job's own profile `.env` first (`cron/scheduler_delivery.py:465`
and `:497`), which `routing.env` seeds. The bundled profiles resolve to their own
topics:

| Profile | Thread (`docker/profiles/*/routing.env`) |
|---|---|
| biglobster | 2 |
| grow-shop | 3 |
| finview | 61 |
| socialagenda | 1986 |
| bl-site-package | 3686 |
| auditor | 1904 (shared with the incident watcher) |

**Rentals have no `routing.env`**, so today a rental with bare `deliver: telegram`
falls back to the global home, which is General. After a move it would have **no
target at all**: the satellite scope does not see Zeabur's
`TELEGRAM_HOME_CHANNEL`, so delivery errors instead of falling back. Provisioning
defaults to `local` (`provision_bl_client.py:704`), but check each rental row.

---

## 2. Migration mechanics

### 2.1 How the stores run

- **One tick per store.** The multiplex ticker walks every served home
  (`cron/scheduler_provider.py:624-647`). Each tick runs inside
  `_profile_cron_scope(home)`, which sets the home override and `use_cron_store`,
  and takes that store's own `cron/.tick.lock` (`cron/scheduler_tick.py:33`).
- **Advance, then claim.** The tick calls `advance_next_runs` for every due job
  *before* dispatch (`scheduler_tick.py:83`, `jobs.py:2645`), so recurring jobs are
  at-most-once: a crash mid-run skips a slot rather than re-firing it.
  `mark_job_run` re-arms from completion. External fires go through
  `claim_job_for_fire` (`jobs.py:2694`), which stamps `fire_claim` and advances
  `next_run_at` under the fire fence.
- **No dedup across stores.** The in-flight guard key carries the home
  (`scheduler.py:774`). The fire fence is keyed `cron_dir::id` (`jobs.py:333`).
  The fork's `_job_run_lock` lives under `_get_hermes_home()/cron`
  (`cron/fork_ext/run_guard.py:55`), and that resolves to the profile home under
  multiplex. **The same id in two stores is two independent jobs.** The invariant
  a move must hold is: *at every instant, at most one runnable record per id across
  all stores.*
- **Delivery for a satellite job.** `tick_adapters_for` (`scheduler_provider.py:580`)
  hands a credentialless profile `SharedRouteAdapters(primary adapters, routes)`.
  `SharedRouteAdapters.get` (`scheduler_preflight.py:211`) returns the primary
  Telegram adapter only when an enabled route for this profile declares a
  `chat_id` or `thread_id` and `ProfileRoute.matches` accepts the exact target.
  Anything else is a miss, and `_resolve_target_transport`
  (`scheduler_delivery.py:1368`) then reports "not configured/enabled". The job
  runs; its delivery fails closed. The home target comes from the satellite's own
  scope (`get_secret` under `run_one_job`'s scope, `scheduler.py:3257`), so bare
  `deliver: telegram` resolves to the profile's `routing.env` thread, which the
  `fork-topic:<p>:<thread>` route matches (`boot_reconcile.py:428`).

### 2.2 Moving one job

No move tool exists upstream, and `hermes cron remove` + `create` is wrong three
ways:
- `create_job` mints a new id, which breaks `hermes cron runs`, Langfuse session
  ids, `context_from` and every doc that names the job;
- it recomputes `next_run_at` from now, which shifts an interval job's phase;
- `remove_job` deletes the notepad and output (fact 8).

Step 0f adds a fork subcommand, `hermes cron move <id> --to-profile <p>` /
`--to-default`. It is a dry run unless `--apply` is given, lives in
`cron/fork_ext/cli.py` `SUBCOMMANDS` next to `sync-prompt`, and runs as `hermes`.
Ordering:

1. **Refuse** unless all of these hold:
   - the job is not in flight: no live `fire_claim` and no `claimed` or `running`
     row in the source home's `executions.db`. The running-job registry lives in
     the gateway's memory and a CLI cannot read it;
   - `next_run_at` is at least 10 minutes ahead;
   - the id is absent from the target store;
   - every `context_from` edge moves in the same call;
   - the job is not a webhook trigger, or `--webhook-route-disabled` is passed
     (see the auditor cohort).
2. **Pause the source** under the default store's jobs lock, with
   `paused_reason="moving to <p>"`. From here the source cannot be ticked.
3. **Write the target record** under `use_cron_store(profile home)` and that
   store's jobs lock (the fork's `jobs_backup` snapshots it first). It is a deep
   copy with the same `id`, `next_run_at`, `last_run_at`, `last_status`, `repeat`,
   `prompt_source` and paused state as before step 2. It drops `profile`,
   `fire_claim` and `pending_slot`.
4. **Copy per-home state** for that id. Copy, don't move: the source copy is the
   rollback.
   - **Execution rows, always.** `completed_occurrence` (`cron/occurrences.py:21`)
     proves a slot is done from the current home's `executions.db`. Without the
     rows, the target has no proof for the last slot, and catch-up can re-fire it.
   - **`cron/output/<id>/`**, for `context_from` and last output.
   - **Notepad rows,** only if they are not already in the target (1d says they
     should be).
5. **Delete the source record** with a direct `save_jobs(removed_ids={id})`,
   never `remove_job`.

| Crash point | What you find | Recovery |
|---|---|---|
| between 2 and 3 | source paused, target absent: the job skips slots, visibly (`cron list` shows the pause reason) | re-run the tool; it finishes the move |
| between 3 and 5 | both records exist, the source paused, so there is no double run | re-run the tool. Until then, run neither copy by hand: `hermes cron run` resumes a paused job (`trigger_job`, `jobs.py:2166`) and so does the dashboard's fire (`force=True`, `jobs.py:2745`), and either would leave two runnable records |

**Skipped slots:** with the 10-minute margin the target's `next_run_at` is in the
future, so the next slot fires once, from the new store. If the margin is ever
overridden and the slot passes mid-move, the target's first tick sees a past
`next_run_at`. The due scan's grace and catch-up policy then decides to fire it
once or log the skip: the same path `resume_job` relies on (`jobs.py:2126`, #113603).
It is never a silent re-anchor.

**Webhook caveat (verified):** a pause does not stop a `trigger_cron_job_id` run
(fact 7). For a webhook-triggered job, disabling the route (`enabled: false`,
checked at `webhook.py:602`) for the move window is **mandatory**.

**After a move, every per-job CLI command needs `-p <p>`**: `cron runs`, `run`,
`pause`, `resume`, and `sync-prompt` (`cron/fork_ext/cli.py:98-110` goes through
the CLI's own store). A bare `hermes cron sync-prompt <id>` answers "not found".
`--prompt-source` stays cwd-relative. Update the runbooks and memory entries that
use the bare form in the same cohort.

### 2.3 A profile job that delivers to General

After the move, General delivery fails closed: there is no route mapping General
to that profile, and `SharedRouteAdapters` never falls back to the default bot.
The run succeeds. The delivery is logged as a delivery error, and the CEO never
sees the output. `failure_deliver` fails the same way. The kickoff ping fails for
every moved job regardless (fact 12).

| Option | Verdict |
|---|---|
| **A. Retarget to the profile's own topic** (`deliver: telegram`, bare). No code, delivery stays fail-closed, and the output lands where that profile's conversation already lives. | **Decided, for every such job.** |
| B. Keep it a default-profile job | **Rejected.** It would run the job with the default profile's keys. |
| C. Add a route General → profile | **Rejected.** Routes are inbound too (fact 10): every General message would go to that profile. |
| D. A fork outbound-only grant list in `SharedRouteAdapters` | **Rejected for now.** It is a new pattern, it widens a fail-closed boundary, and it patches an upstream file. Revisit only if A and B leave a job with no home. |

**Decided 2026-09-29: A.** Every profile job that posts to General is retargeted
to its own profile's topic as part of its move. The inventory lists which jobs
that covers. Retargeting a rental needs a topic first: rentals have no
`routing.env` (section 1).

---

## 3. Secrets: fork scope vs satellite scope

| | Fork `profile` job (today) | Satellite-store job |
|---|---|---|
| In-process `get_secret` | `{**os.environ, **profile .env}` (`profile_scope.py:255`) | `build_profile_secret_scope(home)`: `.env` + external sources (`agent/secret_scope.py:372`). Under multiplex a miss returns the default value and never reads `os.environ` (`:217`). |
| `HERMES_CRON_*` tuning, run-time reads (`HERMES_CRON_TIMEOUT`) | read from that scope, so the service env counts | `cron_env_setting` (`cron/env_settings.py:19`) reads the scope only. A key absent from the profile `.env` means the default applies. |
| `HERMES_CRON_*` tuning, tick-loop reads (`HERMES_CRON_MAX_PARALLEL`) | no scope in the tick loop: read from `<home>/.env` on disk, in **every** store, default included | same. A value set only in the Zeabur env is already ignored for pool sizing today. |
| Terminal/script children | launch `.env` residue stripped (`tools/environments/local.py:433`, keyed off the home override), credentials scrubbed, **plus** `child_env_overlay` (scope keys that differ from `os.environ`) | the same, **without** the overlay: `_IN_PROFILE_RUN` is unset. |

The profile `.env` already receives `INJECT` (`boot_reconcile.py:44`):
`OPENROUTER_API_KEY`, `HERMES_CALLBACK_SECRET`/`_URL`, `HERMES_MAX_ITERATIONS`,
`EXA_API_KEY`, `HUGGINGFACE_API_KEY`, `GITHUB_TOKEN`, `GH_TOKEN`,
`AUXILIARY_VISION_MODEL`, `GSC_SERVICE_ACCOUNT_B64` and `PEXELS_API_KEY`. The
exceptions: rentals are minus `TENANT_EXCLUDE`, and the auditor is minus
`GITHUB_TOKEN`/`GH_TOKEN`, which are then pinned to `HERMES_AUDITOR_GITHUB_TOKEN`'s
value, plus its model knobs and dedicated OpenRouter key. Every profile also gets
its `routing.env` keys.

**What each class would lose**, from code. The probe in step 0e gives the
definitive per-profile list from the live env, names only.

| Key(s) | Lost by | Effect | Close by |
|---|---|---|---|
| `HERMES_LANGFUSE_PUBLIC_KEY` / `_SECRET_KEY`, the base URL (+ `_ENV`, `_RELEASE`, `_SAMPLE_RATE` if set) | every profile | no traces, no error (fact 4). Langfuse is our source of truth for agent behaviour | BigLobster-owned profiles: `INJECT`, with BigLobster's two keys also in `TENANT_EXCLUDE`. Rentals: their own project, pinned at boot (see "Rental Langfuse" below). |
| `HERMES_CRON_TIMEOUT` and other run-time `HERMES_CRON_*` **if set only in the service env** | every profile | the watchdog reverts to its default | `INJECT` them. (`HERMES_CRON_MAX_PARALLEL` is lost already, everywhere; if it matters, pin `cron.max_parallel_jobs` via `OVERRIDES`.) |
| Auditor identity (`GITHUB_TOKEN`/`GH_TOKEN` = bot) | nobody | already in the auditor `.env` (`boot_reconcile.py:315`) | nothing. `HERMES_AUDITOR_GITHUB_TOKEN` by name is read only by `boot_reconcile.py` |
| `HERMES_AUDITOR_JUDGE_*` knobs | auditor, if they are set in the service env | `auditor/llm.py` reads them through `_env_value` (`os.environ`, then `$HERMES_HOME/.env`) inside a **child** process, so the answer depends on the child-env row above. The probe must check the auditor's child env specifically. | stamp them into the auditor `.env` next to `HERMES_AUDITOR_SYSTEM_MODEL` (`boot_reconcile.py:337`) |
| `EXA_API_KEY`, `HUGGINGFACE_API_KEY` | rentals | intended (fact 11). Before moving a rental, check its recent Langfuse runs for `web_search` via Exa or HF tools | nothing: policy |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`, `TELEGRAM_GROUP_ALLOWED_CHATS` | every profile | **correct.** Today every fork profile job's scope holds them via `os.environ`; moving is a least-privilege gain | must stay absent. Never `INJECT` them (`CLAUDE.md`: a satellite holding the token is a fatal duplicate credential). Delivery rides the primary adapter via routes. |
| `SHORTS_STUDIO_GITHUB_TOKEN`, `YOUTUBE_*`, `META_*` | every profile | correct: shorts jobs have no profile and stay in the default store | must stay absent (`CLAUDE.md`, "Do not add them to `INJECT`") |

**Why `INJECT` and not a scope fallback:** `INJECT` is the existing pattern. It
survives rotation on every boot, which is the reason it exists (the 2026-06-05
incident), and it keeps a profile's secrets inspectable in one file. A per-profile
`os.environ` fallback would rebuild the fork scope under another name.

**The guard is a test, not a comment.** Step 0c adds a new assertion in
`tests/test_biglobster_github_token_propagation.py`, where `INJECT` is already
tested. `INJECT` never contains any of these:
- `TELEGRAM_BOT_TOKEN`, or any `TELEGRAM_*ALLOWED*` key;
- `SHORTS_STUDIO_GITHUB_TOKEN`, `YOUTUBE_*` or `META_*`;
- `HERMES_RENTAL_LANGFUSE_*`.

### Rental Langfuse: its own project (decided 2026-09-29)

**Finding: BigLobster's Langfuse keys are within rental jobs' reach today.** A
rental job is a fork `profile` job, and its scope is
`{**os.environ, **profile .env}` (`profile_scope.py:255`). BigLobster's
`HERMES_LANGFUSE_PUBLIC_KEY`/`_SECRET_KEY` live in the Zeabur service env, which
is where `boot_reconcile.py:679` reads them. So every rental run resolves them
through `get_secret`. A key in a client profile's reach can be read by that
client's agent. This step closes a real exposure; it is not a tidy-up.

**Decision:** rentals trace into a **separate Langfuse project with its own key
pair**, so a leak from a rental profile exposes rental traces only.

| | |
|---|---|
| Service vars | `HERMES_RENTAL_LANGFUSE_PUBLIC_KEY`, `HERMES_RENTAL_LANGFUSE_SECRET_KEY`, set in the Zeabur service env (done 2026-09-29) |
| Written as | `HERMES_LANGFUSE_PUBLIC_KEY` / `HERMES_LANGFUSE_SECRET_KEY` in each rental's `.env`: the names the plugin reads, `HERMES_`-prefixed. **Also pin the unprefixed `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` to empty.** The plugin falls back to those names when the prefixed ones are empty (`langfuse/__init__.py:255`), and the fork scope would resolve them from the service env if they exist there. The unprefixed base URL is not pinned. |
| Base URL | keeps its unprefixed name (`CLAUDE.md`, Langfuse section). Same host, different project, and not a secret, so it goes into `INJECT` for every profile. Satellites need it written somewhere, because the scope does not see `os.environ`, and the plugin's fallback is `https://cloud.langfuse.com`. Note that `CLAUDE.md`'s curl example uses `$HERMES_LANGFUSE_BASE_URL`; the 0e probe (names only) settles which name the service env actually carries. |
| Which profiles | rentals only: `is_rented_tenant` (`boot_reconcile.py:214`), i.e. `BL_SITE_URL` in the profile `.env`, which only `provision_bl_client.py` writes. **Not `INJECT`**, which reaches every profile. |
| Model | the auditor-token step (§1b of `sync_envs`, run by `docker/cont-init.d/03-biglobster-config`): resolve with `_resolve`, write with `_pin_vars` |
| Rotation | overwrite on **every boot**, never `setdefault`. A key rotated in Zeabur reaches every rental on the next boot, which closes the "rotated secrets do not reach profile `.env` files" trap (`CLAUDE.md`) for these keys. |
| Unset | fail closed: **pin both key names (prefixed and unprefixed) to empty values**, and log a names-only `WARNING`. Stripping them, as the auditor step does, would fail **open** here. A rental's scope is `{**os.environ, **profile .env}`, so a missing line resolves BigLobster's key from the service env. An empty line does not: `KEY=` parses to `""` (`agent/secret_scope.py:325-328`), a `""` from the profile `.env` overrides the service env in that merge, and `get_secret` returns it, because only `None` falls through (`:214-216`). **An empty value disables tracing:** the plugin builds no client without both keys (`langfuse/__init__.py:256`). Keep the empty pin at least until that rental moves to its own store. After the move the scope no longer contains `os.environ`, so a missing line would be safe too, but the empty pin stays harmless and is the simpler rule. |
| BigLobster's keys | in `INJECT` **and** `TENANT_EXCLUDE`, so the shared loop never writes them into a rental. The pin then replaces any stale line under the same names. |
| Logs | name and profile only. No boot line prints a value. |

**Tests** (`tests/test_biglobster_github_token_propagation.py`). Both use sentinel
values and assert on booleans, so a failure's output shows no value either:
1. **A rental never resolves BigLobster's Langfuse keys.** The test asserts on the
   **resolved scope**, not on the `.env` file.
   - **Setup:** the process env holds BigLobster sentinel keys under both the
     prefixed and the unprefixed names. The test runs `sync_envs`, then enters
     the rental's profile-run scope (`_job_profile_context`).
   - **Assertion:** `get_secret` for all four key names never returns a
     BigLobster sentinel.
   - **Cases:** (a) both key pairs set, where the prefixed names resolve to the
     rental sentinels; (b) the rental pair unset, where all four resolve to empty
     and the plugin's `_build_client` returns `None`; (c) a rental `.env` that
     already holds BigLobster's keys before the boot.
   - **Also under the satellite scope:** the same three cases run under
     `build_profile_secret_scope(rental home)`, so the test keeps guarding after
     the move.
2. **A changed service value replaces the old one in the rental `.env`.** Two
   boots with different rental values end with exactly one line per key, holding
   the new value.

**Scope of the fix, in time:**
- **Once the step deploys** with the vars set, the rental `.env` overrides the two
  names in the fork scope, so `get_secret` in a rental run returns the rental
  keys at once.
- **BigLobster's values stay in `os.environ`, and therefore in the fork scope
  under their own names, until that rental moves to its own store** (step 1,
  cohort 3). Only the satellite scope, which is `.env` only, removes them
  entirely. Rentals should therefore not wait long in the queue after this step.

---

## 4. Concurrency

- **Does `TERMINAL_CWD` still force serial runs? No** (fact 2). Other
  process-global hazards in a profile run:
  - `_job_profile_context`'s `os.environ` delta-restore backstop
    (`profile_scope.py:272`): two overlapping restores can clobber each other's
    writes. It disappears in step 2.
  - The FAL client cache: already handled by `fal_client_for_current_key`.
  - Isolated checkouts: per run, not shared (`cron/fork_ext/isolated_checkout.py`).

  Nothing left requires one thread.
- **What upstream actually does.** Parallel pools are **per profile home**
  (`scheduler.py:1176`), sized by that profile's `cron.max_parallel_jobs` or
  `HERMES_CRON_MAX_PARALLEL` (`scheduler.py:4070`). The default is unbounded.
  There is no global cap: total concurrency is the sum over profiles. The ticker
  visits profiles one after another, but each tick only submits
  (`sync=False`, `scheduler_provider.py:644`), so jobs across profiles run at once.
- **The 90-iteration budget is per run** (`agent.max_turns: 90`, pinned on main and
  every profile, `boot_reconcile.py:103`/`:128`). Widening does not change it.
  Here is what widening changes:
  - **Starvation disappears.** A 38-minute auditor run no longer holds the Gap
    Hunter (`CLAUDE.md`, 2026-09-12). This is the prize.
  - **Spend happens at once instead of spread out.** Content jobs share one
    OpenRouter key and one weekly limit. Exhausting it once already 402'd the
    auditor for a day (memory: auditor key isolation). N jobs in parallel reach
    the cap sooner, not more often.
  - **Pod load stacks.** Agent loops, `git clone --local` checkouts and MCP stdio
    servers stack up in one container, with no liveness probe to notice.
  - **Implicit ordering disappears.** The lane ran profile jobs in submit order.
    Anything that relied on "A finishes before B starts" in the same tick must say
    so explicitly (`context_from` reads the latest *completed* output, so it is
    safe).
  - **Bounding each run stays necessary** (`auditor.pending` `DEFAULT_LIMIT`). It
    now protects spend and the watchdog, not other agents' start times.
- **Proposal.** Keep the lane through the retirement: step 0a makes it
  store-aware, so moving is not widening. Widen as its own step 5, to
  `cron.max_parallel_jobs: 1` pinned on every profile via `OVERRIDES`. That gives
  serial runs within a profile, profiles in parallel, a total bounded by the
  number of profiles, and per-customer ordering unchanged.

---

## 5. Retirement order

Each step is one PR, merged with a merge commit, deployed with
`scripts/deploy.sh`, and accepted by the checks in section 6 before the next step
starts. Every step ends by updating the `CLAUDE.md` sections it makes stale.

### Step 0: parity for satellite-store runs (code; no job moves)

After this step a job in a profile store behaves like a fork `profile` job in
every way we depend on. Nothing moves yet, and the stores are empty, so existing
behaviour does not change. It can ship as up to four PRs:

| | Change | Tests |
|---|---|---|
| 0a | `is_sequential` is also true when `_get_hermes_home()` is not the launch home. The tick, `dispatch_job_async` and `run_event_job` already run inside the store's scope. The webhook path gets its store from the home-override fallback in `_current_cron_store` (`jobs.py:164-167`), not from `use_cron_store`. Pin that with a test, because a re-pointed `CRON_DIR` would silently send lookups to the default store. **Done:** `dispatch.in_profile_store()` compares `get_hermes_home()` with `get_routing_process_hermes_home()` and fails closed; the default store, ticked under its own scope, keeps its plain jobs parallel. | `tests/cron/test_sequential_dispatch_fork.py`, `tests/gateway/test_webhook_cron_job_lane_fork.py` |
| 0b | A satellite-store run enters `profile_run()` and calls `_assert_own_subprocess_identity`, without the `os.environ` merge. One fork re-anchor in the run path, next to `run_guard.guarded_run_job`. **Done:** `profile_scope._satellite_store_context`, entered in `guarded_run_job` beside `_job_profile_context`; inert for `profile` jobs and the launch store. | `tests/cron/test_profile_env_scope_fork.py`, `tests/cron/test_scheduler_fork.py` |
| 0c | `INJECT` gains BigLobster's Langfuse keys (also added to `TENANT_EXCLUDE`), the base URL, and any `HERMES_CRON_*` found in the service env. The auditor `.env` gains the `HERMES_AUDITOR_JUDGE_*` knobs. Rentals get their own Langfuse pin (section 3, "Rental Langfuse"). Add the negative `INJECT` test and the two rental tests. **This also fixes the probable stage 2b tracing gap (fact 5), and it closes the rental exposure, so it is worth shipping on its own first.** **Done:** shipped in #379, deployed as `sha-9a158aa99`. | `tests/test_biglobster_github_token_propagation.py`, `tests/hermes_cli/test_boot_reconcile_fork.py` |
| 0d | The incident sweep loads jobs from every served store (`use_cron_store` per `profiles_to_serve(multiplex=True)`). It prefixes incident ids with the profile **for non-default stores only**. Changing the ids of already-`seen` default incidents in `incidents/state.json` would re-alert every open one. **Done:** `sweep.load_served_store_jobs`; ids, titles and handoffs gain `<profile>/` for profile stores only. A profile-store handoff does not start `cron job id `, so remediation (which acts on the default store) refuses it, and the reconcile pass still sees default-store jobs only. | `tests/test_incident_sweep_regression.py` |
| 0e | `process_env_scope --check` adds, per profile: the key names a fork job resolves that a satellite job would not, minus the deny set; the child-env view for the auditor; and the default-store `profile` jobs with their delivery class. **Done:** printed after the existing report, names and classes only; a failure prints its type and never costs the verdict. Finding while testing: a moved auditor's child still inherits service-env-only knobs, and loses any that are also in the launch `.env` (`strip_launch_profile_env`). | `tests/agent/test_process_env_scope_fork.py` |
| 0f | `hermes cron move` (section 2.2). **Done:** `cron/fork_ext/move.py`. Takes several ids so `context_from` edges move together; `--to-default` needs `--from-profile <p>` and restores the `profile` field; the source keeps its pre-move pause state in `fork_move`, so a re-run after a crash finishes the move with that state. Dry run unless `--apply`. | new `tests/cron/test_cron_move_fork.py`: ordering, both crash points, refusals, id/`next_run_at`/executions/notepad preserved, never calls `remove_job` |
| 0g | The kickoff ping works for satellite jobs (fact 12). Send it after the run's secret scope is installed (`scheduler.py:3257`), and pass the resolved target so `SharedRouteAdapters` can authorize it. Check first whether default-store Telegram jobs are already affected. **Done:** in `cron/fork_ext/kickoff.py`, with no upstream call site moved: the ping installs the firing home's scope itself when none is active (a caller's scope is kept), and resolves each target with result delivery's own `_resolve_target_transport`. Whether default-store jobs were affected: `grep "kickoff ping failed" /opt/data/logs/agent.log`; the symptom reads "could not read this profile's TELEGRAM_HOME_CHANNEL". | `tests/cron/test_scheduler_fork.py` |

**Rollback:** revert the PR. No data changes.

### Step 1: move the jobs (ops, per cohort; no deploy)

Cohorts go in order of blast radius. Each cohort must be accepted before the next
starts.

1. **Canary.** One BigLobster-owned job that delivers to its own topic, with no
   webhook trigger and no `context_from`, firing at least daily. Pick it from the
   inventory.
2. **The rest of the BigLobster-owned profile jobs.** General-delivering ones are
   retargeted to their profile's topic in the same move (section 2.3).
3. **Rentals, one client first.** They write to client sites, and blog posts go
   live immediately. Check fact 11 per rental first.
4. **Auditor, last.** It is identity-critical and gates merges.
   1. Add `profile: auditor` to its route.
   2. Disable the route for the move window.
   3. Move the job.
   4. Change the GitHub webhook URL on every audited repo to
      `/p/auditor/webhooks/<route>` (`webhook.py:235`). This is an external change, and yours to make.
   5. Re-enable the route.

   Missed webhooks in the window are covered by the 6h poll.
5. **Stop creating the old shape.** `provision_bl_client.py` creates jobs inside
   `use_cron_store(profile_dir)` with no `profile=` (the webhook provisioning path
   follows). Code PR. Tests: `tests/scripts/test_provision_bl_agents.py`,
   `tests/scripts/test_rental_agent_toolsets.py`.
   **Done ahead of the cohorts (2026-10-01):** `provision()` creates the jobs under
   `use_cron_store(profile_dir)` with no `profile`, applies the boot's per-rental
   `.env` sync at once (the onboarding job fires in 5 minutes, before any boot; a
   parity test holds it equal to `sync_envs`), and refuses a `--deliver` other than
   `local`. The webhook path calls the same `provision()`.

**Rollback:** `hermes cron move <id> --to-default --apply` for that cohort. **Not
the multiplex lever** (fact 1). For the auditor, also restore the old webhook URLs.

### Step 2: drop the fork `profile` field (code)

**Precondition:** the 0e probe reports zero default-store `profile` jobs, twice,
at least 7 days apart, and cohort 1.5 is deployed.

Remove all of these:
- `_job_profile_context` and `ProfileResolutionError`. Keep
  `_assert_own_subprocess_identity`, which 0b now calls from the satellite path.
- `_read_profile_env_value` and the fork `profile` branches in
  `scheduler_delivery.py:465` and `:497`.
- `run_guard.py:106`.
- `scheduler.py:2402`.
- the `profile` clause in `is_sequential`.
- `jobs.py` `_normalize_profile` and the field (`:1710`, `:1716`, `:1888`).
- `scheduler_delivery.py:641`.
- `scheduler.py:2337`, which passes `profile` to the isolated checkout, and the
  slug in `isolated_checkout.py:146`.
- `JOB_ARG_FIELDS` `profile` and the list/detail rows in `cron/fork_ext/cli.py`
  (`:56-57`, `:85-86`), and `--profile` in
  `hermes_cli/subcommands/cron_fork_ext.py`.
- the `profile` param and routing-gap warning in `tools/cronjob_tools.py`
  (`:72-73`, `:138`, `:148-150`, `:710`).
- `--to-default` in `hermes cron move`.

**Keep one guard.** A record that still carries `profile` is refused with an error
naming `hermes cron move`. It is never run. Silently running it under the default
identity is exactly the `ProfileResolutionError` bug class.

Tests to update: `tests/cron/test_profile_env_scope_fork.py`,
`tests/cron/test_scheduler_fork.py`, `tests/cron/test_sequential_dispatch_fork.py`,
`tests/tools/test_cronjob_tools_fork.py`,
`tests/gateway/test_webhook_cron_job_lane_fork.py`,
`tests/cron/test_cron_move_fork.py`.

**Rollback:** revert and redeploy. No record carries the field, so there is
nothing to restore.

### Step 3: drop the `auto_profile` fallback (code)

**Precondition:** `Routing message to profile` (the log line in `base.py:4442`)
has zero occurrences for at least 7 days, in the default **and** the per-profile
logs (section 6). That line fires only when no route matched, so zero means the
fallback is dead code in production. `agent.log` rotates at 5 MB × 3 by default,
so check the rotated files' date span actually covers the 7 days before calling
it zero.

Remove:
- the call site at `base.py:4468-4473` and `_run_in_auto_profile` (`:4438`);
- `auto_profile=` at `plugins/platforms/telegram/adapter.py:7082`;
- the field at `gateway/platforms/event.py:81`.

Keep `hermes_cli/delegate_core.py` `run_delegate_in_profile`:
`hermes_cli/fork_ext/web.py:243-249` still calls it for profile-scoped delegation
from the web server (`delegate_runner` is the child it spawns). Its
`no_delegate_prompt` and `resume_history` kwargs lose their gateway caller, so
drop them if `web.py` doesn't pass them.

**This step adds code as well as removing it.** Deleting the call site alone
sends a bound topic with no route into `self._message_handler(event)`
(`base.py:4474`), and the **default** profile would answer a client's topic from
our memory. Examples of a bound topic with no route: a parked profile, or a topic
added before the next boot. The step must drop such a message, with a warning
naming the topic and profile, and add a test for it.

This step also retires `ROUTE_BOUND_TOPICS=False` as a lever, because unrouted
topics would now be dropped. The constant goes in step 4.

Tests: `tests/gateway/test_profile_topic_routes_fork.py`,
`tests/gateway/test_telegram_topic_profile_routing.py`.
`tests/hermes_cli/test_delegate_core_profile_cwd.py` stays as it is.

**Rollback:** revert and redeploy.

### Step 4: drop `fork_ext/multiplex.py` and the opt-out (code)

**Precondition:** steps 1–3 have run for at least 7 days with no rollback.

Remove:
- `hermes_cli/fork_ext/multiplex.py`;
- its two call sites, `hermes_cli/gateway_multiplex_mode.py:96` and `:246`;
- `tests/hermes_cli/test_gateway_multiplex_optout_fork.py`;
- the `("gateway", "multiplex_profiles")` pin in `OVERRIDES` and
  `ROUTE_BOUND_TOPICS` (upstream treats unset as on and rewrites an explicit
  false).

Restore upstream's expectations where the fork comments assert the opt-out:
`tests/hermes_cli/test_gateway_multiplex_s6.py:117`,
`tests/hermes_cli/test_cron_fire_dashboard.py:276`,
`tests/hermes_cli/test_gateway_enroll_multiplex_warning.py:69` and `:105`,
`tests/hermes_cli/test_gateway_multiplex_mode.py:135`, and
`tests/hermes_cli/test_boot_reconcile_fork.py`.

Keep `process_env_scope.py`: the launch profile's scope still needs the Zeabur
env. Rewrite `CLAUDE.md`'s "Rollback is both pins back to False".

**Rollback:** revert and redeploy. After this step no config-only lever remains,
which is why it comes last.

### Step 5 (separate, optional): widen the lane

`cron.max_parallel_jobs: 1` goes into `OVERRIDES` for main and every profile, and
`is_sequential` then returns only `workdir` jobs. Once that has been clean for
a week, it returns nothing, and `dispatch.py` goes with the re-anchor in
`scheduler_tick.py:108`. Tests: `tests/cron/test_sequential_dispatch_fork.py`,
`tests/test_incident_sweep_regression.py` (stall detection).

---

## 6. Acceptance: production checks per step

`CLAUDE.md`'s rule applies: tests passed both stage-2 attempts the first time, so
only production evidence counts. Before any Telegram test, wait for
`Cold boot: dropping Telegram updates` followed by `Gateway running` in
`agent.log`. Every Langfuse query prints the HTTP status and asks for
`fields=core,basic,io`.

**Logs are split per profile under multiplex.** `_enable_multiplex_log_routing`
(`gateway/run.py:1728`) routes each record by `get_hermes_home()` to
`/opt/data/profiles/<p>/logs/agent.log`. Satellite jobs' `Job '<id>'` lines land
there, and so does the old context's `using Hermes profile` line, which is logged
after the home override is set. **"Grep `agent.log`" below means both
`/opt/data/logs/agent.log` and `/opt/data/profiles/*/logs/agent.log`,
rotated files included.** A grep of the default log alone passes trivially.

| Step | Check | Pass |
|---|---|---|
| **every step** | plain message in General | answered by default |
| | `process_env_scope --check` | `VERDICT: OK` |
| 0 | message in the biglobster topic (thread 2), then Langfuse `v2/observations` for that session | HTTP 200 with observations. Before 0c, this documents fact 5. |
| | 0e probe | per-profile "would lose" lists contain only the deny set |
| | `incidents.sweep` dry run | lists jobs from every store |
| | after 0g, the kickoff ping of a default-store Telegram job | ping arrives; no `kickoff ping raised` in any log |
| | after 0c, a rental run | its trace appears in the **rental** Langfuse project (queried with the rental keys) and not in BigLobster's |
| | after 0c, the rental `.env` key names (a count, never values: `grep -c 'LANGFUSE_[A-Z]*_KEY=' /opt/data/profiles/<rental>/.env`) | 4 lines per rental: two set, two empty, or all four empty when the rental vars are unset |
| | after 0c, boot log | a names-only line per rental saying the rental Langfuse keys were pinned, and no values |
| 1, per job | `hermes -p <p> cron list` | same id and `next_run_at` as before the move |
| | `hermes cron list --all` | no longer shows it |
| | after its next slot, `hermes -p <p> cron runs <id>` | exactly one run, `ok`, at the slot |
| | `hermes cron runs <id>` | no new run (no double run) |
| | `agent.log` `Job '<id>'` lines | one run for the slot |
| | Langfuse `sessionId=cron_<id>_<YYYYMMDD>_<HHMMSS>` | tool-call observations with arguments (keys reach satellites) |
| | the profile's topic | the delivery arrives, and `last_delivery_error` is empty |
| | git-writing jobs | the PR author is the profile's identity, never `hermes-auditor` or the CEO |
| | kill-switch test, once per cohort: `hermes -p <p> cron pause <id>` | the incident watcher does not alert on the pause, and does alert on a forced failure |
| 1 auditor | a PR event on one repo | `[webhook] direct-cron-trigger queued ... route=<r>` under `/p/auditor/` |
| | review author | `hermes-auditor` |
| | poll | still runs every 6h |
| 2 | `hermes cron list --all` | no `Profile` rows |
| | every `agent.log` (default and per profile) | no `using Hermes profile` lines (the old context's log, `profile_scope.py:259`) |
| | each moved job's next slot | fires as in step 1 |
| 3 | one message in each bound topic (2, 3, 61, 1986, 3686, 1904) | each answered |
| | every `agent.log` | zero `Routing message to profile` |
| | a message in a topic bound to a profile with no route (test profile, parked) | dropped with the new warning; **not** answered by default |
| | each profile's `state.db` `sessions` table | a new row for that topic |
| 4 | control socket / `hermes gateway status` | reports multiplex on |
| | `/opt/data/config.yaml` | `multiplex_profiles` is `true` or absent. (The boot-log `fork keeps the opt-out` line only prints when opted out, so its absence proves little.) |
| 5 | two profiles' jobs due in the same minute | `hermes cron runs` shows overlapping start times across profiles and none within a profile |
| | OpenRouter spend for the day | within the usual band |

---

## 7. Decisions (2026-09-29)

1. **Profile jobs that post to General are retargeted to the profile's own
   topic** (section 2.3, option A). Making them default-profile jobs would run
   them with the default profile's keys.
2. **Rentals trace into their own Langfuse project with their own key pair**,
   pinned into rental `.env` files only, on every boot (section 3, "Rental
   Langfuse"). It is not BigLobster's project, because a key in a client
   profile's reach can be read by that client's agent.
3. **Parallel runs come later, at one job per profile at a time, as step 5 on its
   own after all the moves.** They are never combined with a move.

Docs that stage 3 makes stale, updated in the step that makes them stale:
- `CLAUDE.md`:
  - "Multiplex is on"
  - "Rollback is both pins back to False" (step 4)
  - "One long agent run starves every other agent" (step 5)
  - "A profile job's `.env` never reaches `os.environ`" (step 2)

Already corrected on 2026-09-29, ahead of stage 3: the `TERMINAL_CWD` sentences and
the "only multiplex runs those" line in `CLAUDE.md`, and the `process_env_scope.py`
docstring from fact 1.
