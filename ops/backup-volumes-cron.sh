#!/bin/bash
# Cron wrapper for backup-volumes.sh on the Zeabur host: one line per run in the log,
# with the exit status, so a failed night is at least findable. Installed as
# /root/backup-volumes-cron.sh and called from /etc/cron.d/backup-volumes (02:00 UTC).
#
# Check it with:  ssh zeabur-frankfurt 'sudo tail -n 20 /var/log/backup-volumes.log'
# Nothing pages anyone on failure yet: the Hermes incident watcher does not see host
# cron, so a broken night shows only in this log.
LOG=/var/log/backup-volumes.log
START=$(date -u +%FT%TZ)
if /root/backup-volumes.sh >>"$LOG" 2>&1; then
  echo "$START OK" >>"$LOG"
else
  rc=$?
  echo "$START FAILED exit=$rc" >>"$LOG"
  exit "$rc"
fi
