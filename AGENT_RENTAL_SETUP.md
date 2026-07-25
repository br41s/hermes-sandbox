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
in hand. If the client ordered `onboarding-content` (below), the CEO also
needs their existing/old site URL — `FORMULARIO-CLIENTE.md` asks for this.

## What each rented agent needs, and what it doesn't

Both agents below publish through the `bl_site_publish` tool
(`tools/bl_site_publish_tool.py`) — an authenticated HTTP client for the
client's own bl-site-package panel API, **not** git. There is no per-client
repo to check out, so unlike biglobster's own agents these jobs need no
`workdir`/checkout at all.

| Agent | Prompt file | Publishes via | Schedule |
|---|---|---|---|
| Content Gap Hunter | `gap-hunter/bl-site-package-gap-hunter.prompt` | `create_blog_post` (draft) | daily |
| SEO/GEO On-Site | `onsite-seo/bl-site-package-seo-agent.prompt` | `update_page_text` (direct) | daily |
| Onboarding Content Agent | `onboarding-content/bl-site-package-onboarding-content.prompt` | both actions | once, 5m after provisioning |
| Product Article Agent | `product-articles/bl-site-package-product-articles.prompt` | `create_blog_post` (draft, with CTA) | daily |

Onboarding Content Agent is the odd one out: it's a **one-shot** job, not a
recurring daily job like the other two. It runs once, scans the client's old
site (`--old-site-url`), and bulk-populates the new site's blank pages —
existing pages already filled by the client via `/setup` are left untouched.
Order it only when the client actually has an old site to migrate content
from; there's nothing for it to do otherwise.

Product Article Agent is for clients whose real storefront is still a
distributor-hosted catalog (`--old-site-url`, same flag as onboarding-content)
that they can't sell from directly (e.g. Shoroban's Grupo Solutex catalog
pages) — it crawls that catalog for individual product pages, skips ones it's
already written about (`bl_site_publish(action="list_posts")`, which sees
drafts too, not just published posts), and writes up to 3 new product
articles per run (description, specs, usage tutorial, comparison with real
catalog products, FAQ, CTA button back to the original product page) until
the catalog is covered, then goes quiet (`[SILENT]`). Needs
`bl-site-package`'s blog schema to support `cta_url`/`cta_label` and markdown
content — ship that migration first if a client's site predates it.

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

Run `scripts/provision_bl_client.py` from the repo root, using the repo's
venv Python (the bare `python3` on PATH won't have PyYAML and other deps
this pulls in via `cron/jobs.py`):

```bash
.venv/bin/python3 scripts/provision_bl_client.py \
  --slug bl-cliente-nieto \
  --client-name "Francisco Nieto" \
  --site-url https://blcliente.zeabur.app \
  --panel-password '<their panel password>' \
  --openrouter-key sk-or-v1-... \
  --agents gap-hunter,seo,onboarding-content,product-articles \
  --old-site-url https://their-old-site.example.com
```

`--agents` is a comma-separated list from `gap-hunter`, `seo`,
`onboarding-content`, `product-articles`. `--old-site-url` is required if
`onboarding-content` and/or `product-articles` is ordered — omit both if the
client has no existing site. `--model` sets the profile's base/orchestrator
model (defaults to `deepseek/deepseek-v4-flash`, the cheap orchestrator, billed
to the client's own key); override it only if a client wants a different model.
What the script does, in order:

1. Validates the slug and checks a profile with that name doesn't already exist.
2. Calls the live OpenRouter API to confirm the client's key works **and** that the chosen `--model` is callable on it — both *before* the profile/jobs exist, so a broken key or bad model id fails here instead of every cron run failing silently. (The one-shot `onboarding-content` job auto-removes after its single run, so a first run that 400s can't be re-run by id — validating up front is the only safe order.)
3. `hermes profile create <slug> --no-skills` — an isolated `~/.hermes/profiles/<slug>/` (empty, no clone — this client needs none of BigLobster's own skills/config).
4. Writes that profile's `SOUL.md`, matching the terse style of `docker/profiles/grow-shop/SOUL.md` — scope, working boundaries, nothing more.
5. Writes that profile's `config.yaml` with `model.default`/`model.provider: openrouter` — **without this the profile has no base model and every cron run 400s with `No models provided`** (the Shoroban bug). Only the model block is written; all other config is deep-merged from defaults at runtime.
6. Writes that profile's `.env` (mode `0600`): `BL_SITE_URL`, `BL_SITE_PANEL_PASSWORD`, `OPENROUTER_API_KEY` (BYOK — never BigLobster's own key), plus `OLD_SITE_URL` if given.
7. Creates one cron job per ordered agent, with `profile=<slug>` and `prompt_source=<the shared prompt file above>`. `gap-hunter`/`seo` get a deterministic off-peak daily time staggered by client+agent; `onboarding-content` gets a one-shot run 5 minutes out instead.

Confirm back to the CEO: profile slug, job IDs created (the script prints them as JSON), and which agents are now active for this client.

If the script isn't available and a step needs doing by hand, the underlying primitives are `hermes profile create`, setting the base model with `hermes -p <slug> config set model.default deepseek/deepseek-v4-flash` + `config set model.provider openrouter` (**don't skip this — a profile with no `config.yaml` has no model and every cron run 400s**), writing `.env`/`SOUL.md` directly under the profile dir, and `cronjob(action="create", ..., prompt_source=...)`. Always set `prompt_source` (not just `prompt`) so the job stays covered by the prompt-drift detector (`incidents/sweep.py`).

## Editing a shared agent prompt

Edit the `.prompt` file once (e.g. `gap-hunter/bl-site-package-gap-hunter.prompt`).
That edit does **not** reach any customer's running job by itself — each cron
job stores its own frozen copy of the prompt text from creation time, and
`incidents/sweep.py` only *flags* the drift, it doesn't fix it. Propagate the
edit to every customer job built from that file in one pass:

```bash
.venv/bin/python3 scripts/sync_prompt_drift.py --source gap-hunter/bl-site-package-gap-hunter.prompt --dry-run   # preview
.venv/bin/python3 scripts/sync_prompt_drift.py --source gap-hunter/bl-site-package-gap-hunter.prompt --yes       # apply
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
