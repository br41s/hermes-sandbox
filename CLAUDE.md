# Hermes Sandbox — Claude Code Instructions

Brais's fork of [NousResearch/hermes-agent](https://github.com/nousresearch/hermes-agent),
running in production as the COO for BigLobster and its customers. Repo: `br41s/hermes-sandbox`.

Extends the workspace `CLAUDE.md`. Rules here are fork- and deployment-specific only.

**This repo is the control plane for the other projects.** Changes here can affect
FinView, biglobster, grow-shop and every other repo Hermes touches. Treat production
changes accordingly.

## Upstream guide — read on demand, never whole

`AGENTS.md` (1,434 lines) is upstream's core development guide, written by NousResearch
contributors for people working on hermes-agent itself. Read the relevant section when a
task touches that subsystem; do not read it front to back.

| Working on | Read in AGENTS.md |
|---|---|
| A model tool | `## Adding New Tools`, `## Toolsets` |
| Plugins or skills | `## Plugins`, `## Skills`, `## Curator (skill lifecycle)` |
| Agent loop / prompts | `## AIAgent Class (run_agent.py)` |
| CLI or TUI | `## CLI Architecture`, `## TUI Architecture` |
| Config or env | `## Adding Configuration` |
| Scheduled jobs | `## Cron (scheduled jobs)` |
| Profiles | `## Profiles: Multi-Instance Support` |
| Tests | `## Testing` |
| Anything surprising | `## Known Pitfalls` |

Skip `## Contribution Rubric` unless opening an upstream PR — we run a fork.

## Two invariants — apply to every change

- **Per-conversation prompt caching is sacred.** A long-lived conversation reuses a cached
  prefix every turn. Anything that mutates past context, swaps toolsets, or rebuilds the
  system prompt mid-conversation invalidates the cache and multiplies cost. The only
  exception is context compression.
- **The core is a narrow waist; capability lives at the edges.** Every core tool is sent on
  every API call. New capability goes in a plugin or skill unless there is a concrete
  reason it cannot.

## How Hermes manages the other projects

**A customer or project is a Hermes profile.** Native profiles (`hermes_cli/profiles.py`)
give per-profile isolated memory, workspace, git credentials, skills and cron. We do not
build a namespacing layer on top. Live profiles: `default`, `grow-shop` (real client), and
the `hermes-*` role profiles.

Profile-scoped delegation runs in a **subprocess** with `HERMES_HOME=<profile home>` — the
web server is pinned to `default`, and in-process env mutation would race.

### Cron jobs with a workdir inject that repo's context file

`tools/cronjob_tools.py` — a job with `workdir` set injects the project context file from
that directory into its system prompt, and points terminal/file/code_exec at it.

Resolution order (`agent/prompt_builder.py:2011`), **first match wins, only one loads**:

1. `.hermes.md` / `HERMES.md` — walks up to the git root
2. `AGENTS.md` / `agents.md` — cwd only
3. `CLAUDE.md` / `claude.md` — cwd only
4. `.cursorrules` / `.cursor/rules/*.mdc`

Consequences to hold in mind:

- **Editing another repo's `CLAUDE.md` changes what Hermes injects into jobs run there.**
  A large project doc lands in that job's cached prefix on every turn — see the caching
  invariant above.
- `.claude/rules/` is a **Claude Code** mechanism. Hermes does not read it. A repo split
  into a lean `CLAUDE.md` plus rules gives Hermes only the lean core; it must use its file
  tools for the rest. This is deliberate for FinView.
- In this repo, `AGENTS.md` outranks `CLAUDE.md`, so **this file is inert to the Hermes
  runtime** — it is read by Claude Code only. Adding a `.hermes.md` here would not be:
  it would outrank AGENTS.md and change the agent's own context. Don't, without a reason.

### One long agent run starves every other agent

`cron/scheduler.py` dispatches every job that sets `profile` or `workdir` on a
**single-thread sequential pool** — profile execution mutates `os.environ` and a
context-local `HERMES_HOME`, so two cannot safely overlap. One slow job therefore
blocks all the others, and from outside a queued run is indistinguishable from a
dead one: the waiting job sits in `claimed` with no log output at all.

Seen on 2026-09-12 — a 38-minute auditor run held the thread while the BigLobster
Gap Hunter (`ce583d11dedd`) sat silent for 20+ minutes, which read on Telegram
exactly like a crash.

The defence is **bounding each agent, not widening the pool**. The agent loop
already hard-stops at `max_iterations=90` (`run_agent.py:434`), so the ceiling is
~90 tool calls; what matters is that a job's queue fits inside it —
`auditor.pending`'s `DEFAULT_LIMIT` exists for exactly that. Widening the pool
would mean removing the env mutation first; treat that as a separate, riskier
piece of work and do not fold it into a bug fix.

Corollary when triaging: before calling a quiet cron job dead, check whether
another profile/workdir job is running. `hermes cron runs <job_id>` shows the
holder.

## Deployment — Zeabur, Frankfurt

One engine, project `hermes-eu`, EU region for GDPR residency and to clear a Spanish Plesk
geo-block. Static egress IP `43.157.39.241`. Panel at
[blhermes.zeabur.app](https://blhermes.zeabur.app), GitHub-OAuth only.

Gotchas, each of which cost a session. Detail in workspace `memories/decisions/hermes.md`:

- **GitHub Actions builds; `scripts/deploy.sh` deploys.** `.github/workflows/
  ghcr-publish.yml` builds every push to main and publishes the same image as
  both `:latest` and `:sha-<commit>`. By the time you deploy the image already
  exists, so the script only moves the service tag and verifies — seconds, not
  the 8-20 minutes a build takes. Pull main, then run it: it derives the SHA
  itself, refuses a non-main branch, refuses to move the tag to an image that
  was never published, moves it, polls the pod until it reports that commit,
  and prints how to verify. `--dry-run` prints every command without running
  one.

  `--build` restores the old behaviour and builds via Cloud Build first. Keep
  it for when Actions is unavailable or its GHCR push breaks — it is the only
  path that needs `$GHCR_TOKEN`, and the only one that refuses a dirty tree
  (`gcloud builds submit` uploads the working directory, so uncommitted code
  would ship under a commit's tag; Actions builds the committed ref and cannot
  do that). The default path only warns, listing the uncommitted files so it
  is obvious they are not in the deploy.

  **The tag contract is `--short=9`, on both sides.** git picks an abbreviation
  length from the object count: a full clone gives 9, the shallow clone
  `actions/checkout` makes by default gives 7. If the workflow and the script
  ever disagree, the deploy points the service at a tag nobody pushed and the
  rollout dies as "Service Image Pull Failed" (PR #197/#198). Upstream's
  `docker.yml` stamps the FULL `github.sha` — do not copy it.

  **Actions needs package-level write, not just repo-level.** A GHCR package
  created by a PAT does not grant the repo's `GITHUB_TOKEN` write access, and
  the repo's "Workflow permissions → Read and write" setting does not change
  that — verified 2026-09-18, the push kept failing `permission_denied:
  write_package` after it was set. The fix is on the package: *Manage Actions
  access* → add the repo with role **Write**.

  The rest of this section is what the script automates — read it before
  overriding anything.

- **Deploy by moving the image tag. Everything else is a no-op.** Zeabur only reconciles a
  prebuilt service when its *spec* changes, and `latest` never looks changed — so every
  in-place operation is entitled to answer "nothing to do" and leave old code running under
  a healthy-looking container. All three of the obvious paths fail:
  `hermes gateway restart` (not supported on this platform, see below), `service restart`
  (re-runs the image already on the node, no pull), and `service redeploy`
  (`CANNOT_REDEPLOY_INPLACE` — it wants a bound GitHub repo, which a prebuilt service has
  no). Changing an env var to force a rollout does not reliably work either; it was tried
  and the pod never cycled.

  Cloud Build tags every build `sha-<commit>` as well as `latest`. Point the service at the
  sha — the spec changes, so a rollout has to happen, and afterwards you can name exactly
  which build is running:

  ```bash
  # 1. wait for the build to finish and publish, then:
  gcloud builds list --limit=3 --format="value(id,status,createTime)"
  # 2. point the service at that build (never omit -i=false; the CLI prompts otherwise)
  zeabur service update tag --id 6a5ea5074d439e41ee4cd38c -t sha-<commit> -y -i=false
  # 3. verify by a file that only exists in the new image
  zeabur service exec --id 6a5ea5074d439e41ee4cd38c -i=false -- ls -l /opt/hermes/<new file>
  ```

  **Order matters**: bumping anything before the image is published just cycles onto the
  stale one. Verify by mtime or by a file that did not exist before — never by grepping for
  a string, which an older image can also contain.

  `-t latest` puts it back on the floating tag when you want that.

  **Never read Cloud Build substitutions into a terminal.** `gcloud builds describe ...
  --format="value(substitutions)"` prints `_GITHUB_TOKEN` in clear. The token is stored in
  plaintext in every build's metadata; treat it as exposed and rotate it if it is ever
  printed.
- **`hermes gateway start/stop/restart` — the old "not supported on this platform" cause is
  gone; `/command/s6-svc` is still the known-good path.** The original reason no longer
  holds: `is_container()` detects Kubernetes too (`KUBERNETES_SERVICE_HOST`, the
  serviceaccount path, `kubepods`/`containerd`/`crio` cgroup markers), and
  `detect_service_manager()` no longer gates s6 on it at all — `_s6_running()` stands alone
  on `/proc/1/comm` + `/run/s6/basedir`, so the s6 dispatch path is live in a Zeabur pod.
  Nobody has re-verified the full lifecycle there since, so reach for
  `/command/s6-svc -u /run/service/gateway-<profile>` first and treat a working
  `hermes gateway restart` as a pleasant surprise worth recording here.
- **Container restarts every 1–2h are benign** — Zeabur deployment rollouts re-serialise the
  env array, producing a new pod-template-hash and a k8s rolling restart. Not a crash, not
  OOM; there are no liveness probes. Self-heals in ~3 min. Do not chase it.
- **Rotated secrets do not reach profile `.env` files.** Main `.env` is the source of truth
  for provider keys, but each profile carries its own. A rotation that updates only the main
  file leaves profiles on revoked keys — surfacing as OpenRouter 401 "User not found", not
  as a config error.
- **`group_topics` belongs at top-level `telegram.extra`**, not `display.telegram.extra`,
  which is dead config the adapter never loads.
- **Zeabur `service delete` half-completes** — UI hides it, backend record and PVC linger,
  retry returns `ALREADY_EXISTS`. `project export` shows what a clone would include.
- **Bulk data cannot move via `service exec`** (fails past ~5MB). Host-to-host root SSH
  rsync of `/opt/data` works. `project clone --region` carries services and volumes, but
  not volumes large enough to exceed the S3 backup limit.

## Diagnosing an agent run — Langfuse first, logs second

`/opt/data/logs/agent.log` is an **infrastructure** log, not a record of what an
agent did. It emits `agent.tool_executor: tool X completed` for tool calls issued
one at a time, but a **parallel batch of tool calls produces no such line at all** —
the results just appear as a jump in the next API call's input tokens. Diagnosing a
run from the log therefore undercounts tool use, and it fails in the dangerous
direction: it looks like the agent skipped work it actually did.

That misread happened on 2026-09-12. A Gap Hunter run showed "4 terminal calls, zero
web calls" in `agent.log`, and was one step away from being reported as having
fabricated its research ledger. The Langfuse trace for the same session showed **nine**
web calls — four `web_extract` in one parallel batch, four `web_search` in the next —
every one matching a ledger line. The `[SILENT]` it returned was correct behaviour.

**Langfuse is the source of truth for agent behaviour**: the full tool-call sequence
with arguments and outputs. Traces are keyed by the cron session id, which is
`cron_<job_id>_<YYYYMMDD>_<HHMMSS>` and appears in every `agent.log` line for the run.

**The env vars are `HERMES_`-prefixed for the keys and NOT for the base URL.** That
asymmetry is a trap: an unprefixed `$LANGFUSE_PUBLIC_KEY` expands to empty, the request
goes out unauthenticated, and Langfuse answers **401 with a body that still parses as
`{"data": []}`**. Read through a `.get("data", [])` that is "no traces" — which reads as
*the agent did nothing* or *tracing is broken*, when it means *you did not authenticate*.
Always print the HTTP status, never just the row count.

**Query `/api/public/v2/observations` — never `/traces`.** v4 has no trace object: a
trace is just the rows sharing a `traceId`, and `GET /traces`, `GET /traces/{id}` and
`GET /observations` are removed on 2026-11-16. There is no v2 single-trace getter and
no `traces get` worth reaching for; every question below is one observations query.
The same applies to the CLI — `langfuse-cli api observations list`, never
`api traces get` / `api traces list`, which are the deprecated v1 endpoints wearing a
friendlier name.

```bash
# Run inside the container. Note HERMES_ on the keys, none on the base URL.
# Everything in one session: root, generations and tool calls.
curl -s -w '\nHTTP:%{http_code}\n' -G \
  -u "$HERMES_LANGFUSE_PUBLIC_KEY:$HERMES_LANGFUSE_SECRET_KEY" \
  "$HERMES_LANGFUSE_BASE_URL/api/public/v2/observations" \
  --data-urlencode "sessionId=<session_id>" \
  --data-urlencode "fields=core,basic,io" \
  --data-urlencode "limit=100"

# One trace, or a time window when you have no session id.
#   --data-urlencode "traceId=<trace_id>"
#   --data-urlencode "fromStartTime=<ISO8601>"  (bound big queries; cursor-paginated)
```

**`fields` is the new way to get an empty answer.** A response carries only the field
groups you ask for, and the default is `core,basic` — which does **not** include
`input`/`output`. Omit `fields=...,io` and every tool call comes back with no
arguments and no result, which reads exactly like an agent that did nothing. Ask for
`io` whenever the question is *what did it actually do*. Input and output are raw
strings in v2; parse them yourself.

Observations of type `TOOL` carry the arguments; sort by `startTime` and a shared
timestamp across several tools means they were issued as one parallel batch.

**A killed run still has no trace header, but its finished tool calls are now
findable by `sessionId`.** Trace-level attributes flush when the trace finalises;
each observation flushes when it ends, carrying its own `sessionId` (this is what
the propagation fix below buys). Interrupt the process — the cron inactivity watchdog
at `cron/scheduler.py:4048` does this at `HERMES_CRON_TIMEOUT`, default 600s — and the
completed tool calls survive the query above while the root and the in-flight
generation never land, because a span that never ends never exports. Verified
2026-09-22 against a deliberately unfinalised run. Absence of the root is therefore
evidence the run was killed, not evidence it did nothing.

**Ruling out an empty answer that is not an empty result** — check these before
concluding anything about the agent: the 401 above; a missing `io` field group; a
killed run's unended spans; and, for any trace written before the session-propagation
fix shipped, children that never carried a `sessionId` at all (filter by `traceId`
instead for those).

| Question | Where |
|---|---|
| Did the run start, and when? | `agent.log` / `hermes cron runs <job_id>` |
| Which model, cache-hit rate, token counts | `agent.log` |
| Did it deliver, or return `[SILENT]`? | `agent.log` (`cron.scheduler` line) |
| Container restarts, scheduler state | `agent.log` |
| **What the agent actually did** | **Langfuse** |
| **Which tools, with what arguments** | **Langfuse** |
| **Whether it really visited a source** | **Langfuse** |

Never conclude an agent skipped a step from `agent.log` alone. Pull the trace.

## Secrets

Keys live in Zeabur env vars and propagate to profile `.env` files. **Never print a variable
table or `env[N].value` into a session transcript** — path-based redaction does not catch
those, and keys have leaked here that way before.

## Rented agents write to client sites through one audited path

The bl-site-package rentals (`AGENT_SOURCES` in `scripts/provision_bl_client.py`) all reach
a client's site over HTTP with that profile's own panel password — never a database, never
a repo. `bl_site_publish` covers blog and page text; `bl_site_product` covers product
sheets; `bl_site_health` is the read-only maintenance check; `bl_site_redirect` covers
same-site 301s — `scan` detects a same-site URL that's gone (its own history file, confirmed
dead on two separate runs), `find_target` reads a dead product URL's last Wayback Machine
snapshot and matches it to the current catalogue by barcode/reference, `propose`/`publish`/
`remove` write through the site's own `/api/redirects`. It's wired into the onsite SEO agent
(`onsite-seo/bl-site-package-seo-agent.prompt`), not Website Maintenance — detection belongs
with the tool that acts on it, independent of which other products a client has bought.

Two rules hold across all of them, and they are what makes unattended writing defensible:

- **Facts are the site's, prose is the agent's.** A tool submits text. Identifiers, the
  change-detection fingerprint and publication eligibility are decided server-side, so an
  agent cannot assert a barcode, pin a stale fingerprint, or talk itself into publishing
  something thin.
- **Everything except blog posts waits for a human.** Product sheets save as drafts
  unless publication is explicit and the site's checklist passes; redirects always save as
  pending, and `publish` only goes through for a match the site itself verified by
  identifier. That gate is `_refuse_unpublishable_tier`: it reads the row's stored
  `match_tier` back from `GET /api/redirects` and refuses anything but `gtin`/`mpn`,
  failing closed if it cannot read it. Reading the tier rather than accepting it as an
  argument is the point — an agent that would publish a weak match would also assert a
  strong tier. Anything resolved by title similarity or human judgement stays pending.
  Until 2026-09-18 that rule was prose in the tool description and the agent prompt and
  nothing else: the site's `POST /:id/publish` only re-checks that `new_path` resolves,
  so a `human`-tier redirect went live on one call.
  **Blog posts are the exception: `create_blog_post` hardcodes
  `"status": "published"` (`tools/bl_site_publish_tool.py`) and goes live on the client's
  public site immediately.** That is deliberate — it is why `gap-hunter`'s prompt says its
  rules are not optional, since nothing is checked afterwards — but it means a bad blog
  post is a client-visible incident, not a draft someone catches. Treat any change to what
  those agents write as shipping straight to production.
- **Blog posts are reversible instead, as of bl-site-package 1.8.x.** Every write through
  `PUT /api/blog/posts/:id` snapshots the body it replaces, in the same transaction, and the
  client restores it from their panel under Blog → Historial. That covers the whole fleet,
  not one agent, because it lives on the route rather than in a prompt: an infographic
  insert and a maintenance link repair are as restorable as a Content Updater correction.
  Two things make it real rather than nominal, and both are easy to regress:
  every writing agent passes **`author`** (without it the client's history attributes the
  agent's edit to *them*), and **`base_hash`**, which the site checks at write time and
  refuses on a mismatch — the lost-update guard that `onsite-seo` used to hand-roll by
  re-reading and comparing, which could never close the gap between the compare and the
  write. `base_hash` is optional on `PUT` so pre-existing callers keep working, and
  mandatory on `propose`. Deleting an article deletes its history with it: a revision row
  holds the full body, so leaving them behind would mean "delete" does not delete.

  **`content-updater` is the only rented agent that rewrites published prose**, and the only
  one that refuses to run at all on a site too old to keep versions — it checks for
  `content_hash` on its first read. It publishes directly like `gap-hunter`; what makes that
  defensible is the undo, plus a sourcing bar that scales with cost (an official source for
  any price, tax rate or legal deadline; the client's own site for any fact about their
  business; otherwise the stale claim is removed, never replaced). It reviews **one article
  per run and each article at most once a year** — the ledger flag, not the scoring, is what
  bounds the work, so an empty queue means the corpus is done, not that something broke.

### The Infographic Engineer is one agent for every site

`infographic/infographic-engineer.prompt` serves biglobster AND every rental. The
lane is resolved at runtime from `BL_SITE_URL`: set means the client's HTTP lane
(`bl_site_publish`, live on save), unset means the git lane (branch + PR, a human
merges). Only two things differ per stack — the delivery lane and the design-token
names (`--bg-surface`/`--font-body` here, `--bg`/`--font` on a client site). Never
fork the drawing rules for one client.

**Raster generation was removed from this agent.** An image model misspells its own
labels, renders hex colour codes as if they were text and crops its composition, and
none of that is checkable before it is live on a client's site. Everything is inline
SVG now, which the client sanitizer allows (`svg g title desc path rect circle
ellipse line polyline polygon text tspan` — but not `defs`, `marker` or `style`).

**`infographic/validate_infographic.py` is the gate, and it is not optional.** The
agent cannot render what it draws; the old prompt asked it to do the layout
arithmetic in its head and 16 of 82 published graphics had text outside its box or
the canvas. The validator measures with real Inter advance widths
(`inter-metrics.json`, regenerable with `measure_inter_metrics.js`) and exits
non-zero with a list of fixes. It is a committed script invoked by path because cron
denies `execute_code` — same convention as `onsite-seo/build_sitestate.py`. It needs
`hunspell`+`hunspell-es`, which the Dockerfile now installs, for the spelling check;
without them it warns and skips rather than failing.

Two things that look like details and are not:

- **The done-marker is `class="article-infographic"`, not the HTML comment.** The
  client sanitizer strips comments before render (they survive in the DB, which is
  why `shorts:auto` still works — that agent reads the API, never the page), and on
  biglobster 55 articles already carry a figure with no comment, so the old check
  would have given them a second infographic.
- **The canvas is always `viewBox="0 0 800 <height>"`,** on every site. Widths used
  to run 360→1000, so the same `font-size` rendered up to 3x bigger on one article
  than another. Each site fits it to the column and adds its own "Ampliar" control;
  presentation is the stack's job, not the agent's.

`product-sheets` is the exception worth remembering when selling: it only does anything for
a client whose catalogue comes from a distributor feed, because that feed is the only thing
it writes from. Sold to a client without one it goes quiet on every run.

## Fork-specific docs

- `AGENT_RENTAL_SETUP.md` — rental provisioning, incl. whitelisting `43.157.39.241` on 443
- `BIGLOBSTER_SETUP.md` — BigLobster profile wiring
- `hermes-already-has-routines.md` — what upstream provides before you build scheduling

## Before you build

Upstream already solves 40+ tools, skills, memory and delegation. Check `AGENTS.md` and the
existing plugins first. If it exists upstream under MIT, adapt it.
