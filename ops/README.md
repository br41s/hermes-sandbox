# ops/ — the fork's own runbooks and security records

Deployment runbooks (Zeabur), incident write-ups and security procedures for
this fork. They live here, not in `docs/`, on purpose:

- **Upstream folded `docs/` into the public Docusaurus site** (`website/docs/`)
  in v2026.9.x. Anything left in `docs/` gets carried there by the merge's
  directory-rename detection, so our incident and security runbooks would end
  up published.
- **A directory upstream does not have never conflicts.** These files cost
  seven "file location" conflicts on the v2026.9.24 merge while they sat in
  `docs/`.

Keep new fork-only docs here (or in `tasks/` for plans), never in `docs/` or
`website/`. When a `tasks/` plan ships and still has lasting value, move it here
with a status header saying where the work landed:

- `design/` — design records for shipped subsystems
- `incidents/` — root-cause write-ups
- `security/` — security incidents and procedures
- `upstream-merge/` — the upstream-sync runbook and per-merge records

## Backups

Two scripts, two homes:

- **`backup-full.sh` lives on the volume, not in this repo**:
  `/opt/data/scripts/backup-full.sh`, run nightly by the default profile's cron job
  `3f6f866ce1af` (no agent). It zips `/opt/data` with `hermes backup`, keeps 7 local
  zips, and mirrors the newest to Drive `hermesdrive:/HermesBackups` with
  `/opt/data/scripts/bin/rclone`. The zip excludes `backups/` itself. Edit it on the
  host path as root and keep it `10000:10000`, or the next run fails on permissions.
  **2026-10-06**: `hermes backup` exits non-zero when a file vanishes between its scan
  and the archive (cron output pruned mid-run) but still writes the zip atomically; the
  script deleted that zip, so 7 of the 10 nights before the fix had no backup at all.
  It now keeps a zip that exists, says `Archive kept` in the log and passes
  `zipfile.testzip()`. Previous version saved as `backup-full.sh.bak-20261006`.
- **`backup-volumes.sh` (this directory) runs as root on the host.** It dumps the
  other services' volumes (three Postgres via `pg_dumpall`, the chatwoot Redis RDB, the
  BigLobster sentinel SQLite via the online backup API, the idle Chrome/CDP volumes as
  tars) into `/opt/data/backups/volumes/<stamp>/` and uploads them with the Hermes
  container's rclone to `hermesdrive:/VolumeBackups/<stamp>`. First run 2026-10-06
  (192 MB). Installed as `/root/backup-volumes.sh` on the host; **not scheduled**, so
  those volumes are only as fresh as the last manual run.

## Server access (key-only since 2026-10-06)

The Tencent Frankfurt host (`43.157.39.241`) accepts SSH **only** with the key
`~/.ssh/zeabur_frankfurt` on Brais's Mac, as `ubuntu` (passwordless sudo). The Mac's
`~/.ssh/config` has the alias, so:

```bash
ssh zeabur-frankfurt 'sudo kubectl get pods -A'
```

`/etc/ssh/sshd_config.d/00-hardening.conf` sets `PasswordAuthentication no`,
`KbdInteractiveAuthentication no`, `PermitRootLogin no`. It sorts before Tencent's
`50-cloud-init.conf` (`PasswordAuthentication yes`) and sshd keeps the first value, so
leave the `00-` prefix alone. Consequences:

- `npx zeabur@latest server ssh` / `ssh-info` and Zeabur's dashboard "Open Terminal" no
  longer log in: they use the stored password. Zeabur's deployments and restarts do not
  use SSH (k3s talks outbound), verified before and after.
- The `zeabur-server-ssh` plugin skill's password recipe is dead here; use the alias.
- **Lockout escape is OS reinstall from the Zeabur server page, not "Reset SSH
  password"**, which resets a password sshd will not accept. Keep the key backed up.
- `authorized_keys.bak-20261006` holds the two `hermes-migration-ephemeral` keys that
  were removed (private halves unknown).
