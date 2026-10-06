#!/bin/bash
# Back up the non-Hermes service volumes on the Zeabur server (Tencent Frankfurt)
# and mirror them to Google Drive next to the nightly Hermes zip.
#
# Runs as root ON THE HOST (not in a container): it reads the k3s local-path
# volumes directly and uses kubectl only for the logical dumps. Dumps are staged
# in a root-only directory and uploaded by the host's own rclone (Ubuntu package,
# root-owned binary) with a root-only config, so other projects' data never
# passes through the Hermes container, whose volume every agent can read and
# whose rclone binary and config live on that writable volume.
# /root/.config/rclone/backup.conf holds the same Drive OAuth stanza as the
# container's rclone.conf (copied 2026-10-06); revoking that OAuth client stops both.
#
# Accepted trade-off (Brais, 2026-10-06): the Drive folder is reachable with the
# token the container also holds, so a compromised container could download these
# dumps from Drive. That token already reads the nightly Hermes zip (every profile's
# .env), so the marginal exposure is these dumps. Encrypting with an rclone crypt
# remote was offered and declined to keep restores key-free.
#
# Why dumps and not a tar of a running database: a file copy of a live Postgres
# data directory is not guaranteed restorable. pg_dumpall / a forced Redis SAVE /
# SQLite's online backup API are.
#
# First run 2026-10-06 (one-off, see ops/README.md). Not scheduled yet.
set -euo pipefail
set -o noclobber

ST=/var/lib/rancher/k3s/storage
HNS=environment-6a5ea4ecb0b7a4abeb4e61fd
HDEP=deploy/service-6a5ea5074d439e41ee4cd38c
RCLONE="/usr/bin/rclone --config /root/.config/rclone/backup.conf"
STAMP=$(date -u +%Y%m%d-%H%M%S)
STAGE=/var/backups/volumes
OUT=$STAGE/$STAMP
mkdir -p "$OUT" && chmod 700 "$STAGE" "$OUT"

# Resolve a PVC prefix to exactly one directory. Zero matches is a clean skip (prints
# nothing, returns 1); more than one is a hard failure, never a guess.
one_dir() {  # prefix
  local matches=()
  for d in "$ST"/"$1"*; do [ -d "$d" ] && matches+=("$d"); done
  case ${#matches[@]} in
    0) echo "skip $1: volume no longer exists" >&2; return 1 ;;
    1) printf '%s\n' "${matches[0]}" ;;
    *) echo "refusing: $1 matches ${#matches[@]} volumes: ${matches[*]}" >&2; exit 1 ;;
  esac
}

dump_pg() {  # ns deploy label
  kubectl exec -n "$1" "$2" -- sh -c 'pg_dumpall -U "${POSTGRES_USER:-postgres}" --clean --if-exists' \
    | gzip > "$OUT/$3-pg_dumpall.sql.gz"
}
dump_pg "$HNS" deploy/service-6a7dac382b4272705cd16068 biglobster-eu-chatwoot-pgvector
dump_pg environment-6a5f3b21b0b7a4abeb4e65ea deploy/service-6a5f3b214d439e41ee4d120e social-agenda-postgres
dump_pg environment-6a77a9a65f062718bc7b8222 deploy/service-6a77aa1bb3e95d61b0de29d4 flywell-postgres

# Redis (chatwoot sessions/cache): force a snapshot and insist on Redis saying OK
# (redis-cli exits 0 on an auth error), then copy the RDB file.
if REDIS=$(one_dir pvc-9f0ca35c); then
  reply=$(kubectl exec -n "$HNS" deploy/service-6a7dac382b4272705cd16077 -- \
    sh -c 'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning save' 2>&1 | tr -d '\r')
  [ "$reply" = "OK" ] || { echo "redis SAVE did not return OK: $reply" >&2; exit 1; }
  cp "$REDIS/dump.rdb" "$OUT/biglobster-eu-redis-dump.rdb"
fi

# BigLobster sentinel (SQLite in WAL mode): online backup API, then the rest of the volume.
if SENT=$(one_dir pvc-2b694de2); then
  python3 - "$SENT/sentinel.db" "$OUT/biglobster-cursin-sentinel.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
ok = dst.execute("pragma integrity_check").fetchone()[0]
dst.close(); src.close()
sys.exit(0 if ok == "ok" else 1)
PY
  tar -C "$SENT" --exclude='sentinel.db*' -czf "$OUT/biglobster-cursin-data.tar.gz" .
fi

# "untitled" project (Chrome/CDP proxy, no running workload): plain tars. Its volumes
# were deleted on 2026-10-06; a missing volume is skipped, not an error.
for v in pvc-0b325c83 pvc-43eae7d9 pvc-11946d91; do
  d=$(one_dir "$v") || continue
  tar -C "$d" -czf "$OUT/untitled-$(basename "$d" | sed 's/.*_//').tar.gz" .
done

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
