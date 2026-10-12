#!/usr/bin/env bash
# VERSIONED COPY. The running script is /opt/data/scripts/backup-full.sh on the Hermes
# volume (owner hermes, uid 10000), run nightly by cron job 3f6f866ce1af (no agent).
# It is not shipped in the image: after editing here, copy it to the volume as root
# and keep the owner. Kept in ops/ since 2026-10-06 so the "kept zip" rule below is
# reviewable; before that the script existed only on the volume.
#
# Daily full Hermes backup -> /opt/data/backups, then mirror newest zip to
# Google Drive (off-host copy), prune both to last KEEP.
# Run by Hermes cron (no_agent). Silent on full success; emits ALERT on failure.
set -uo pipefail
export HERMES_HOME=/opt/data
# Paths default to the production ones; BACKUP_* overrides exist for tests only.
HERMES="${BACKUP_HERMES:-/opt/hermes/bin/hermes}"
OUTDIR="${BACKUP_OUTDIR:-/opt/data/backups}"
RCLONE="${BACKUP_RCLONE:-/opt/data/scripts/bin/rclone}"
RCLONE_CONF="${BACKUP_RCLONE_CONF:-/opt/data/.config/rclone/rclone.conf}"
DRIVE_REMOTE=hermesdrive:/HermesBackups
KEEP=7
mkdir -p "$OUTDIR"
STAMP=$(date +%Y%m%d-%H%M%S)
ZIP="$OUTDIR/hermes-backup-$STAMP.zip"
LOG="$OUTDIR/last-backup.log"

if "$HERMES" backup -o "$ZIP" >"$LOG" 2>&1; then
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

# Retention: keep newest KEEP zips locally.
mapfile -t ALL < <(ls -1t "$OUTDIR"/hermes-backup-*.zip 2>/dev/null)
if [ "${#ALL[@]}" -gt "$KEEP" ]; then
  printf '%s\n' "${ALL[@]:$KEEP}" | xargs -r rm -f
fi

# If local backup failed, alert now and stop (no point syncing).
if [ "$STATUS" = FAIL ]; then
  echo "ALERT: Hermes full backup FAILED at $STAMP"
  echo "Log: $LOG"
  tail -15 "$LOG"
  exit 1
fi

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
    if ! "$RCLONE" --config "$RCLONE_CONF" copy "$NEWEST" "$DRIVE_REMOTE/" \
         --immutable --drive-chunk-size 64M --transfers 1 \
         >"$OUTDIR/last-drive-sync.log" 2>&1; then
      DRIVE_STATUS=FAIL
    else
      # Prune Drive to KEEP (newest first by name; zips are timestamped).
      "$RCLONE" --config "$RCLONE_CONF" lsf "$DRIVE_REMOTE/" \
        --files-only -R 2>/dev/null \
        | sort -r | tail -n +$((KEEP+1)) \
        | while read -r f; do
            [ -n "$f" ] && "$RCLONE" --config "$RCLONE_CONF" deletefile "$DRIVE_REMOTE/$f" >/dev/null 2>&1
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
  echo "WARN: Drive sync skipped (rclone or config missing) at $STAMP; local backup only."
  exit 0
fi
