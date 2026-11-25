#!/usr/bin/env bash
# VERSIONED COPY. The running script is /opt/data/scripts/backup-full.sh on the Hermes
# volume (owner hermes, uid 10000), run nightly by cron job 3f6f866ce1af (no agent).
# It is not shipped in the image: after editing here, copy it to the volume as root
# and keep the owner. Kept in ops/ since 2026-10-06 so the "kept zip" rule below is
# reviewable; before that the script existed only on the volume.
#
# Daily full Hermes backup -> /opt/data/backups, slimmed, then mirror the newest zip
# to Google Drive (off-host copy). Drive keeps the last KEEP zips and the oldest is
# removed BEFORE the upload (Brais, 2026-11-25), because the 15 GB quota is shared
# with Gmail and Photos. Local keeps the last LOCAL_KEEP, and an older local zip is
# deleted only once the newest one is verified on Drive.
# Run by Hermes cron (no_agent). Silent on full success; emits ALERT on failure.
set -uo pipefail
export HERMES_HOME="${BACKUP_HERMES_HOME:-/opt/data}"
# Paths default to the production ones; BACKUP_* overrides exist for tests only.
HERMES="${BACKUP_HERMES:-/opt/hermes/bin/hermes}"
PYTHON="${BACKUP_PYTHON:-/opt/hermes/.venv/bin/python}"
SLIM="${BACKUP_SLIM:-$(dirname "$0")/backup_slim.py}"
OUTDIR="${BACKUP_OUTDIR:-/opt/data/backups}"
RCLONE="${BACKUP_RCLONE:-/opt/data/scripts/bin/rclone}"
RCLONE_CONF="${BACKUP_RCLONE_CONF:-/opt/data/.config/rclone/rclone.conf}"
DRIVE_REMOTE=hermesdrive:/HermesBackups
KEEP=3
LOCAL_KEEP=2
WARN_BYTES="${BACKUP_WARN_BYTES:-1600000000}"
mkdir -p "$OUTDIR"
STAMP=$(date +%Y%m%d-%H%M%S)
ZIP="$OUTDIR/hermes-backup-$STAMP.zip"
LOG="$OUTDIR/last-backup.log"

# --keep 0: `hermes backup` would otherwise prune hermes-backup-*.zip to its default 3
# before anything reached Drive. Local pruning happens below, after verification.
if "$HERMES" backup -o "$ZIP" --keep 0 >"$LOG" 2>&1; then
  STATUS=OK
elif [ -s "$ZIP" ] && grep -q "Archive kept" "$LOG" \
     && python3 -c 'import sys, zipfile; sys.exit(0 if zipfile.ZipFile(sys.argv[1]).testzip() is None else 1)' "$ZIP"; then
  # `hermes backup` exits non-zero when a file vanished between its scan and the
  # archive (cron output pruned mid-run) but still writes the zip atomically,
  # complete minus that file. A verified zip is a backup, not a failure: keep it.
  # Until 2026-10-06 this branch did not exist and 7 of 10 nightly zips were deleted.
  STATUS=OK
else
  STATUS=FAIL
  rm -f "$ZIP"
fi

if [ "$STATUS" = OK ]; then
  # Drop regenerable files and recompress with zstd. An unslimmed ~5 GB zip must
  # never be uploaded: it would fill the Drive quota and block Gmail too.
  if ! "$PYTHON" -I "$SLIM" "$ZIP" "$HERMES_HOME" >>"$LOG" 2>&1; then
    STATUS=FAIL
    rm -f "$ZIP"
  fi
fi

mapfile -t ALL < <(ls -1t "$OUTDIR"/hermes-backup-*.zip 2>/dev/null)

# If local backup failed, alert now and stop (no point syncing).
if [ "$STATUS" = FAIL ]; then
  echo "ALERT: Hermes full backup FAILED at $STAMP"
  echo "Log: $LOG"
  tail -15 "$LOG"
  exit 1
fi

# Local retention, run after the Drive copy. Each zip past the newest LOCAL_KEEP is
# deleted only once `rclone check --one-way` (size + MD5) verifies the NEWEST one on
# Drive. Until 2026-10-21 local zips were pruned before the upload, by `hermes backup`.
on_drive() {
  "$RCLONE" --config "$RCLONE_CONF" check "$OUTDIR/" "$DRIVE_REMOTE/" \
    --one-way --include "/$1" >>"$OUTDIR/last-drive-sync.log" 2>&1
}

# Delete Drive zips past the newest KEEP-1, so tonight's upload lands in the last free
# slot and the folder never holds more than KEEP. Gone before the copy on purpose:
# the quota counts trashed files too (--drive-use-trash=false). It only ever prunes to
# KEEP-1, so a night whose upload fails cannot take Drive below KEEP-1 copies.
make_room() {
  local list f rc=0
  if ! list=$("$RCLONE" --config "$RCLONE_CONF" lsf "$DRIVE_REMOTE/" \
      --files-only --include "/hermes-backup-*.zip" 2>>"$OUTDIR/last-drive-sync.log"); then
    echo "make_room: lsf failed" >>"$OUTDIR/last-drive-sync.log"
    return 1
  fi
  while read -r f; do
    [ -n "$f" ] || continue
    if ! "$RCLONE" --config "$RCLONE_CONF" deletefile "$DRIVE_REMOTE/$f" \
        --drive-use-trash=false >>"$OUTDIR/last-drive-sync.log" 2>&1; then
      echo "make_room: deletefile failed: $f" >>"$OUTDIR/last-drive-sync.log"
      rc=1
    fi
  done <<<"$(printf '%s\n' "$list" | sort -r | tail -n +"$KEEP")"
  return $rc
}

# --- Off-host mirror to Google Drive ---
# Drive failure is critical (it's the only off-host copy) but must NOT
# delete the good local zip. Track separately so the alert is specific.
DRIVE_STATUS=OK
if [ -x "$RCLONE" ] && [ -f "$RCLONE_CONF" ]; then
  NEWEST="${ALL[0]:-}"   # ls -1t => newest first
  if [ ! -r "$RCLONE_CONF" ]; then
    # rclone rewrites its config on every OAuth token refresh, so one run as root
    # leaves it root:hermes 0600 and this script (user hermes) cannot read it.
    # 2026-10-08..11: 4 nights lost to "Failed to load config file ... permission
    # denied". Say so in the log the DRIVE SYNC FAILED alert tails, with the fix.
    {
      echo "rclone config is not readable by $(id -un): $RCLONE_CONF"
      echo "current owner/mode: $(ls -l "$RCLONE_CONF")"
      echo "Fix (run as root): chown hermes:hermes $RCLONE_CONF"
      echo "Cause: rclone was run as root and rewrote the file on a token refresh."
      echo "Run rclone via PATH (the /opt/hermes/bin/rclone shim drops root to hermes),"
      echo "never as root by full path /opt/data/scripts/bin/rclone."
    } >"$OUTDIR/last-drive-sync.log" 2>&1
    DRIVE_STATUS=FAIL
  elif [ -n "$NEWEST" ]; then
    NEWEST_NAME=${NEWEST##*/}
    : >"$OUTDIR/last-drive-sync.log"
    # A make_room failure (lsf or deletefile) fails the run but the upload still goes:
    # a full folder is better than no off-host copy.
    make_room || DRIVE_STATUS=FAIL
    if ! "$RCLONE" --config "$RCLONE_CONF" copy "$NEWEST" "$DRIVE_REMOTE/" \
         --immutable --drive-chunk-size 64M --transfers 1 \
         >>"$OUTDIR/last-drive-sync.log" 2>&1; then
      DRIVE_STATUS=FAIL
    elif ! on_drive "$NEWEST_NAME"; then
      # Nothing local is deleted: the newest zip may not be safely off-host yet.
      DRIVE_STATUS=FAIL
      echo "Drive copy not verified; kept locally: $NEWEST_NAME" >>"$OUTDIR/last-drive-sync.log"
    else
      # The newest is on Drive in full: local zips past the newest LOCAL_KEEP can go.
      for f in "${ALL[@]:$LOCAL_KEEP}"; do
        rm -f "$f"
      done
    fi
  fi
else
  DRIVE_STATUS=SKIP
fi

if [ "$DRIVE_STATUS" = FAIL ]; then
  echo "ALERT: Hermes backup OK locally but DRIVE SYNC FAILED at $STAMP"
  echo "Local zip retained: $NEWEST"
  echo "Drive log: $OUTDIR/last-drive-sync.log"
  tail -15 "$OUTDIR/last-drive-sync.log"
  exit 1
elif [ "$DRIVE_STATUS" = SKIP ]; then
  echo "WARN: Drive sync skipped (rclone or config missing) at $STAMP; local backup only, nothing pruned."
  exit 0
fi

# Silent on full success, except when three copies would not fit in Drive.
SIZE=$(stat -c %s "$NEWEST" 2>/dev/null || echo 0)
if [ "$SIZE" -gt "$WARN_BYTES" ]; then
  echo "WARN: Hermes backup is $((SIZE / 1000000)) MB; three copies must fit in Drive (15 GB shared with Gmail and Photos)."
fi
exit 0
