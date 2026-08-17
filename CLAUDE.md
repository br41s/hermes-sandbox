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

## Fork-specific docs

- `AGENT_RENTAL_SETUP.md` — rental provisioning, incl. whitelisting `43.157.39.241` on 443
- `BIGLOBSTER_SETUP.md` — BigLobster profile wiring
- `hermes-already-has-routines.md` — what upstream provides before you build scheduling

## Before you build

Upstream already solves 40+ tools, skills, memory and delegation. Check `AGENTS.md` and the
existing plugins first. If it exists upstream under MIT, adapt it.
