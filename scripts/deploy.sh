#!/usr/bin/env bash
#
# deploy.sh — point the Hermes service at the current commit's image.
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
# something to look up: it is whatever `git rev-parse --short=9 HEAD` returns,
# because that is the value cloudbuild.yaml receives as _COMMIT_SHA and the
# value ghcr-publish.yml tags and stamps with. So this script derives it once
# and reuses it, and the value is never typed.
#
# USAGE
#   scripts/deploy.sh [--status] [--dry-run] [--verify-file PATH] [--yes] [--build]
#
# THE BUILD NORMALLY IS NOT THIS SCRIPT'S JOB. `.github/workflows/
# ghcr-publish.yml` builds every push to main and publishes BOTH `:latest` and
# `:sha-<commit>`, so by the time you deploy the image already exists and this
# script only has to move the service tag and verify — seconds, not 7-20 min.
# It refuses to move the tag to an image that was never published, so a failed
# or still-running Actions build cannot become a broken rollout.
#
# `--build` restores the old behaviour and builds via Cloud Build first. Keep
# it for when Actions is unavailable or its GHCR push is broken. ONLY that path
# REQUIRES a GitHub token in $GHCR_TOKEN or $GITHUB_TOKEN: cloudbuild.yaml's
# step 1 runs `docker login ghcr.io -u br41s --password-stdin` with it, so the
# build fails immediately without it. Set it with a LEADING SPACE so it stays
# out of shell history:
#     export GHCR_TOKEN=ghp_...
# or source it from a file you keep outside the repo.
#
#   --status        read-only: report which commit production is running and
#                   how far behind origin/main it is, then exit. Needs no
#                   token and no clean tree. `git pull` CANNOT answer this —
#                   it reports on your checkout, never on the image.
#   --dry-run       print every command, run none of them (the token is never
#                   printed — it shows as ***). Needs no credential: it warns
#                   about a missing token rather than refusing to rehearse.
#   --verify-file   after deploying, sha256 this repo-relative file inside the
#                   container and compare against local. Pass a file this
#                   deploy actually CHANGED — an unchanged file hashes the same
#                   on the old image and would pass a stale deploy.
#   --yes           skip the confirmation prompt
#   --build         build via Cloud Build first instead of using the image
#                   GitHub Actions already published. Needs the token above.
#
set -euo pipefail

SERVICE_ID="6a5ea5074d439e41ee4cd38c"
IMAGE="ghcr.io/br41s/hermes-sandbox"

DRY_RUN=0
ASSUME_YES=0
STATUS_ONLY=0
DO_BUILD=0
VERIFY_FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --status) STATUS_ONLY=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --build) DO_BUILD=1; shift ;;
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

# Ask a yes/no question, and exit unless the answer is yes.
#
# Use this rather than a bare `read`: at EOF `read` returns non-zero, which
# under `set -e` kills the script on that line — before any "aborted" branch
# can say why. A caller with no tty (CI, an agent session, a piped shell) then
# sees exit 1 and not one line of output, which looks exactly like a broken
# script rather than an unanswered prompt.
confirm() {
  local reply
  if ! read -r -p "$1" reply; then
    echo >&2
    echo "✗ no answer on stdin — this shell is not interactive." >&2
    echo "  Re-run with --yes to confirm up front." >&2
    exit 1
  fi
  [ "$reply" = "y" ] || [ "$reply" = "Y" ] || { echo "aborted"; exit 1; }
}

cd "$(dirname "$0")/.."

# ── --status: what is actually running out there? ────────────────────────────
# The question `git pull` cannot answer. A clean pull means your CHECKOUT
# matches origin/main; it says nothing about the image, and the two drift
# apart silently — on 2026-09-14 a build ran three minutes after one PR merged
# and 83 minutes before the next, so main was ahead of production while the
# laptop reported "Already up to date" and a deploy was skipped.
#
# The image carries its commit at /opt/hermes/.hermes_build_sha (Dockerfile
# ARG HERMES_GIT_SHA, fed by cloudbuild.yaml). Images built before that arg
# was wired have no file — report that honestly rather than guessing, because
# a wrong "up to date" here is exactly the failure this flag exists to stop.
if [ "$STATUS_ONLY" -eq 1 ]; then
  echo "→ Fetching origin/main"
  git fetch origin main --quiet
  LOCAL_MAIN="$(git rev-parse --short origin/main)"

  RUNNING="$(zeabur service exec --id "$SERVICE_ID" -i=false -- \
    sh -c 'cat /opt/hermes/.hermes_build_sha 2>/dev/null' 2>/dev/null \
    | tr -d "[:space:]")" || true

  echo
  echo "  origin/main : $LOCAL_MAIN  $(git log -1 --format=%s origin/main)"
  if [ -z "$RUNNING" ]; then
    echo "  production  : UNKNOWN — no /opt/hermes/.hermes_build_sha in the image"
    echo
    echo "  That image predates the HERMES_GIT_SHA build-arg, so it cannot say"
    echo "  which commit it is. Date it by hashing a file recent commits touched:"
    echo "      git show <commit>:<path> | shasum -a256"
    echo "      zeabur service exec --id $SERVICE_ID -i=false -- sha256sum /opt/hermes/<path>"
    echo "  The next deploy from this script bakes the SHA in and ends the guessing."
    exit 2
  fi

  echo "  production  : $RUNNING"
  echo
  if git merge-base --is-ancestor "$RUNNING" origin/main 2>/dev/null; then
    BEHIND="$(git rev-list --count "$RUNNING"..origin/main 2>/dev/null || echo "?")"
    if [ "$BEHIND" = "0" ]; then
      echo "✓ Production is at origin/main. Nothing to deploy."
      exit 0
    fi
    echo "⚠ Production is $BEHIND commit(s) behind origin/main:"
    git log --oneline --no-decorate "$RUNNING"..origin/main | sed "s/^/      /"
    echo
    echo "  Deploy with: scripts/deploy.sh"
    exit 1
  fi
  echo "⚠ $RUNNING is not an ancestor of origin/main — production is running code"
  echo "  that is not on main (a rollback, or a build from another branch)."
  exit 1
fi

# ── Guard 1: the build uploads THIS DIRECTORY, not the repo ──────────────────
# `gcloud builds submit` tars the working directory and ships that. A dirty
# tree therefore builds uncommitted code and publishes it under a commit's SHA
# tag — an image whose name is a lie, and the tag is immutable so it stays a
# lie. Refuse.
#
# Only on --build. The default path uploads nothing: Actions built the
# committed ref, this script moves a tag to it, and the deployed bits are
# identical whatever the working tree looks like. Refusing there blocked the
# ordinary case — local edits in progress, production behind main, deploy main
# — on a hazard that path does not have, and forced a stash to get around a
# check that was never about it.
#
# Still worth a word, because a dirty tree is the shape of "I want my changes
# live": say plainly that they are not in this deploy, then carry on.
if [ -n "$(git status --porcelain)" ]; then
  if [ "$DO_BUILD" -eq 1 ]; then
    echo "✗ Working tree is not clean." >&2
    echo "  The build uploads this directory, so uncommitted changes would ship" >&2
    echo "  under $(git rev-parse --short HEAD)'s tag. Commit, stash or clean first:" >&2
    git status --short >&2
    exit 1
  fi
  echo "⚠ Working tree is not clean — these changes are NOT part of this deploy."
  echo "  Actions built $(git rev-parse --short=9 HEAD) from the committed ref;"
  echo "  this script only moves the service tag to it."
  git status --short | sed "s/^/      /"
  echo
fi

# ── Guard 0: the build cannot start without a registry credential ────────────
# cloudbuild.yaml declares a default for _COMMIT_SHA but NOT for _GITHUB_TOKEN,
# so omitting it fails the whole submit with "key in the template
# _GITHUB_TOKEN is not matched in the substitution data" — after uploading
# ~200 MiB of context. Check first, fail in a second instead of a minute.
#
# A --dry-run builds nothing and logs in to nothing — it prints the token as
# *** and never reads the value — so requiring a valid credential to REHEARSE
# a deploy made the flag useless exactly when it is most wanted: checking what
# the script would do from a machine that has no token to hand. Warn and carry
# on instead, so the rehearsal still reports the real blocker.
GHCR_TOKEN="${GHCR_TOKEN:-${GITHUB_TOKEN:-}}"
if [ "$DO_BUILD" -eq 1 ] && [ -z "$GHCR_TOKEN" ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "⚠ No GitHub token in \$GHCR_TOKEN or \$GITHUB_TOKEN."
    echo "  Fine for --dry-run, but a real run stops here. Set it with a"
    echo "  LEADING SPACE so it stays out of shell history:"
    echo "      export GHCR_TOKEN=ghp_..."
    echo
  else
    echo "✗ No GitHub token in \$GHCR_TOKEN or \$GITHUB_TOKEN." >&2
    echo "  cloudbuild.yaml logs in to ghcr.io with it; the build cannot start." >&2
    echo "  Set it with a LEADING SPACE so it stays out of shell history:" >&2
    echo "      export GHCR_TOKEN=ghp_..." >&2
    exit 1
  fi
fi

# Presence is not validity, and the difference costs a whole build. A revoked
# or expired token passes the check above, uploads ~200 MiB of context, builds
# the image, and only then dies in step 1 on `docker login ghcr.io` with
# "denied: denied" — which is exactly what happened on 2026-09-12.
#
# So perform the real handshake here. `docker login` does not send basic auth
# to the registry: it exchanges the credential at ghcr.io's token endpoint for
# a bearer token, and that exchange is what actually fails for a dead PAT.
# (A plain `curl -u .../v2/<name>/tags/list` is NOT a valid check — the
# registry v2 protocol answers 401 there even for good credentials, to point
# the client at this same token service.)
#
# Derived from $IMAGE rather than hardcoded: the GHCR namespace has changed
# once already with the braisntext → br41s rename, and a check pinned to a
# stale owner would pass while the build still failed.
GHCR_REPO="${IMAGE#ghcr.io/}"
GHCR_USER="${GHCR_REPO%%/*}"
# Skipped on --dry-run: with no token there is nothing to validate, and probing
# a credential over the network is not part of rehearsing a command.
if [ "$DO_BUILD" -eq 1 ] && [ "$DRY_RUN" -eq 0 ] && command -v curl >/dev/null 2>&1; then
  if ! curl -fsS --max-time 15 -u "$GHCR_USER:$GHCR_TOKEN" \
        "https://ghcr.io/token?service=ghcr.io&scope=repository:${GHCR_REPO}:pull,push" \
        2>/dev/null | grep -q '"token"'; then
    echo "✗ GHCR rejected the token in \$GHCR_TOKEN/\$GITHUB_TOKEN." >&2
    echo "  It is present but not valid for pushing $IMAGE, so cloudbuild's" >&2
    echo "  step 1 would fail after uploading ~200 MiB and building." >&2
    echo "  Generate a CLASSIC PAT (not fine-grained) with write:packages," >&2
    echo "  read:packages and repo, then re-export it:" >&2
    echo "      https://github.com/settings/tokens" >&2
    echo "      export GHCR_TOKEN=ghp_...   # leading space keeps it out of history" >&2
    echo "  Do NOT reuse \$HERMES_AUDITOR_GITHUB_TOKEN — different account." >&2
    exit 1
  fi
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
# `--short=9`, never a bare `--short`: git picks the length from the object
# count, so a full clone here gives 9 while the shallow clone actions/checkout
# makes gives 7. The workflow stamps and tags with `--short=9` too; if the two
# ever disagree this script moves the service to a tag nobody pushed.
SHA="$(git rev-parse --short=9 HEAD)"
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
#
# Ask Cloud Build whether this _COMMIT_SHA has ever built green. NOT
# `--format="value(images)"`, which is what this guard used to grep: gcloud
# only fills `images` for builds that declare an `images:` block, and
# cloudbuild.yaml publishes with an explicit `docker push` step instead (step
# 3, immutable tag first). So that field is empty on every build in this
# project and the check it fed could never fire — a same-commit rebuild went
# through to a no-op deploy with no warning at all.
#
# Never widen --format to include substitutions: _GITHUB_TOKEN is stored in
# them in clear, and printing it here would leak it into the terminal and any
# pasted log. Filtering on a substitution does not print it; formatting does.
#
# Only meaningful when THIS script builds. In the default path Actions has
# already built every commit on main, so "has it been built" is always yes and
# the interesting question is the opposite one — has it been published yet —
# which is checked below instead.
if [ "$DO_BUILD" -eq 1 ] && command -v gcloud >/dev/null 2>&1; then
  if [ -n "$(gcloud builds list --limit=1 \
        --filter="substitutions._COMMIT_SHA=$SHA AND status=SUCCESS" \
        --format="value(id)" 2>/dev/null)" ]; then
    echo "⚠ $TAG has been built before — this commit is already published." >&2
    echo "  Re-pointing the service at the same tag changes no spec, so Zeabur" >&2
    echo "  will NOT roll out. If you need new code live, make a commit." >&2
    if [ "$ASSUME_YES" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
      confirm "  Continue anyway? [y/N] "
    fi
  fi
fi

# ── Guard 3: never point the service at an image nobody pushed ───────────────
# Without a build step the image is somebody else's output, so its existence
# stops being implied and has to be checked. Moving the tag to an absent image
# is not a no-op — it is a broken rollout ("Service Image Pull Failed", PR
# #197/#198), and the pod does not come back on its own.
#
# Anonymous pull token: the package is public, and this needs no credential in
# the default path. A registry that cannot be reached at all is reported as
# unknown rather than treated as absent — refusing to deploy because a network
# probe failed would be worse than letting the tag move and the poll below
# catch it.
if [ "$DO_BUILD" -eq 0 ]; then
  echo "→ Checking Actions published $TAG"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  [dry-run] HEAD https://ghcr.io/v2/%s/manifests/%s\n' "$GHCR_REPO" "$TAG"
  elif command -v curl >/dev/null 2>&1; then
    PULL_TOKEN="$(curl -fsS --max-time 15 \
      "https://ghcr.io/token?service=ghcr.io&scope=repository:${GHCR_REPO}:pull" 2>/dev/null \
      | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')"
    if [ -z "$PULL_TOKEN" ]; then
      echo "  ⚠ could not reach ghcr.io to confirm the tag — continuing" >&2
    elif curl -fsS -o /dev/null --max-time 20 \
          -H "Authorization: Bearer $PULL_TOKEN" \
          -H "Accept: application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.v2+json,application/vnd.docker.distribution.manifest.list.v2+json" \
          "https://ghcr.io/v2/${GHCR_REPO}/manifests/${TAG}" 2>/dev/null; then
      echo "  ✓ $IMAGE:$TAG exists"
    else
      echo "✗ $IMAGE:$TAG has not been published." >&2
      echo "  GitHub Actions builds every push to main — it is probably still" >&2
      echo "  running, or it failed. Check it:" >&2
      echo "      gh run list --branch main --workflow=ghcr-publish.yml --limit 3" >&2
      echo "  Then re-run this script, or pass --build to build it here instead." >&2
      exit 1
    fi
  fi
fi

if [ "$ASSUME_YES" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
  if [ "$DO_BUILD" -eq 1 ]; then
    confirm "Build and deploy $TAG? [y/N] "
  else
    confirm "Deploy $TAG? [y/N] "
  fi
fi

if [ "$DO_BUILD" -eq 1 ]; then
  echo "→ Building (7-20 min)"
  # Printed redacted on purpose: `run` would echo the token into the terminal,
  # and from there into scrollback and any pasted log.
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  [dry-run] gcloud builds submit --substitutions=_COMMIT_SHA=%s,_GITHUB_TOKEN=***\n' "$SHA"
  else
    gcloud builds submit --substitutions=_COMMIT_SHA="$SHA",_GITHUB_TOKEN="$GHCR_TOKEN"
  fi
fi

echo "→ Pointing service at $TAG"
run zeabur service update tag --id "$SERVICE_ID" -t "$TAG" -y -i=false

# ── Wait for the NEW pod, not for 60 seconds ─────────────────────────────────
# A flat sleep is a guess, and it guessed wrong on 2026-09-15: the rollout
# finished at 08:32:59 and the verify exec had already fired, dying with
# CONTAINER_NOT_FOUND on a deploy that had in fact succeeded. Under `set -e`
# that failed command substitution aborts the script, so a healthy deploy
# reports as a failure.
#
# "exec works" is NOT readiness either — during a rolling update the OLD pod
# answers perfectly well, and hashing a file against it would confirm the
# previous image. The baked-in build SHA is what distinguishes them, so poll
# until the container reports the commit THIS run deployed.
echo "→ Waiting for the pod to report $SHA (up to 5 min)"
if [ "$DRY_RUN" -eq 1 ]; then
  printf '  [dry-run] poll .hermes_build_sha until it reports %s\n' "$SHA"
else
  DEADLINE=$(( $(date +%s) + 300 ))
  POD_READY=0
  while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    sleep 10
    RUNNING_SHA="$(zeabur service exec --id "$SERVICE_ID" -i=false -- \
      sh -c 'cat /opt/hermes/.hermes_build_sha 2>/dev/null' 2>/dev/null \
      | tr -d '[:space:]')" || true
    if [ "$RUNNING_SHA" = "$SHA" ]; then
      POD_READY=1
      echo "  pod is serving $SHA"
      break
    fi
  done
  if [ "$POD_READY" -eq 0 ]; then
    echo "⚠ Pod did not report $SHA within 5 min (last seen: ${RUNNING_SHA:-none})." >&2
    echo "  The tag was moved, so the rollout may still be in progress. Check with:" >&2
    echo "      scripts/deploy.sh --status" >&2
    exit 1
  fi
fi

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
