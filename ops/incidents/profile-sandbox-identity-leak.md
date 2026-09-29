# Profile identity leak between sequential cron runs (2026-09-12)

> **Status (2026-09-29): fixed** — PR #232 (ef9f80b267, 2026-09-12); docs caveat in
> PR #235 (`website/docs/user-guide/configuration.md`). The tripwire `ProfileIdentityError`
> now lives in `cron/fork_ext/profile_scope.py` (re-exported by `cron/scheduler.py`), the
> snapshot pin in `tools/environments/base.py` (`_SNAPSHOT_PINNED_VARS`). The fork's
> per-profile sandbox keying in `tools/terminal_tool.py` was dropped in the v2026.9.24
> merge (51be1bfdc0) because upstream now covers it. Moved from
> `tasks/todo-profile-sandbox-identity-leak.md`.

## Root cause

Cron jobs of different profiles shared ONE terminal sandbox.

`tools/terminal_tool.py::_resolve_container_task_id` collapsed every tool-call
task_id to the single key `"default"`, so `_active_environments["default"]` was
one `LocalEnvironment` object shared by every job in the process, regardless of
<!-- no-tmp: ok — describes where upstream puts the snapshot -->
profile. That object owns a bash env snapshot file (`/tmp/hermes-snap-<id>.sh`)
which `BaseEnvironment._wrap_command` `source`s at the top of every command —
re-exporting the `HOME` captured when the environment was created, on top of the
`HOME` Hermes had just recomputed for this spawn.

In the terminal lane `HOME` IS the git/gh identity: `GITHUB_TOKEN`/`GH_TOKEN`
are Tier-1 stripped from every spawned subprocess (`local._ALWAYS_STRIP_KEYS`),
so `~/.gitconfig`, `~/.git-credentials` and `~/.config/gh/hosts.yml` are the
only credentials a command can reach.

Nothing reaped the environment between jobs: per-turn `cleanup_vm()` is called
with the agent's `session_id`, which never matches the `"default"` key, so only
the 5-minute idle reaper collects it.

## Evidence

    agent.log  07:04:24  cron_c19bb95c0a62 (auditor) creates local env "default"
               07:05:04  auditor run ends — NO cleanup line
               07:06:31  cron_3988cc0c189f (finview) starts, REUSES that env
               07:17:20  idle reaper finally cleans it up

<!-- no-tmp: ok — quotes the snapshot path the bug produced -->
    /tmp/hermes-snap-944fb1db9df2.sh
      declare -x HOME="/opt/data/profiles/auditor/home"
      declare -x HERMES_HOME="/opt/data/profiles/auditor"

    FinView PR #245 commit 0ea96c3b
      author + committer: hermes-auditor <hermes-auditor@users.noreply.github.com>

`os.environ` was fully restored by `_job_profile_context` and every on-disk
credential was correct, so neither a token check nor an env-restore audit would
have found this.

## Fix

- [x] `tools/terminal_tool.py` — scope the shared sandbox key to the active
      profile (`"default"` / `"default-<profile>"`). Brings the in-process cache
      into agreement with the Docker backend, which already reuses containers per
      `(task_id, profile)`. Subagents still share their parent's key.
- [x] `tools/environments/base.py` — pin `HOME`/`HERMES_HOME` across the snapshot
      `source`, so the snapshot can never override the per-spawn identity.
      Backend-agnostic (in-shell save/restore).
- [x] `cron/scheduler.py` — `ProfileIdentityError` tripwire: each profile job
      verifies, before it runs, that `get_subprocess_home()` is not another
      profile's home. Fails closed, matching `ProfileResolutionError`.
- [x] Regression tests, each verified to fail without its fix.

## Unprovisioned profiles

`resolve_profile_env` accepts ANY directory under `profiles/` as a profile.
`earthsaver` in production is a bare husk — created 2026-07-09, containing only
an auto-created `cron/output/`, with no `SOUL.md`, no `.env`, no `home/` and no
jobs. A job pointed at it would have run as the OS user.

The tripwire now fails closed for this shape, calibrated off the siblings: if
any other profile has a `home/`, this install pins identity per profile and one
without it is mis-provisioned. On a host install (no profile has `home/`) the
check stays silent, so the same code is strict in production and quiet on a
laptop.

The directory itself is inert but should be removed by hand — nothing reads it:

    rm -rf /opt/data/profiles/earthsaver    # run in the container, as hermes

## Decisions

- FinView #245 and biglobster #506: left as they are (CEO, 2026-09-12). Both
  carry `hermes-auditor` authorship in the commit itself, so neither can be
  rehabilitated by reopening.

## Still open

- [ ] `website/docs/user-guide/configuration.md` + the `hermes-agent-dev` skill
      still document the pre-fix "one container shared across sessions"
      collapse. Needs the profile-boundary caveat.
      *(2026-09-29: `configuration.md` done in PR #235. The `hermes-agent-dev` skill is
      not in this repo, so it cannot be checked from here.)*
