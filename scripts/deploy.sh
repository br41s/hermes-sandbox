#!/usr/bin/env bash
#
# deploy.sh — build the current commit and point the Hermes service at it.
#
# WHY THIS EXISTS
# Deploying here means moving the service's image tag to `sha-<commit>`. Zeabur
# only reconciles a prebuilt service when its *spec* changes, so `:latest` never
# looks changed and restart/redeploy/env-var churn are all entitled to answer
# "nothing to do" while old code keeps running under a healthy-looking
# container. See CLAUDE.md, "Deploy by moving the image tag".
#
# That leaves a hand-run sequence whose one input is a short SHA typed twice —
# once into the build, once into the tag move. Getting it wrong has already put
# "Service Image Pull Failed" into production (PR #197/#198). The SHA is not
# something to look up: it is whatever `git rev-parse --short HEAD` returns,
# because that is the value cloudbuild.yaml receives as _COMMIT_SHA. So this
# script derives it once and reuses it, and the value is never typed.
#
# USAGE
#   scripts/deploy.sh [--dry-run] [--verify-file PATH] [--yes]
#
# REQUIRES a GitHub token in $GHCR_TOKEN or $GITHUB_TOKEN: cloudbuild.yaml's
# step 1 runs `docker login ghcr.io -u br41s --password-stdin` with it, so the
# build fails immediately without it. Set it with a LEADING SPACE so it stays
# out of shell history:
#     export GHCR_TOKEN=ghp_...
# or source it from a file you keep outside the repo.
#
#   --dry-run       print every command, run none of them (the token is never
#                   printed — it shows as ***)
#   --verify-file   after deploying, sha256 this repo-relative file inside the
#                   container and compare against local. Pass a file this
#                   deploy actually CHANGED — an unchanged file hashes the same
#                   on the old image and would pass a stale deploy.
#   --yes           skip the confirmation prompt
#
set -euo pipefail

SERVICE_ID="6a5ea5074d439e41ee4cd38c"
IMAGE="ghcr.io/br41s/hermes-sandbox"

DRY_RUN=0
ASSUME_YES=0
VERIFY_FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --verify-file) VERIFY_FILE="${2:-}"; [ -n "$VERIFY_FILE" ] || { echo "--verify-file needs a path" >&2; exit 2; }; shift 2 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  [dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

cd "$(dirname "$0")/.."

# ── Guard 1: the build uploads THIS DIRECTORY, not the repo ──────────────────
# `gcloud builds submit` tars the working directory and ships that. A dirty
# tree therefore builds uncommitted code and publishes it under a commit's SHA
# tag — an image whose name is a lie, and the tag is immutable so it stays a
# lie. Refuse.
if [ -n "$(git status --porcelain)" ]; then
  echo "✗ Working tree is not clean." >&2
  echo "  The build uploads this directory, so uncommitted changes would ship" >&2
  echo "  under $(git rev-parse --short HEAD)'s tag. Commit, stash or clean first:" >&2
  git status --short >&2
  exit 1
fi

# ── Guard 0: the build cannot start without a registry credential ────────────
# cloudbuild.yaml declares a default for _COMMIT_SHA but NOT for _GITHUB_TOKEN,
# so omitting it fails the whole submit with "key in the template
# _GITHUB_TOKEN is not matched in the substitution data" — after uploading
# ~200 MiB of context. Check first, fail in a second instead of a minute.
GHCR_TOKEN="${GHCR_TOKEN:-${GITHUB_TOKEN:-}}"
if [ -z "$GHCR_TOKEN" ]; then
  echo "✗ No GitHub token in \$GHCR_TOKEN or \$GITHUB_TOKEN." >&2
  echo "  cloudbuild.yaml logs in to ghcr.io with it; the build cannot start." >&2
  echo "  Set it with a LEADING SPACE so it stays out of shell history:" >&2
  echo "      export GHCR_TOKEN=ghp_..." >&2
  exit 1
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" != "main" ]; then
  echo "✗ On branch '$BRANCH', not main. Deploy from main." >&2
  exit 1
fi

echo "→ Fetching origin/main"
run git fetch origin main --quiet
if [ "$DRY_RUN" -eq 0 ] && [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
  echo "✗ Local main is not at origin/main. Run: git pull --ff-only origin main" >&2
  exit 1
fi

# ── The one input, derived rather than typed ─────────────────────────────────
# Must match cloudbuild.yaml's expectation exactly: it tags the image
# `sha-$_COMMIT_SHA`, and we pass this same value as _COMMIT_SHA. Note git
# chooses the abbreviation length itself (9 chars in this repo, not the 7
# GitHub displays) — another reason never to copy it from a web page.
SHA="$(git rev-parse --short HEAD)"
TAG="sha-$SHA"

echo
echo "  commit : $(git log -1 --oneline)"
echo "  tag    : $TAG"
echo "  image  : $IMAGE:$TAG"
echo "  service: $SERVICE_ID"
echo

# ── Guard 2: the same-commit no-op ───────────────────────────────────────────
# Rebuilding a commit reuses its tag, so the service spec does not change and
# no rollout happens — the deploy silently does nothing. A new commit is the
# only way out; there is no force.
if [ "$DRY_RUN" -eq 0 ] && command -v gcloud >/dev/null 2>&1; then
  if gcloud builds list --limit=20 --format="value(images)" 2>/dev/null | grep -q ":$TAG\$"; then
    echo "⚠ $TAG has been built before — this commit is already published." >&2
    echo "  Re-pointing the service at the same tag changes no spec, so Zeabur" >&2
    echo "  will NOT roll out. If you need new code live, make a commit." >&2
    if [ "$ASSUME_YES" -eq 0 ]; then
      read -r -p "  Continue anyway? [y/N] " reply
      [ "$reply" = "y" ] || [ "$reply" = "Y" ] || { echo "aborted"; exit 1; }
    fi
  fi
fi

if [ "$ASSUME_YES" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
  read -r -p "Build and deploy $TAG? [y/N] " reply
  [ "$reply" = "y" ] || [ "$reply" = "Y" ] || { echo "aborted"; exit 1; }
fi

echo "→ Building (7-20 min)"
# Printed redacted on purpose: `run` would echo the token into the terminal,
# and from there into scrollback and any pasted log.
if [ "$DRY_RUN" -eq 1 ]; then
  printf '  [dry-run] gcloud builds submit --substitutions=_COMMIT_SHA=%s,_GITHUB_TOKEN=***\n' "$SHA"
else
  gcloud builds submit --substitutions=_COMMIT_SHA="$SHA",_GITHUB_TOKEN="$GHCR_TOKEN"
fi

echo "→ Pointing service at $TAG"
run zeabur service update tag --id "$SERVICE_ID" -t "$TAG" -y -i=false

echo "→ Waiting 60s for the pod to cycle"
run sleep 60

# ── Verification ─────────────────────────────────────────────────────────────
# Deliberately not automatic without a file: the only cheap in-container proof
# is hashing a file, and an UNCHANGED file hashes identically on the old image,
# so an automatic check over an arbitrary file would report success for a
# deploy that never happened. Verify against something this deploy changed.
echo
if [ -n "$VERIFY_FILE" ]; then
  if [ ! -f "$VERIFY_FILE" ]; then
    echo "✗ --verify-file '$VERIFY_FILE' does not exist in the repo" >&2
    exit 1
  fi
  LOCAL_HASH="$(sha256sum "$VERIFY_FILE" | cut -d' ' -f1)"
  echo "→ Verifying /opt/hermes/$VERIFY_FILE"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  [dry-run] zeabur service exec --id %s -i=false -- sha256sum /opt/hermes/%s\n' "$SERVICE_ID" "$VERIFY_FILE"
    printf '  [dry-run] expecting %s\n' "${LOCAL_HASH:0:12}"
  else
    REMOTE_HASH="$(zeabur service exec --id "$SERVICE_ID" -i=false -- sha256sum "/opt/hermes/$VERIFY_FILE" | awk '{print $1}' | head -1)"
    if [ "$REMOTE_HASH" = "$LOCAL_HASH" ]; then
      echo "✓ Deployed. $VERIFY_FILE matches (${LOCAL_HASH:0:12})"
    else
      echo "✗ Mismatch — the new image is NOT running." >&2
      echo "    local : ${LOCAL_HASH:0:12}" >&2
      echo "    remote: ${REMOTE_HASH:0:12}" >&2
      echo "  Wait a minute and re-check; if it persists the tag did not move." >&2
      exit 1
    fi
  fi
else
  echo "→ Verify manually against a file this deploy changed, e.g.:"
  echo "    sha256sum <path>"
  echo "    zeabur service exec --id $SERVICE_ID -i=false -- sha256sum /opt/hermes/<path>"
  echo "  Same hash = the new image is live."
fi

echo
echo "Deployed $TAG"
echo "If any cron job's .prompt file changed, sync it into the live job:"
echo "  cronjob(action=\"sync_prompt\", job_id=\"<id>\")"
echo "The sync refuses if the live prompt was edited since its last sync — that"
echo "refusal is a review item, not something to force past."
