#!/usr/bin/env bash
# Run the full test suite inside the production image — the merge gate.
#
# Every flag here was paid for with a wrong answer. Do not simplify it.
#
#   1. MOUNTS. Upstream's .dockerignore excludes tests/, docs/, website/,
#      acp_registry/, assets/, .github and .gitignore from the image. Without
#      the mounts the suite either cannot run at all (scripts/run_tests.sh
#      exits 0 with "No test files to run" — a PASSING exit code for a suite
#      that never ran) or invents ~10 files of failures for docs the tests read.
#
#   2. HOME MUST HAVE >=2 PATH COMPONENTS. The image runs as root, and
#      approval.py's _home_prefix_fold_regex deliberately refuses to fold a
#      single-component home (/home/alice folds, /home does not — an
#      anti-clobber guard). Under HOME=/root, absolute-path writes to
#      /root/.ssh/authorized_keys are not flagged dangerous and three security
#      tests "fail". Setting HOME=/home/tester took test_approval.py from
#      3 failed to 312 passed.
#
#   3. DEFAULT NETWORK. Do NOT pass --network none: it inflates failures ~5x by
#      breaking provider/DNS tests that are otherwise green.
#
#   4. NEVER the real entrypoint. Booting /init with prod env starts a SECOND
#      Telegram poller that steals updates from production.
#
# ALWAYS compare the PASS COUNT and the failing FILENAMES against the recorded
# baseline in tasks/upstream-merge-v2026.7.20.md — never the exit code, and
# never the raw failure count (the corpus grows).
#
# Usage:
#   scripts/gate.sh                      # build, then run the full suite
#   scripts/gate.sh --no-build           # reuse the existing image
#   scripts/gate.sh --no-build tests/cron # ...and only part of the suite

set -euo pipefail

IMAGE="${HERMES_GATE_IMAGE:-hermes-gate:local}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BUILD=1
if [[ "${1:-}" == "--no-build" ]]; then BUILD=0; shift; fi

if [[ "$BUILD" == "1" ]]; then
  echo "==> building $IMAGE (amd64 prod builds on Cloud Build; this is local arch)"
  docker build -t "$IMAGE" -f Dockerfile .
fi

# Only mount paths that exist, so this keeps working if upstream drops one.
MOUNTS=()
for p in tests docs website acp_registry assets .github .gitignore; do
  [[ -e "$REPO_ROOT/$p" ]] && MOUNTS+=(-v "$REPO_ROOT/$p:/opt/hermes/$p:ro")
done

if [[ $# -gt 0 ]]; then
  INNER="python -m pytest $* -q"
  echo "==> running: $INNER"
else
  INNER="scripts/run_tests.sh"
  echo "==> running the full suite"
fi

exec docker run --rm --entrypoint /bin/bash "${MOUNTS[@]}" "$IMAGE" -c "
  mkdir -p /home/tester && export HOME=/home/tester
  cd /opt/hermes && $INNER
"
