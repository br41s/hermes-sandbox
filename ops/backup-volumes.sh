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

dump_pg() {  # ns deploy label
  kubectl exec -n "$1" "$2" -- sh -c 'pg_dumpall -U "${POSTGRES_USER:-postgres}" --clean --if-exists' \
    | gzip > "$OUT/$3-pg_dumpall.sql.gz"
}
dump_pg "$HNS" deploy/service-6a7dac382b4272705cd16068 biglobster-eu-chatwoot-pgvector
dump_pg environment-6a5f3b21b0b7a4abeb4e65ea deploy/service-6a5f3b214d439e41ee4d120e social-agenda-postgres
dump_pg environment-6a77a9a65f062718bc7b8222 deploy/service-6a77aa1bb3e95d61b0de29d4 flywell-postgres

# Redis (chatwoot sessions/cache): force a snapshot, then copy the RDB file.
kubectl exec -n "$HNS" deploy/service-6a7dac382b4272705cd16077 -- \
  sh -c 'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning save' >/dev/null
cp "$(ls -d "$ST"/pvc-9f0ca35c*)/dump.rdb" "$OUT/biglobster-eu-redis-dump.rdb"

# BigLobster sentinel (SQLite in WAL mode): online backup API, then the rest of the volume.
SENT=$(ls -d "$ST"/pvc-2b694de2*)
python3 - "$SENT/sentinel.db" "$OUT/biglobster-cursin-sentinel.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PY
tar -C "$SENT" --exclude='sentinel.db*' -czf "$OUT/biglobster-cursin-data.tar.gz" .

# "untitled" project (Chrome/CDP proxy, no running workload): plain tars. Its volumes
# were deleted on 2026-10-06; a missing volume is skipped, not an error.
for v in pvc-0b325c83 pvc-43eae7d9 pvc-11946d91; do
  d=$(ls -d "$ST"/${v}* 2>/dev/null || true)
  [ -n "$d" ] || { echo "skip $v: volume no longer exists"; continue; }
  tar -C "$d" -czf "$OUT/untitled-$(basename "$d" | sed 's/.*_//').tar.gz" .
done

echo "== staged (root-only): $OUT"; ls -la "$OUT"; du -sh "$OUT"

# Off-host copy from the host itself; the container is not involved.
$RCLONE copy "$OUT" "hermesdrive:/VolumeBackups/$STAMP" --transfers 1
echo "== drive: VolumeBackups/$STAMP"
$RCLONE lsl "hermesdrive:/VolumeBackups/$STAMP"
