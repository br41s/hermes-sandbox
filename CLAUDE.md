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

## Deployment — Zeabur, Frankfurt

One engine, project `hermes-eu`, EU region for GDPR residency and to clear a Spanish Plesk
geo-block. Static egress IP `43.157.39.241`. Panel at
[blhermes.zeabur.app](https://blhermes.zeabur.app), GitHub-OAuth only.

Gotchas, each of which cost a session. Detail in workspace `memories/decisions/hermes.md`:

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
- **`hermes gateway start/stop/restart` does not work on Zeabur.** `is_container()` detects
  Docker only (`/.dockerenv`, cgroup `docker`), not Zeabur's k8s pods, so it reports "Not
  supported on this platform." Use `/command/s6-svc -u /run/service/gateway-<profile>`.
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

## Secrets

Keys live in Zeabur env vars and propagate to profile `.env` files. **Never print a variable
table or `env[N].value` into a session transcript** — path-based redaction does not catch
those, and keys have leaked here that way before.

## Rented agents write to client sites through one audited path

The bl-site-package rentals (`AGENT_SOURCES` in `scripts/provision_bl_client.py`) all reach
a client's site over HTTP with that profile's own panel password — never a database, never
a repo. `bl_site_publish` covers blog and page text; `bl_site_product` covers product
sheets; `bl_site_health` is the read-only maintenance check; `bl_site_redirect` covers
same-site 301s — `find_target` reads a dead product URL's last Wayback Machine snapshot and
matches it to the current catalogue by barcode/reference, `propose`/`publish`/`remove` write
through the site's own `/api/redirects`.

Two rules hold across all of them, and they are what makes unattended writing defensible:

- **Facts are the site's, prose is the agent's.** A tool submits text. Identifiers, the
  change-detection fingerprint and publication eligibility are decided server-side, so an
  agent cannot assert a barcode, pin a stale fingerprint, or talk itself into publishing
  something thin.
- **Nothing an agent writes goes live implicitly.** Blog posts save as drafts; product
  sheets save as drafts unless publication is explicit and the site's checklist passes;
  redirects save as pending and only auto-publish on a checksum-verified barcode or
  manufacturer-reference match — anything resolved by title similarity or human judgement
  stays pending for a person to publish.

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
