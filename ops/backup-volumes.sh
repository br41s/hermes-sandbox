#!/bin/bash
# Back up the non-Hermes service data on the Netcup server (Coolify, Nuremberg) and
# mirror it to Google Drive next to the nightly Hermes zip.
#
# Runs as root ON THE HOST (not in a container): it reads the Docker volumes directly
# and uses `docker exec` only for the logical dumps. Dumps are staged in a root-only
# directory and uploaded by the host's own rclone (Debian package, root-owned binary)
# with a root-only config, so other projects' data never passes through the Hermes
# container, whose volume every agent can read and whose rclone binary and config live
# on that writable volume.
# /root/.config/rclone/backup.conf holds the same Drive OAuth stanza as the container's
# rclone.conf; revoking that OAuth client stops both.
#
# Coolify also uploads the three Postgres databases to R2 every night. It does not see
# the volumes below, and the incident watcher reads this Drive folder, so both stay.
#
# Accepted trade-off (Brais, 2026-10-06): the Drive folder is reachable with the
# token the container also holds, so a compromised container could download these
# dumps from Drive. That token already reads the nightly Hermes zip (every profile's
# .env), so the marginal exposure is these dumps. Encrypting with an rclone crypt
# remote was offered and declined to keep restores key-free.
#
# Why dumps and not a tar of a running database: a file copy of a live Postgres
# data directory is not guaranteed restorable. pg_dumpall, a forced Redis SAVE and
# SQLite's online backup API are.
set -euo pipefail
set -o noclobber

VOLUMES=/var/lib/docker/volumes
RCLONE="/usr/bin/rclone --config /root/.config/rclone/backup.conf"
STAMP=$(date -u +%Y%m%d-%H%M%S)
STAGE=/var/backups/volumes
OUT=$STAGE/$STAMP
mkdir -p "$OUT" && chmod 700 "$STAGE" "$OUT"

# Coolify names a database's container after its uuid. The file names are the ones
# VOLUME_BACKUP_DUMPS in incidents/sweep.py requires: change them together.
dump_pg() {  # container label
  docker exec "$1" sh -c 'pg_dumpall -U "${POSTGRES_USER:-postgres}" --clean --if-exists' \
    | gzip > "$OUT/$2-pg_dumpall.sql.gz"
}
dump_pg dvohhynzxxgjzqzcprsqe88h biglobster-eu-chatwoot-pgvector
dump_pg h37zud6pi0ener9906gooucz social-agenda-postgres
dump_pg pcffmkflmojnujjjcgtvw20e flywell-postgres

# Redis (chatwoot sessions/cache): force a snapshot and insist on Redis saying OK
# (redis-cli exits 0 on an auth error), then copy the RDB file.
reply=$(docker exec 5iuojkalzft8ocrli3l7lcns \
  sh -c 'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning save' 2>&1 | tr -d '\r')
[ "$reply" = "OK" ] || { echo "redis SAVE did not return OK: $reply" >&2; exit 1; }
cp "$VOLUMES/redis-data-5iuojkalzft8ocrli3l7lcns/_data/dump.rdb" "$OUT/biglobster-eu-redis-dump.rdb"

# Chatwoot's uploaded files.
tar -C "$VOLUMES/zizhirxoo2p6azitnkr7rf4w_chatwoot-storage/_data" -czf "$OUT/chatwoot-storage.tar.gz" .

# The bl-site-package reference site (SQLite in WAL mode): online backup API, then the
# rest of the volume.
SITE=$VOLUMES/7upzm0o1hdox8bivde1nwg6g-bl-site-data/_data
python3 - "$SITE/app.db" "$OUT/bl-site-app.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
ok = dst.execute("pragma integrity_check").fetchone()[0]
dst.close(); src.close()
sys.exit(0 if ok == "ok" else 1)
PY
tar -C "$SITE" --exclude='app.db*' -czf "$OUT/bl-site-data.tar.gz" .

# Every staged file must be non-empty and every gzip must decompress.
for f in "$OUT"/*; do
  [ -s "$f" ] || { echo "empty dump: $f" >&2; exit 1; }
  case $f in *.gz) gzip -t "$f" || { echo "corrupt gzip: $f" >&2; exit 1; } ;; esac
done
echo "== staged (root-only): $OUT"; ls -la "$OUT"; du -sh "$OUT"

# Off-host copy from the host itself; the container is not involved. `check` fails
# the run unless Drive holds every staged file with matching size and hash.
$RCLONE copy "$OUT" "hermesdrive:/VolumeBackups/$STAMP" --transfers 1
$RCLONE check "$OUT" "hermesdrive:/VolumeBackups/$STAMP" --one-way
echo "== drive: VolumeBackups/$STAMP verified"
$RCLONE lsl "hermesdrive:/VolumeBackups/$STAMP"

# Retention (Brais, 2026-10-06): 14 days on the host, 30 in Drive. Only reached after
# the upload above verified, so a failing night never prunes. Only stamp-named
# directories are considered, by their UTC date in the name.
HOST_KEEP_DAYS=14; DRIVE_KEEP_DAYS=30
host_cutoff=$(date -u -d "$HOST_KEEP_DAYS days ago" +%Y%m%d)
drive_cutoff=$(date -u -d "$DRIVE_KEEP_DAYS days ago" +%Y%m%d)
for d in "$STAGE"/*/; do
  n=$(basename "$d")
  [[ $n =~ ^[0-9]{8}-[0-9]{6}$ ]] || continue
  if [ "${n:0:8}" -lt "$host_cutoff" ]; then echo "prune host: $n"; rm -rf -- "$STAGE/$n"; fi
done
$RCLONE lsf "hermesdrive:/VolumeBackups/" --dirs-only | tr -d '/' | while read -r n; do
  [[ $n =~ ^[0-9]{8}-[0-9]{6}$ ]] || continue
  if [ "${n:0:8}" -lt "$drive_cutoff" ]; then echo "prune drive: $n"; $RCLONE purge "hermesdrive:/VolumeBackups/$n"; fi
done
echo "== retention: host keeps >= $host_cutoff, drive keeps >= $drive_cutoff"
