# Model watch — weekly check of the models we use

## What it is

A weekly check of every model we use. One stdlib-only script, `model_watch/watch.py`, runs in two places.

- **In Hermes:** a `no_agent` cron job "Model watch — OpenRouter", default store, `0 8 * * 1`.
  Script `model_watch.sh`, seeded every boot by `docker/cont-init.d/03-biglobster-config` §5h.
  Delivers to Telegram (home channel); no LLM runs. Silent when nothing changed since last week.
  State: `/opt/data/model-watch/state.json`.
- **On Brais's Mac:** `python3 -I /Users/brais/VSCODE/hermes-sandbox/model_watch/watch.py opencode`, for the model chains in `/Users/brais/VSCODE/.claude/skills/opencode-delegate/delegate.sh` (workspace repo). Weekly; see "Mac schedule" below.

## The rule (Brais, 2026-11-21)

- Same family, more capacity, price ≤ → update automatically.
- Same family, more capacity, dearer → report with a comparison table ("Avisar").
- Other family, more capacity, cheaper → suggest with a table ("Sugerencia"). Never applied
  without a human A/B.

## How it decides

**Family.** The `family` field of the OpenCode Go/Zen catalogs (vendor + tier): `deepseek-flash` ≠ `deepseek-pro`, `glm` ≠ `glm-flash`. OpenRouter ids are matched through `canonical_id`; models in neither catalog fall back to `<stem>:<tier>` from the id. A flash model never moves to pro on its own.

**Capacity.** Artificial Analysis intelligence index, from `benchmarks.artificial_analysis.intelligence_index` in the public OpenRouter catalog (no key). Same family: newer release and the index does not drop (a missing index on either side means the newer version counts). Other family: both must have an index and the candidate's must be higher. Epoch AI's ECI (`https://epoch.ai/data/eci_scores.csv`, CC BY) is a second opinion only; a model without an AA index cannot be compared across families (the report lists it under "Notas").

**Price.** Every numeric price field of the catalog entry (input, output, cache, image fees…).
"≤" means ≤ on every field.

**Hard filters for OpenRouter candidates:** a Zero-Data-Retention endpoint (`GET /api/v1/endpoints/zdr`); no `:free` or other `:` variants; no `anthropic/`, `google/gemini` or `openai/gpt-` except `gpt-oss` (403 on our account); same output modalities; a superset of input modalities; `tools` for agent roles; reasoning that can be turned off (`none`) for judges. **OpenCode candidates:** same catalog, chat, tools, not `limited_regions` on Go.

**What it watches in Hermes:** `HERMES_DEFAULT_MODEL`, `HERMES_FALLBACK_MODEL`, `HERMES_AUDITOR_ORCHESTRATOR_MODEL`, `HERMES_AUDITOR_SYSTEM_MODEL` and `HERMES_AUDITOR_CONTENT_MODEL` (judges), `AUXILIARY_VISION_MODEL`, `OPENROUTER_IMAGE_MODEL`, and every agent cron job pinned to a model in any store. A value from a code default (env var unset) is never changed automatically: it needs a PR.

**Judges are never changed automatically:** `tasks/judge_provider_probe.py` (workspace) needs a real PR first.

A model that left the catalog is reported first, under "Ya no están en el catálogo".

## How an automatic change reaches production

Nothing restarts on its own.

1. The weekly run writes the upgrade to `/opt/data/model-watch/queue.json` (status `queued`) and
   reports it.
2. The next `scripts/deploy.sh` asks the pod (`python -m model_watch queue-env`), lists the
   variables before the confirmation prompt, and sets them in Coolify
   (`PATCH /applications/<uuid>/envs`) while Hermes is stopped. `--skip-model-queue` leaves them
   for a later deploy. A failed PATCH warns and the deploy goes on.
3. On boot, `03-biglobster-config` §5h runs `python -m model_watch apply-pins`. Once every variable of an entry holds the new model, the cron jobs pinned to the old model move to the new one and the entry becomes `applied`. If a variable holds a third value the entry becomes `stale`. Unpinned jobs simply follow the main model (upstream since 2026-09-16, `0469740ab3`; the old drift guard is gone).

The Coolify env endpoint (`PATCH /envs` with `{"key","value"}`) is used for the first time here
and was not exercised before merge. Check the first deploy that applies a queue.

In OpenCode mode an automatic change is written straight into `delegate.sh` (chain lines only).
Nothing is committed.

## One-time setup

(a) After the deploy, as `hermes` in the container:

```bash
hermes cron create "0 8 * * 1" --no-agent --script model_watch.sh --deliver telegram --name "Model watch — OpenRouter"
hermes cron run <id>
```

The first run reports everything current. Use the wrapper from CLAUDE.md "Never run hermes in the container as root".

(b) Mac schedule: not installed yet. It needs Brais's OK because it is persistent config.
Save as `~/Library/LaunchAgents/top.biglobster.model-watch.plist` (create `~/VSCODE/tasks/model-watch/` first):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>top.biglobster.model-watch</string>
  <key>StartCalendarInterval</key>
  <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/python3</string><string>-I</string><string>/Users/brais/VSCODE/hermes-sandbox/model_watch/watch.py</string><string>opencode</string></array>
  <key>StandardOutPath</key><string>/Users/brais/VSCODE/tasks/model-watch/opencode-latest.md</string>
</dict>
</plist>
```

Then `launchctl load ~/Library/LaunchAgents/top.biglobster.model-watch.plist` to enable it.

## First run, 2026-11-21 (dry run against the live config, approved by Brais)

**Hermes:** no automatic change. One "Avisar": `deepseek/deepseek-v4.1-flash` (alias, September snapshot) → `deepseek/deepseek-v4.1-flash-1017`. AA — → 32.6, ECI 157.01 → 159.41, $0.051/$0.203 → $0.150/$0.600 per M. Used by 9 sites; kept as is. The judges' `moonshotai/kimi-k2.6` has no AA index.

**OpenCode:** applied `qwen3.8-max` → `qwen4-max`, `gpt-6.1-sol` → `gpt-6.2-sol` and `kimi-k3` → `kimi-k3.1-pro`. Removed `inkling-2-small-free`, `minimax-m3.1-flash-free` and `glm-5.3-flash-free` (gone from Zen).

## Run it by hand

```bash
# In the pod (docker exec), as hermes: prints everything, writes nothing.
/usr/bin/env PATH=/command:/usr/local/bin:/usr/bin:/bin /command/with-contenv s6-setuidgid hermes \
  sh -c 'cd /opt/hermes && HOME=/opt/data .venv/bin/python -m model_watch hermes --dry-run'

# On the Mac.
python3 -I model_watch/watch.py opencode --dry-run
```

Tests: `tests/test_model_watch.py`.
