# Agent rental provisioning — runbook for Hermes

This is reference material for Hermes' own agent to follow when the CEO asks
(via Telegram) to set up agent rental for a bl-site-package client — e.g.
"set up agent rental for Francisco Nieto, gap-hunter + seo, site
https://blcliente.zeabur.app, panel password X, their OpenRouter key Y".

This is a **semi-automated** process: the CEO is always the trigger (no
biglobster.top wizard submission ever provisions anything by itself — there is
no payment gate on that wizard yet). Hermes executes the steps below once told.

Prerequisite for each order: the CEO has already confirmed the client actually
signed up (per `bl-site-package`'s own `ONBOARDING-INTERNO.md`/`FORMULARIO-CLIENTE.md`
flow) and has their site URL, panel password, and their own model API key
in hand.

## What each rented agent needs, and what it doesn't

Both agents below publish through the `bl_site_publish` tool
(`tools/bl_site_publish_tool.py`) — an authenticated HTTP client for the
client's own bl-site-package panel API, **not** git. There is no per-client
repo to check out, so unlike biglobster's own agents these jobs need no
`workdir`/checkout at all.

| Agent | Prompt file | Publishes via |
|---|---|---|
| Content Gap Hunter | `gap-hunter/bl-site-package-gap-hunter.prompt` | `create_blog_post` (draft) |
| SEO/GEO On-Site | `onsite-seo/bl-site-package-seo-agent.prompt` | `update_page_text` (direct) |

Infographic Engineer and Off-Site GEO Scout are **not yet adapted** —
bl-site-package's blog schema has no cover-image field (Infographic Engineer
would need a schema change first), and Off-Site GEO Scout is monitoring-only
and may not need the publish tool at all. Adapt them the same way, once a
client actually orders one.

## Steps

1. **Pick a profile slug** for the client — short, lowercase, e.g. `bl-cliente-nieto`.

2. **Create the profile** (empty, no clone — this client needs none of BigLobster's own skills/config):
   ```
   hermes profile create <slug> --no-skills
   ```

3. **Write a short SOUL.md** for the profile (`~/.hermes/profiles/<slug>/SOUL.md`), matching the terse style of `docker/profiles/grow-shop/SOUL.md` — scope, working boundaries, nothing more. This profile only ever touches this one client's site.

4. **Set the profile's `.env`** (`~/.hermes/profiles/<slug>/.env`, created by step 2, already `0o600`):
   ```
   BL_SITE_URL=https://<client's zeabur domain>
   BL_SITE_PANEL_PASSWORD=<client's panel password>
   OPENROUTER_API_KEY=<client's own key>          # BYOK — never BigLobster's own key
   ```
   Confirm the key actually works before moving on (a bad key means every cron run fails silently until someone notices).

5. **Create one cron job per agent the client ordered**, using the `cronjob` tool (not the CLI — this is a real registered tool):
   ```
   cronjob(action="create",
           name="Content Gap Hunter — <client>",
           prompt_source="gap-hunter/bl-site-package-gap-hunter.prompt",
           profile="<slug>",
           schedule="<daily, off-peak, staggered from other clients>")
   ```
   and the same pattern for `onsite-seo/bl-site-package-seo-agent.prompt` if ordered. Always set `prompt_source` (not just `prompt`) so this job stays covered by the prompt-drift detector (`incidents/sweep.py`) the same way biglobster's own jobs are.

6. **Confirm back to the CEO**: profile slug, job IDs created, and which agents are now active for this client.

## Removing a client

`hermes profile delete <slug>` after removing their cron jobs (`cronjob(action="remove", job_id=...)` for each). Confirm before deleting — this is destructive and not reversible.
