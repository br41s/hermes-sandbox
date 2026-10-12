#!/bin/sh
# shellcheck shell=sh
# /opt/hermes/bin/rclone — privilege-drop shim for the Drive backup client.
#
# Background
# ----------
# ops/backup-full.sh runs as the `hermes` user (UID 10000) and reads
# /opt/data/.config/rclone/rclone.conf. rclone rewrites that file whenever it
# refreshes the Drive OAuth token. On 2026-10-08 someone ran rclone as root in a
# `docker exec` / `zeabur service exec` session; the rewrite left the config
# root:hermes 0600 and the nightly Drive sync failed 4 nights running with
# "Failed to load config file ... permission denied".
#
# Fix
# ---
# Same pattern as hermes-exec-shim.sh. This shim sits at /opt/hermes/bin/rclone,
# earliest on PATH. As root it drops to the hermes user (s6-setuidgid) before
# exec'ing the real binary, so anything rclone writes under /opt/data is
# hermes-owned. As any other uid it execs the real binary directly.
#
# The real binary is not in the image; it lives on the data volume at
# /opt/data/scripts/bin/rclone (and may be absent). A system rclone is accepted
# as a fallback. Every candidate is an absolute path outside /opt/hermes/bin, so
# the second hop can never re-enter this shim.
#
# Known gap: calling the real binary by its full path as root bypasses this
# shim. ops/backup-full.sh checks for that every night.

set -e

REAL=
for candidate in /opt/data/scripts/bin/rclone /usr/local/bin/rclone /usr/bin/rclone; do
    if [ -x "$candidate" ]; then
        REAL=$candidate
        break
    fi
done

if [ -z "$REAL" ]; then
    echo "rclone-shim: no rclone binary found (looked in /opt/data/scripts/bin, /usr/local/bin, /usr/bin)" >&2
    exit 127
fi

if [ "$(id -u)" != "0" ]; then
    exec "$REAL" "$@"
fi

# Root: drop to hermes. s6-setuidgid is not on `docker exec`'s PATH; use the
# absolute path. Refuse to fall back to running as root silently.
S6_SUID=/command/s6-setuidgid
if [ ! -x "$S6_SUID" ]; then
    echo "rclone-shim: $S6_SUID not found; refusing to silently run rclone as root." >&2
    echo "rclone-shim: re-run with --user hermes." >&2
    exit 126
fi

# Libraries resolve config/cache paths off $HOME; /root is not writable by hermes.
export HOME=/opt/data

exec "$S6_SUID" hermes "$REAL" "$@"
