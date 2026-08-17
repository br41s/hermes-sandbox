# Runbook — Isolate Hermes into its own Zeabur project

Goal: move the Hermes service out of the shared BigLobster Zeabur project into a
dedicated project, **without losing the `/opt/data` volume** (profiles, sessions,
memory, config). Uses Zeabur's one-click **Project Migration** (copies the whole
project incl. volume data via S3), then prunes the unwanted service on each side.

> Why not just "add a new service from the GHCR image"? Because that gives you a
> fresh empty volume — you'd lose all profiles/sessions/memory. Project Migration
> carries the volume for you.

## Zeabur layout reminder
Dashboard → **Projects** → a project contains **Services** → each service has tabs:
**Deployments · Variables · Networking/Domains · Volumes · Settings**.
Today one project ("BigLobster") holds two services: the **web app** and **Hermes**.

---

## 0. Pre-flight (do NOT skip)
1. Confirm Hermes is healthy now: `curl https://blhermes.zeabur.app/health` → `{"status":"ok"}`.
2. Open the BigLobster project. Note the two services and which is Hermes.
3. On the **Hermes** service, open **Variables** and screenshot/save the full env list
   (you'll confirm it carried over later). Key ones expected:
   - `OPENROUTER_API_KEY`
   - `HERMES_DEFAULT_MODEL=owl-alpha`  ← our confirmed main model
   - `HERMES_DASHBOARD=1`
   - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`
   - `HERMES_DASHBOARD_INSECURE` (currently truthy → why the panel is open)
   - optional: `EXA_API_KEY`, `HUGGINGFACE_API_KEY`, `HERMES_CALLBACK_*`
4. On the Hermes service, note: the **Volume** mount path (`/opt/data`) and the
   **Domain** (`blhermes.zeabur.app`).

---

## 1. Copy the project ("Clone Project")
1. Open the **BigLobster project** → **Project Settings** → **Clone Project**
   ("Create a copy of this project in another region with all services and configurations").
2. Pick the target region (it clones to *another* region — expected; just note it).
3. Because there's a volume, Zeabur backs it up to S3 and restores it — **wait for
   completion** (time ∝ volume size). Result: a new project copy (both services + env
   vars + volume data).

---

## 2. Verify the copy — VOLUME DATA IS THE CRITICAL CHECK
The "Clone Project" button text says "all services and configurations" but does NOT
literally say *volume data*. Zeabur's changelog says data IS included, but VERIFY before
pruning. In the **new** project's Hermes (it gets its own temp URL):
- `/health` ok; `/api/status` reachable.
- `/api/profiles` (with the session token from its page HTML) lists **`grow-shop`** as
  well as `default` → **volume data transferred ✅**.
- If only `default` / empty → data did NOT transfer; STOP, keep the old project, and
  fall back to a manual volume copy (tar `/opt/data` via the container terminal).
- Also confirm **Variables** match your pre-flight list.
- Do **not** move the domain or delete anything until this passes.

---

## 3. Prune to one service per project
1. In the **NEW** project → delete the **web app** service (keep Hermes).
   Service → **Settings** → **Delete service**.
2. Rename the new project to **Hermes** (Project Settings → rename).
3. **Leave the OLD project's Hermes service alone for now** — it's your rollback until
   Step 5 passes. (Deleting a service deletes its volume — order matters.)

---

## 4. Rebind the domain
A domain can only point to one service, so:
1. OLD project → old Hermes service → **Networking/Domains** → remove `blhermes.zeabur.app`.
2. NEW project → Hermes service → **Networking/Domains** → add/bind `blhermes.zeabur.app`.
   - If Zeabur won't let you reuse it instantly, bind a temporary domain to the new
     Hermes, verify (Step 5), then cut the real domain over.

---

## 5. Verify the NEW Hermes
1. `curl https://blhermes.zeabur.app/health` → ok.
2. Panel `https://blhermes.zeabur.app` → `/api/status`: `gateway_running:true`,
   `gateway_platforms` shows `telegram: connected`.
3. Profiles present: `default` + `grow-shop`.
4. Send a Telegram message to `@b_l_hermes_bot` → it replies.
5. If the gateway didn't auto-start: NEW Hermes service → **Restart**.
6. If it pulls a private GHCR image and fails: re-add the **registry credentials**
   (GHCR PAT) on the new service — these may not carry in the copy.

---

## 6. Decommission the old Hermes
Only after Step 5 fully passes: OLD project → delete the **Hermes** service.
(Optionally rename the old project to **BigLobster**.) The BigLobster web app keeps running.

---

## 7. BigLobster side
- Domain unchanged → **no change needed**.
- If you assigned a new domain to Hermes → update `HERMES_PANEL_URL` on the BigLobster
  **web** service (Variables tab); the `/admin` redirect uses it.

---

## Do these as SEPARATE operations (don't combine)
- **This migration** and the **panel security lockdown** (OAuth + drop
  `HERMES_DASHBOARD_INSECURE`) are independent. Do one, verify, then the other — so a
  failure points to a single cause.
- The lockdown is config-only (set `HERMES_DASHBOARD_OAUTH_CLIENT_ID` +
  `HERMES_DASHBOARD_PUBLIC_URL`, remove `HERMES_DASHBOARD_INSECURE`, restart). See the
  security section.

## Rollback
At any point before Step 6, the OLD project's Hermes is intact — rebind the domain back
to it and you're where you started. Nothing is irreversible until Step 6.

---

## Troubleshooting: "Clone failed — Volume Copy: hermes failed (volume may be too large)"
Observed 2026-06-04: the `biglobster` volume cloned fine; the **hermes** volume failed
the S3 backup job for being too large. **Upgrading CPU/RAM does NOT help** — the limit is
on the backup job, not compute.

Fix = shrink `/opt/data` below the limit, then retry Clone. Diagnose via the Zeabur
service **Console/Terminal** (or the panel terminal):
```sh
du -sh /opt/data
du -sh /opt/data/* | sort -h
du -sh /opt/data/profiles/*/* 2>/dev/null | sort -h | tail -25
```
Safe to delete (Hermes regenerates these):
- profile **workspace repo clones** + their `node_modules` (usually the biggest)
- `.cache/`, Playwright/Chromium browser dirs, `__pycache__/`, pip/npm caches
- `logs/`
- leftover dirs of the deleted `hermes-*` profiles
- old **sessions** — prune via the panel Sessions page or `/api/sessions/prune`

Keep: `config.yaml`, `.env`, `SOUL.md`, `MEMORY.md`/`USER.md`, and each kept profile's
config/SOUL/memory.

### Manual fallback (if the volume is still too big after pruning)
Skip Clone entirely:
1. New project → add Hermes service from **prebuilt image** `ghcr.io/br41s/hermes-sandbox:latest`
   (+ GHCR registry creds). Let it boot once (creates a fresh `/opt/data`).
2. From the OLD Hermes terminal, tar ONLY the essentials:
   `tar czf /tmp/hermes-core.tgz -C /opt/data config.yaml .env SOUL.md memories profiles`
   (exclude each profile's `workspace*`, `.cache`, `sessions` if large).
3. Download it; upload + extract into the NEW service's `/opt/data`; restart.
   This carries config + profiles + memory; drops session/cache bloat by design.
