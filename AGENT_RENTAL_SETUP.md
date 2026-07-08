# Agent rental provisioning — runbook for Hermes

This is reference material for Hermes' own agent to follow when the CEO asks
(via Telegram) to set up agent rental for a bl-site-package client — e.g.
"set up agent rental for Francisco Nieto, gap-hunter + seo, site
https://blcliente.zeabur.app, panel password X, their OpenRouter key Y".

This is a **semi-automated** process: the CEO is always the trigger (no
biglobster.top wizard submission ever provisions anything by itself — there is
no payment gate on that wizard yet). Hermes runs `scripts/provision_bl_client.py`
once told; nothing calls it on its own.

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

Every bl-site-package customer's job for a given agent points at the *same*
prompt file above — there is no per-client copy, ever. Customer-specific
behavior (which site, which credentials) lives entirely in that customer's
Hermes profile, never in the prompt.

## Onboarding a client

Run `scripts/provision_bl_client.py` from the repo root:

```bash
python scripts/provision_bl_client.py \
  --slug bl-cliente-nieto \
  --client-name "Francisco Nieto" \
  --site-url https://blcliente.zeabur.app \
  --panel-password '<their panel password>' \
  --openrouter-key sk-or-v1-... \
  --agents gap-hunter,seo
```

`--agents` is a comma-separated list from `gap-hunter`, `seo`. What the script does, in order:

1. Validates the slug and checks a profile with that name doesn't already exist.
2. Calls the live OpenRouter API to confirm the client's key actually works — fails here instead of every cron run failing silently until someone notices.
3. `hermes profile create <slug> --no-skills` — an isolated `~/.hermes/profiles/<slug>/` (empty, no clone — this client needs none of BigLobster's own skills/config).
4. Writes that profile's `SOUL.md`, matching the terse style of `docker/profiles/grow-shop/SOUL.md` — scope, working boundaries, nothing more.
5. Writes that profile's `.env` (mode `0600`): `BL_SITE_URL`, `BL_SITE_PANEL_PASSWORD`, `OPENROUTER_API_KEY` (BYOK — never BigLobster's own key).
6. Creates one cron job per ordered agent, with `profile=<slug>` and `prompt_source=<the shared prompt file above>`, on a deterministic off-peak time staggered by client+agent so jobs don't all collide.

Confirm back to the CEO: profile slug, job IDs created (the script prints them as JSON), and which agents are now active for this client.

If the script isn't available and a step needs doing by hand, the underlying primitives are `hermes profile create`, writing `.env`/`SOUL.md` directly under the profile dir, and `cronjob(action="create", ..., prompt_source=...)`. Always set `prompt_source` (not just `prompt`) so the job stays covered by the prompt-drift detector (`incidents/sweep.py`).

## Editing a shared agent prompt

Edit the `.prompt` file once (e.g. `gap-hunter/bl-site-package-gap-hunter.prompt`).
That edit does **not** reach any customer's running job by itself — each cron
job stores its own frozen copy of the prompt text from creation time, and
`incidents/sweep.py` only *flags* the drift, it doesn't fix it. Propagate the
edit to every customer job built from that file in one pass:

```bash
python scripts/sync_prompt_drift.py --source gap-hunter/bl-site-package-gap-hunter.prompt --dry-run   # preview
python scripts/sync_prompt_drift.py --source gap-hunter/bl-site-package-gap-hunter.prompt --yes       # apply
```

## Removing a client

Still manual — this is destructive and not reversible, confirm before doing it:

```
cronjob(action="remove", job_id=...)   # for each of the client's jobs
hermes profile delete <slug>
```

## No auto-trigger from the panel (yet)

Nothing calls `provision_bl_client.py` automatically. The biglobster.top
wizard has no payment gate, so wiring an unauthenticated trigger to it would
let anyone provision a profile and cron jobs for free. Once a payment gate
exists, that's the point to wire an actual trigger (webhook on "order
confirmed" → `provision_bl_client.py`) instead of the CEO running the command
by hand.
