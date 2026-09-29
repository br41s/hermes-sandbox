#!/bin/sh
# onboard-profile.sh — interactive Hermes profile onboarding
#
# Run INSIDE the container as root or hermes:
#   sh /opt/hermes/scripts/onboard-profile.sh
#
# Steps:
#   1. Create profile (hermes profile create)
#   2. Copy provider/model config from main
#   3. Set up git credentials scoped to profile home
#   4. Clone project repo into profile workspace
#   5. Extract project description from README
#   6. Append project scope + work style to SOUL.md
#   7. Sync shared API keys into profile .env
#   8. Add Telegram topic → profile routing to config.yaml
#   9. Write gateway_state.json for s6 auto-start
#  10. Smoke test via /api/delegate

HERMES_HOME="${HERMES_HOME:-/opt/data}"
INSTALL_DIR="/opt/hermes"
PY="$INSTALL_DIR/.venv/bin/python3"
PROFILES_ROOT="$HERMES_HOME/profiles"

# ── helpers ────────────────────────────────────────────────────────────────

as_hermes() {
    if [ "$(id -u)" != 0 ]; then "$@"; return; fi
    if [ -x /command/s6-setuidgid ]; then
        /command/s6-setuidgid hermes "$@"
    else
        "$@"
    fi
}

# Prompts → stderr (visible even inside $() captures).
# Answer → stdout (captured by caller).
ask() {
    _default="$2"
    if [ -n "$_default" ]; then
        printf '%s [%s]: ' "$1" "$_default" >&2
    else
        printf '%s: ' "$1" >&2
    fi
    read -r _val
    printf '%s' "${_val:-$_default}"
}

ask_required() {
    _val=""
    while [ -z "$_val" ]; do
        _val=$(ask "$1" "")
        [ -z "$_val" ] && echo "  Required." >&2
    done
    printf '%s' "$_val"
}

die() { echo "ERROR: $*" >&2; exit 1; }

# ── sanity check ───────────────────────────────────────────────────────────

[ -d "$HERMES_HOME" ] || die "HERMES_HOME ($HERMES_HOME) not found. Run inside the container."
[ -f "$PY" ]         || die "Python venv not found at $PY."

# ── banner ─────────────────────────────────────────────────────────────────

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   Hermes Profile Onboarding Script   ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── gather inputs ──────────────────────────────────────────────────────────

echo "── Profile ──────────────────────────────────────────────"
PROFILE_NAME=$(ask_required "Profile name (slug, e.g. finview)")

PROFILE_DIR="$PROFILES_ROOT/$PROFILE_NAME"
if [ -d "$PROFILE_DIR" ]; then
    echo "  ⚠  Profile already exists: $PROFILE_DIR" >&2
    _ow=$(ask "Continue and overwrite?" "n")
    [ "$_ow" = "y" ] || [ "$_ow" = "Y" ] || { echo "Aborted."; exit 0; }
fi

echo ""
echo "── Git repo (leave blank to skip) ───────────────────────"
REPO_URL=$(ask "GitHub repo HTTPS URL" "")

echo ""
echo "── Telegram routing ─────────────────────────────────────"
echo "  URL format: t.me/c/<chat_id_no_prefix>/<thread_id>/<msg_id>" >&2
echo "  chat_id   = -100 + the number in the URL  (e.g. -1004224848555)" >&2
echo "  thread_id = the middle number             (e.g. 61)" >&2
echo "" >&2

EXISTING_CHAT_ID=$("$PY" - 2>/dev/null <<'PYEOF'
import os, yaml
try:
    cfg = yaml.safe_load(open(os.environ.get("HERMES_HOME", "/opt/data") + "/config.yaml"))
    topics = cfg.get("telegram", {}).get("extra", {}).get("group_topics", [])
    if topics:
        print(str(topics[0].get("chat_id", "")))
except Exception:
    pass
PYEOF
)

CHAT_ID=$(ask    "Telegram group chat_id (negative number)" "${EXISTING_CHAT_ID:-}")
THREAD_ID=$(ask_required "Telegram thread_id for the $PROFILE_NAME topic")
TOPIC_DISPLAY=$(ask "Topic display name" "$PROFILE_NAME")

echo ""
echo "── Summary ──────────────────────────────────────────────"
echo "  Profile  : $PROFILE_NAME"
echo "  Repo     : ${REPO_URL:-(skip)}"
echo "  Chat ID  : ${CHAT_ID:-(skip routing)}"
echo "  Thread   : $THREAD_ID"
echo "  Topic    : $TOPIC_DISPLAY"
echo "─────────────────────────────────────────────────────────"
printf 'Proceed? [y/N]: '
read -r _confirm
[ "$_confirm" = "y" ] || [ "$_confirm" = "Y" ] || { echo "Aborted."; exit 0; }
echo ""

# ── 1. create profile ──────────────────────────────────────────────────────

echo "[1/9] Creating profile '$PROFILE_NAME'..."
if as_hermes hermes profile create "$PROFILE_NAME" 2>/dev/null; then
    echo "      ✓ hermes profile create succeeded"
else
    echo "      ↳ CLI failed — creating dirs manually"
    as_hermes mkdir -p \
        "$PROFILE_DIR/memories" "$PROFILE_DIR/sessions" \
        "$PROFILE_DIR/skills"   "$PROFILE_DIR/workspace" \
        "$PROFILE_DIR/cron"     "$PROFILE_DIR/home"
    as_hermes sh -c "printf '# %s — Hermes profile\n' '$PROFILE_NAME' > '$PROFILE_DIR/SOUL.md'"
    echo "      ✓ dirs created"
fi

# ── 2. copy provider/model config from main ────────────────────────────────

echo "[2/9] Copying provider config from main..."
as_hermes "$PY" - <<PYEOF
import yaml
from pathlib import Path
main_cfg      = yaml.safe_load(open("$HERMES_HOME/config.yaml")) or {}
prof_cfg_path = Path("$PROFILE_DIR") / "config.yaml"
cfg = yaml.safe_load(prof_cfg_path.read_text()) if prof_cfg_path.exists() else {}
cfg = cfg or {}
for k in ("model", "web", "image_gen", "video_gen", "memory"):
    if k in main_cfg:
        cfg[k] = main_cfg[k]
prof_cfg_path.write_text(yaml.dump(cfg, default_flow_style=False))
print("      ✓ provider/model config copied from main")
PYEOF

# ── 3. git credentials ────────────────────────────────────────────────────

echo "[3/9] Setting up git credentials..."
_token="${GITHUB_TOKEN:-$(grep -m1 '^GITHUB_TOKEN=' "$HERMES_HOME/.env" 2>/dev/null | cut -d= -f2-)}"
if [ -n "$_token" ]; then
    HOME="$PROFILE_DIR" as_hermes git config --global credential.helper store
    HOME="$PROFILE_DIR" as_hermes git config --global user.name  "Hermes Agent"
    HOME="$PROFILE_DIR" as_hermes git config --global user.email "hermes@agent.local"
    printf 'https://x-access-token:%s@github.com\n' "$_token" | \
        as_hermes sh -c "cat > '$PROFILE_DIR/.git-credentials'"
    chmod 600 "$PROFILE_DIR/.git-credentials"
    echo "      ✓ .git-credentials written"
else
    echo "      ⚠ GITHUB_TOKEN not found — git clone over HTTPS may fail"
fi

# ── 4. clone repo ─────────────────────────────────────────────────────────

REPO_NAME=""
REPO_PATH=""
if [ -n "$REPO_URL" ]; then
    echo "[4/9] Cloning repo..."
    REPO_NAME=$(basename "$REPO_URL" .git)
    _ws="$PROFILE_DIR/workspace"
    REPO_PATH="$_ws/$REPO_NAME"
    as_hermes mkdir -p "$_ws"
    if [ -d "$REPO_PATH/.git" ]; then
        HOME="$PROFILE_DIR" as_hermes git -C "$REPO_PATH" pull --ff-only \
            && echo "      ✓ pulled latest"
    else
        HOME="$PROFILE_DIR" as_hermes git clone "$REPO_URL" "$REPO_PATH" \
            && echo "      ✓ cloned to $REPO_PATH"
    fi
else
    echo "[4/9] Skipping repo clone"
fi

# ── 5. extract project description from README ────────────────────────────

echo "[5/9] Reading project description from README..."
PROJECT_DESC=$("$PY" - <<PYEOF 2>/dev/null
import re
from pathlib import Path

repo_path = Path("$REPO_PATH") if "$REPO_PATH" else None
desc = ""

if repo_path and repo_path.exists():
    for name in ("README.md", "readme.md", "README.rst", "README"):
        p = repo_path / name
        if p.exists():
            text = p.read_text(encoding="utf-8", errors="ignore")
            lines = []
            for line in text.splitlines():
                s = line.strip()
                # Skip headings, badges, images, HTML, horizontal rules, empty
                if not s:
                    if lines:
                        break   # end of first paragraph
                    continue
                if s.startswith(("#", "![", "[![", "<", "---", "===")):
                    continue
                if re.match(r"^\[!\[", s):
                    continue
                lines.append(s)
                if sum(len(l) for l in lines) > 400:
                    break
            if lines:
                desc = " ".join(lines)[:500]
            break

print(desc if desc else "(no README description found)")
PYEOF
)
echo "      ✓ $PROJECT_DESC"

# ── 6. append to SOUL.md ──────────────────────────────────────────────────

echo "[6/9] Updating SOUL.md..."
_workspace_path="${REPO_PATH:-$PROFILE_DIR/workspace}"
as_hermes sh -c "cat >> '$PROFILE_DIR/SOUL.md'" <<SOULEOF

---

## Project Scope
- **Project:** $TOPIC_DISPLAY
- **Description:** $PROJECT_DESC
- **Repo:** ${REPO_URL:-(not configured)}
- **Working directory:** $_workspace_path
- Only operate on this project. Do not reference, report on, or act on other profiles or projects.
- When asked for project status, report only on this profile and its repo.

## Work Style
- **Simplicity first:** minimal changes, minimal code — no over-engineering.
- **Root causes only:** no temporary fixes or workarounds. Senior developer standards.
- **Act, don't ask:** when the path is clear, execute. Only ask when genuinely ambiguous.
- **Conventional commits:** \`type(scope): description\` (feat, fix, refactor, docs, chore, test). Imperative mood, ≤72 chars.
- **Atomic commits:** one logical change per commit. Never push without explicit confirmation.
- **Only touch what's needed:** do not refactor, rename, or reformat anything not directly related to the task.
- **Verify before done:** never mark a task complete without proving it works.
- **Bugs:** fix autonomously — read errors → reproduce → isolate root cause → fix → verify. Never retry the same failing approach.
- **State ownership:** always know where state lives, what breaks if you change it, and what the blast radius is.
- **Timing:** flag race conditions and async ordering issues before writing code.
- **Security:** escalate destructive or irreversible operations to the user before executing.
SOULEOF
echo "      ✓ SOUL.md updated"

# ── 7. sync API keys ──────────────────────────────────────────────────────

echo "[7/9] Syncing API keys into profile .env..."
as_hermes "$PY" - <<PYEOF
import os, re
from pathlib import Path

home        = Path("$HERMES_HOME")
profile_env = Path("$PROFILE_DIR") / ".env"
inject = [
    "OPENROUTER_API_KEY", "HERMES_CALLBACK_SECRET", "HERMES_CALLBACK_URL",
    "HERMES_MAX_ITERATIONS", "EXA_API_KEY", "HUGGINGFACE_API_KEY",
    "GITHUB_TOKEN", "AUXILIARY_VISION_MODEL",
]
content  = profile_env.read_text(encoding="utf-8") if profile_env.exists() else ""
main_env = (home / ".env").read_text(encoding="utf-8") if (home / ".env").exists() else ""

for var in inject:
    val = os.environ.get(var, "")
    if not val:
        m = re.search(rf"^{re.escape(var)}=(.*)", main_env, re.MULTILINE)
        val = m.group(1).strip() if m else ""
    if not val:
        continue
    if re.search(rf"^{re.escape(var)}=", content, re.MULTILINE):
        content = re.sub(rf"^{re.escape(var)}=.*", f"{var}={val}", content, flags=re.MULTILINE)
    else:
        sep = "" if (not content or content.endswith("\n")) else "\n"
        content += f"{sep}{var}={val}\n"

profile_env.write_text(content, encoding="utf-8")
print(f"      ✓ synced keys → {profile_env}")
PYEOF

# ── 8. telegram topic routing ─────────────────────────────────────────────

if [ -n "$CHAT_ID" ] && [ -n "$THREAD_ID" ]; then
    echo "[8/9] Adding Telegram topic routing..."
    as_hermes "$PY" - <<PYEOF
import yaml
from pathlib import Path

config_path = Path("$HERMES_HOME") / "config.yaml"
cfg = yaml.safe_load(config_path.read_text()) or {}

try:    chat_id = int("$CHAT_ID")
except: chat_id = "$CHAT_ID"
thread_id = int("$THREAD_ID")
new_entry = {"thread_id": thread_id, "name": "$TOPIC_DISPLAY", "profile": "$PROFILE_NAME"}

group_topics = cfg.setdefault("telegram", {}).setdefault("extra", {}).setdefault("group_topics", [])
chat_entry = next((e for e in group_topics if str(e.get("chat_id", "")) == str(chat_id)), None)
if chat_entry is None:
    chat_entry = {"chat_id": chat_id, "topics": []}
    group_topics.append(chat_entry)

topics = chat_entry.setdefault("topics", [])
existing = next((t for t in topics if t.get("thread_id") == thread_id), None)
if existing:
    existing.update(new_entry)
    print("      ✓ updated existing entry")
else:
    topics.append(new_entry)
    print(f"      ✓ added thread_id={thread_id} → profile=$PROFILE_NAME")

config_path.write_text(yaml.dump(cfg, default_flow_style=False, allow_unicode=True))
found = yaml.safe_load(config_path.read_text()).get("telegram", {}).get("extra", {}).get("group_topics", [])
print(f"      ✓ verified: telegram.extra.group_topics has {len(found)} chat entries")
PYEOF
else
    echo "[8/9] Skipping topic routing (no chat_id or thread_id)"
fi

# ── 9. gateway state ──────────────────────────────────────────────────────

echo "[9/9] Setting gateway auto-start state..."
as_hermes sh -c "printf '{\"gateway_state\": \"running\"}\n' > '$PROFILE_DIR/gateway_state.json'"
echo "      ✓ gateway_state.json → running"
echo "      ↳ Restart the container: the boot hook adds this profile's topic route, and the"
echo "        default gateway serves it (multiplex: there is no gateway-$PROFILE_NAME slot)."

# ── smoke test ────────────────────────────────────────────────────────────

echo ""
echo "── Smoke test ───────────────────────────────────────────"
_resp=$(curl -sf --max-time 10 \
    -X POST http://localhost:9119/api/delegate \
    -H "Content-Type: application/json" \
    -d "{\"task\": \"say hello\", \"profile\": \"$PROFILE_NAME\"}" 2>/dev/null \
    || echo "CURL_FAIL")

if echo "$_resp" | grep -q "accepted"; then
    echo "  ✓ /api/delegate profile=$PROFILE_NAME → accepted"
elif [ "$_resp" = "CURL_FAIL" ]; then
    echo "  ⚠ Dashboard unreachable — test manually after restart:"
    echo "    curl -X POST http://localhost:9119/api/delegate \\"
    echo "      -H 'Content-Type: application/json' \\"
    echo "      -d '{\"task\":\"say hello\",\"profile\":\"$PROFILE_NAME\"}'"
else
    echo "  ⚠ Unexpected: $_resp"
fi

# ── done ──────────────────────────────────────────────────────────────────

echo ""
echo "Done. Restart the container, then message thread $THREAD_ID in Telegram."
echo ""
