# ops/ — the fork's own runbooks and security records

Deployment runbooks, incident write-ups and security procedures for
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

- **`backup-full.sh` runs from the volume; this directory holds the versioned copy.**
  The live file is `/opt/data/scripts/backup-full.sh`, run nightly by the default
  profile's cron job `3f6f866ce1af` (no agent). It zips `/opt/data` with `hermes backup`,
  mirrors the newest to Drive `hermesdrive:/HermesBackups`
  with `/opt/data/scripts/bin/rclone`, and keeps 7 on Drive and 2 locally. The zip excludes `backups/` itself.
  **2026-10-21**: a local zip past the newest 2 is deleted only after `rclone check
  --one-way` (size + MD5) finds it on Drive. A zip missing from Drive (a failed night)
  is uploaded again and re-checked; one that still does not verify stays local and
  raises `DRIVE SYNC FAILED`, while the others are pruned. Before that, `hermes backup` pruned its own zips to 3 (`--keep`
  defaults to 3) before anything was uploaded; the script now passes `--keep 0`. After editing
  `ops/backup-full.sh`, copy it to the volume path as root and keep it `10000:10000`, or
  the next run fails on permissions.
  **2026-10-06**: `hermes backup` exits non-zero when a file vanishes between its scan
  and the archive (cron output pruned mid-run) but still writes the zip atomically; the
  script deleted that zip, so 7 of the 10 nights before the fix had no backup at all.
  It now keeps a zip that exists, says `Archive kept` in the log and passes
  `zipfile.testzip()`. Previous version saved as `backup-full.sh.bak-20261006`.
  **rclone: always via PATH (the shim), never as root by full path
  `/opt/data/scripts/bin/rclone`.** 2026-10-08..11: rclone, run as root, rewrote
  `/opt/data/.config/rclone/rclone.conf` on a token refresh and left it root:hermes 0600;
  `backup-full.sh` (user hermes) failed with `Failed to load config file ... permission
  denied` and four nightly Drive syncs were lost. Three layers now guard it:
  `docker/rclone-exec-shim.sh` (installed at `/opt/hermes/bin/rclone`, first on PATH) drops
  root to hermes before running the real binary; `docker/stage2-hook.sh` re-chowns
  `/opt/data/.config/rclone` to hermes at every boot; and `backup-full.sh` writes an
  unreadable config, with owner/mode and the fix (`chown hermes:hermes
  /opt/data/.config/rclone/rclone.conf` as root), into `last-drive-sync.log` so the
  `DRIVE SYNC FAILED` alert carries it. Known gap: calling the real binary by full path as
  root bypasses the shim; the nightly pre-check catches that. The volume copy of
  `backup-full.sh` must be updated by hand (see above) for the pre-check to take effect.
- **`backup-volumes.sh` (this directory) runs as root on the host.** It dumps the
  other services' data (three Postgres via `pg_dumpall`, the Chatwoot Redis RDB, the
  bl-site-package reference site's SQLite via the online backup API, tars of Chatwoot's
  storage and the site's uploads; the Redis `SAVE` reply must be `OK`; every dump is size- and `gzip -t`-checked) into a root-only staging dir
  `/var/backups/volumes/<stamp>/` on the host, uploads it with the host's own `rclone`
  (Debian package), and fails unless `rclone check` confirms every file in Drive by size
  and hash. It uses a root-only config `/root/.config/rclone/backup.conf` (the
  container's Drive OAuth stanza) to `hermesdrive:/VolumeBackups/<stamp>`. Other projects'
  data never touches the Hermes container or its volume, where every agent has file tools
  and the rclone binary itself is writable. **Accepted trade-off:** the Drive folder is
  still readable with the token the container holds, which already reads the nightly
  Hermes zip; encrypting the dumps (rclone `crypt`) was declined on 2026-10-06 so a restore
  never depends on a key. Installed as `/root/backup-volumes.sh` on the host.
  **Scheduled nightly at 19:00 host time (Europe/Berlin)** by `/etc/cron.d/backup-volumes`
  through `backup-volumes-cron.sh` (this directory, installed as
  `/root/backup-volumes-cron.sh`), which appends one `OK` / `FAILED exit=N` line per run
  to `/var/log/backup-volumes.log` (logrotate weekly, 8 kept). Check it with
  `ssh netcup-nue 'tail -n 20 /var/log/backup-volumes.log'`. **The incident
  watcher checks the outcome** (`volume_backup_incidents` in `incidents/sweep.py`): the
  newest dated folder in Drive must be under 50h old and hold the three Postgres dumps,
  or the Incidents topic gets a brief. A night whose final `rclone check` failed after
  the dumps had landed still shows only in this log. **Retention** (Brais,
  2026-10-06): 14 days of stamps on the host, 30 in Drive, pruned by the script itself
  only after that night's upload verified. All three dumps were restored from Drive into
  throwaway containers on 2026-11-05, row counts equal to live.
- **Coolify also uploads the three Postgres databases to R2** (`vps-db-backups`, 03:00 UTC,
  30 days). It does not cover the volumes above, and the watcher does not read it.

## Server access

`ssh netcup-nue` (`62.83.91.62`, Debian 13) logs in as root with the key `~/.ssh/netcup-nue`
on Brais's Mac: Coolify needs root SSH, so `/etc/ssh/sshd_config.d/00-hardening.conf` sets
`PasswordAuthentication no` and `PermitRootLogin prohibit-password`.

- **Docker bypasses ufw.** `docker-user-firewall.service` runs
  `/usr/local/sbin/docker-user-firewall.sh`, which drops 8000/6001/6002 in the DOCKER-USER
  chain. Coolify's panel and API are therefore reached only through SSH:
  `ssh -N -L 8000:127.0.0.1:8000 -L 6001:127.0.0.1:6001 -L 6002:127.0.0.1:6002 netcup-nue`, then
  http://localhost:8000. All three ports are needed, or the panel says "Cannot connect to
  real-time service". Use `127.0.0.1`: port 6001 resets over IPv6 on the server.
- **Traefik files Coolify does not manage**, in `/data/coolify/proxy/dynamic/`:
  `hermes-webhooks.yaml` (`hook.biglobster.top` to `hermes:8644`), `coolify-webhooks.yaml`
  (`deploy.biglobster.top`, only `/webhooks/`, to Coolify for GitHub pushes) and
  `www-redirects.yaml` (301s for `www.`, since Coolify's own redirect is a 302).
- **Env values:** Coolify app env, never in a transcript. Secrets are runtime-only where
  possible, because build-time env becomes a Docker ARG.
