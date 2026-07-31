#!/usr/bin/env bash
# Register the merge drivers .gitattributes refers to.
#
# `merge=ours` is NOT built into git — without this config git silently falls
# back to the normal 3-way merge and the .gitattributes line does nothing. It is
# per-clone (lives in .git/config), so every clone and every worktree used for a
# merge needs it. Run once after cloning.
#
#   scripts/setup-merge-drivers.sh
#
# `true` as the driver command means "succeed without touching the file", i.e.
# keep OUR version. That is correct only for GENERATED files, which are then
# regenerated — see .gitattributes and tasks/upstream-merge-hygiene.md.

set -euo pipefail

git config merge.ours.driver true
echo "registered: merge.ours.driver"
echo
echo "Applies to (from .gitattributes):"
git check-attr merge -- uv.lock website/static/api/model-catalog.json
echo
echo "REMINDER: after any upstream merge, regenerate both —"
echo "  uv lock"
echo "  python scripts/build_model_catalog.py"
