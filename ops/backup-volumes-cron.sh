#!/bin/bash
# Cron wrapper for backup-volumes.sh on the Zeabur host: one line per run in the log,
# with the exit status, so a failed night is at least findable. Installed as
# /root/backup-volumes-cron.sh and called from /etc/cron.d/backup-volumes (02:00 host time,
# UTC+8, so 18:00 UTC).
#
# Check it with:  ssh zeabur-frankfurt 'sudo tail -n 20 /var/log/backup-volumes.log'
# The Hermes incident watcher checks the result in Drive (volume_backup_incidents), not
# this log: a failed rclone check after the dumps had landed shows only here.
LOG=/var/log/backup-volumes.log
START=$(date -u +%FT%TZ)
if /root/backup-volumes.sh >>"$LOG" 2>&1; then
  echo "$START OK" >>"$LOG"
else
  rc=$?
  echo "$START FAILED exit=$rc" >>"$LOG"
  exit "$rc"
fi
