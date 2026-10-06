#!/bin/bash
# Back up the non-Hermes service volumes on the Zeabur server (Tencent Frankfurt)
# and mirror them to Google Drive next to the nightly Hermes zip.
#
# Runs as root ON THE HOST (not in a container): it reads the k3s local-path
# volumes directly and uses kubectl for the logical dumps. The Hermes nightly
# zip excludes backups/, so the output is written there and uploaded explicitly
# with the Hermes container's own rclone + OAuth config, as user hermes.
#
# Why dumps and not a tar of a running database: a file copy of a live Postgres
# data directory is not guaranteed restorable. pg_dumpall / a forced Redis SAVE /
# SQLite's online backup API are.
#
# First run 2026-10-06 (one-off, see ops/README.md). Not scheduled yet.
set -euo pipefail

ST=/var/lib/rancher/k3s/storage
HERMES_PVC=$ST/pvc-29ddc303-6b0f-4ab2-9e33-8866c3a6aa06_environment-6a5ea4ecb0b7a4abeb4e61fd_data-service-6a5ea5074d439e41ee4cd38c
HNS=environment-6a5ea4ecb0b7a4abeb4e61fd
HDEP=deploy/service-6a5ea5074d439e41ee4cd38c
HERMES_UID=10000
STAMP=$(date -u +%Y%m%d-%H%M%S)
VOLDIR=$HERMES_PVC/backups/volumes
OUT=$VOLDIR/$STAMP

# This runs as root but writes into a directory the Hermes container (uid 10000) can
# also write to. Refuse to follow anything the container could have turned into a
# symlink, and never overwrite an existing file.
for p in "$HERMES_PVC" "$HERMES_PVC/backups" "$VOLDIR"; do
  if [ -L "$p" ]; then echo "refusing: $p is a symlink" >&2; exit 1; fi
done
mkdir -p "$OUT"
[ "$(realpath "$OUT")" = "$OUT" ] || { echo "refusing: $OUT resolves elsewhere" >&2; exit 1; }
set -o noclobber

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

# "untitled" project (Chrome/CDP proxy, no running workload): plain tars.
for v in pvc-0b325c83 pvc-43eae7d9 pvc-11946d91; do
  d=$(ls -d "$ST"/${v}*)
  tar -C "$d" -czf "$OUT/untitled-$(basename "$d" | sed 's/.*_//').tar.gz" .
done

chown -R "$HERMES_UID:$HERMES_UID" "$HERMES_PVC/backups/volumes"
echo "== local: $OUT"; ls -la "$OUT"; du -sh "$OUT"

# Off-host copy, as hermes, with the container's rclone and OAuth token.
kubectl exec -n "$HNS" "$HDEP" -- runuser -u hermes -- \
  /opt/data/scripts/bin/rclone --config /opt/data/.config/rclone/rclone.conf \
  copy "/opt/data/backups/volumes/$STAMP" "hermesdrive:/VolumeBackups/$STAMP" --transfers 1
echo "== drive: VolumeBackups/$STAMP"
kubectl exec -n "$HNS" "$HDEP" -- runuser -u hermes -- \
  /opt/data/scripts/bin/rclone --config /opt/data/.config/rclone/rclone.conf \
  lsl "hermesdrive:/VolumeBackups/$STAMP"
