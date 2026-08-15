# Agent rental provisioning — runbook for Hermes

This is reference material for Hermes' own agent to follow when the CEO asks
(via Telegram) to set up agent rental for a bl-site-package client — e.g.
"set up agent rental for Francisco Nieto, gap-hunter + seo, site
https://blcliente.zeabur.app, panel password X, their OpenRouter key Y".

There are now **two triggers**, both landing on the same
`scripts/provision_bl_client.py` code path:

1. **CEO-triggered (semi-automated).** For rentals sold through the manual
   flow. The CEO has already confirmed the client signed up (per
   `bl-site-package`'s own `ONBOARDING-INTERNO.md`/`FORMULARIO-CLIENTE.md`) and
   has their site URL, panel password and their own model API key in hand. If
   the client ordered `onboarding-content` or `product-articles`, the CEO also
   needs their existing/old site URL.
2. **Payment-confirmed webhook (fully automatic).** For the *Site Launch*
   checkout product. BigLobster's Stripe side POSTs a confirmed order to
   `POST /api/bl/rental/provision` on this engine and provisioning runs with
   no human in the per-order loop. Contract in
   [Payment-confirmed provisioning webhook](#payment-confirmed-provisioning-webhook)
   below.

## What each rented agent needs, and what it doesn't

Both agents below publish through the `bl_site_publish` tool
(`tools/bl_site_publish_tool.py`) — an authenticated HTTP client for the
client's own bl-site-package panel API, **not** git. There is no per-client
repo to check out, so unlike biglobster's own agents these jobs need no
`workdir`/checkout at all. Website Maintenance additionally *reads* through
`bl_site_health` (`tools/bl_site_health_tool.py`), which is registered into the
same `bl_site_publish` toolset — read-only, so there is still exactly one write
path to a client's site.

| Agent | Prompt file | Publishes via | Schedule |
|---|---|---|---|
| Content Gap Hunter | `gap-hunter/bl-site-package-gap-hunter.prompt` | `create_blog_post` (draft) | daily |
| SEO/GEO On-Site | `onsite-seo/bl-site-package-seo-agent.prompt` | `update_page_text` (direct) | daily |
| Onboarding Content Agent | `onboarding-content/bl-site-package-onboarding-content.prompt` | both actions | once, 5m after provisioning |
| Product Article Agent | `product-articles/bl-site-package-product-articles.prompt` | `create_blog_post` (draft, with CTA) | daily |
| Infographic Engineer | `infographic/bl-site-package-infographic.prompt` | `update_blog_post` (inserts one inline SVG) | daily |
| Website Maintenance | `maintenance/bl-site-package-maintenance.prompt` | `update_page_text` / `update_blog_post` (repairs only) | daily |
| Site Launch | `site-setup/bl-site-package-site-setup.prompt` | both actions | once, 5m after provisioning |
| Social Shorts | `shorts/bl-site-package-shorts.prompt` | `update_blog_post` (sentinel only) | daily |

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

bl-site-package now supports AI images: blog posts have a cover
(`image_url`/`image_alt`) and each page has an image field. `gap-hunter` and
`onboarding-content` generate them via `image_generate` (client's own FAL key,
BYOK) and attach them with `bl_site_publish` `action: "upload_image"`. Image
generation is billed to the client's `FAL_KEY`; a client who didn't provide one
just gets text-only content (the agents never block on a missing image).

Social Shorts is the only agent whose deliverable is **not** on the client's
site. Each run turns one not-yet-processed blog post into 3–5 vertical MP4s for
Instagram Reels and TikTok plus a `captions.md` of per-network copy, written to
`workspace/shorts/<post-slug>/` in the client's own profile. Its only write to
the site is the `<!-- shorts:auto -->` sentinel appended to the post it just
used, which is how it knows never to redo one — the same
mark-it-in-the-content trick the Infographic Engineer uses, and the same
prose-is-immutable rule. See "Social Shorts" below.

Off-Site GEO Scout is **not yet adapted** — it's monitoring-only and may not
need the publish tool at all. Adapt it the same way, once a client orders it.

Every bl-site-package customer's job for a given agent points at the *same*
prompt file above — there is no per-client copy, ever. Customer-specific
behavior (which site, which credentials) lives entirely in that customer's
Hermes profile, never in the prompt.

## Website Maintenance — the bounded product

`maintenance` is the agent behind BigLobster's *"mantenimiento web"*
subscription SKU. It sits at a much higher price point than the single-purpose
content agents, so the substance has to be real; and, like Site Launch, it has
to be deliverable **without a human deciding per client what "maintenance"
means this month**. Both constraints point the same way: the *checking* is
deterministic code, and the *fixing* is a closed list.

**The check is code, not a prompt.** `tools/bl_site_health_tool.py` —
`bl_site_health(action="check")` — fetches the fixed five pages, the catalog,
the three legal pages, `robots.txt`, `sitemap.xml` and every published post,
then returns one JSON report:

| Measured | Why it is boundable |
|---|---|
| HTTP status + response time + byte weight per route | The route list is fixed by the product; a client cannot add a sixth page |
| Broken links, internal and outbound | Internal targets are a closed set (`valid_internal_routes` + real post slugs); everything else is checked by fetching it |
| **Publish drift** — posts the API calls `published` that 404 on the built site | Two lists compared; a mismatch means `src/build/rebuild.js` didn't run |
| TLS certificate days remaining | Socket read, fixed 21-day warning threshold |
| Security headers vs. the set `src/server.js` sets | Fixed expected list; a missing HSTS means the instance isn't running `NODE_ENV=production` |
| Sitemap `<loc>` count vs. the `site_url` config key | Eleventy emits an empty sitemap when `site_url` is blank — silently uncrawlable |
| Images with no `alt`, or hotlinked from another host | Attribute presence, not taste |
| Empty page-text / legal / business config fields | Fixed field lists, reported never filled |
| **Contact endpoints** — WhatsApp/phone digit count, e-mail syntax, `biz_facebook`/`biz_instagram` still resolving | Format arithmetic (E.164 bounds, one regex) plus an HTTP fetch. Only status 0/404/410 counts as dead — Meta and LinkedIn answer 403/429/999 to a datacenter IP, and calling that dead would be a monthly false positive |
| **Duplicate / thin content** — near-identical `<title>` or meta description across the fixed pages + posts | `difflib` character ratio against a fixed 0.90 threshold, and a fixed 50-char floor for descriptions. String similarity, never meaning |
| **JSON-LD validity** — parse every block, required schema.org fields present and correctly typed | The required-field table comes from what `src/content/structured-data.js` itself emits; "does this parse / is `datePublished` there / is `acceptedAnswer.text` non-empty" has one answer |
| **Old-site paths that now dead-end** (only with `OLD_SITE_URL`) | The old site's own sitemap, or one level of homepage links, capped at 40 — then a fetch each. No crawling heuristics, no depth |
| **Contact form actually accepts and stores a submission** (monthly) | One synthetic POST to the public form, then read the panel inbox back and look for the marker. Boolean, not judgment |
| **Release drift** — the instance's deployed version vs the latest released on `main` | `GET /api/site/status` version against `package.json` on GitHub; **any** mismatch counts (ahead of main = deployed outside the release flow), and either side degrades to `null` instead of failing the run |
| 30-day uptime rollup and `report_due` | From `$HERMES_HOME/bl_site_health_history.json`, per profile |

The checks in bold were added after the first version shipped. All of them are
**report-only**: none extends the fix list. That is deliberate in each
case — the social/contact fields are `biz_*`/`whatsapp_number` (already
forbidden to the agent), rewriting a duplicate title is content work, hand-
authoring correct JSON-LD for a broken page is judgment rather than a mechanical
edit, a redirect is web-server configuration the panel API cannot write, and an
outdated instance is a redeploy BigLobster performs — the drift notice routes to
BigLobster through the cron's delivery channel, and the client is never told
"your site is old".

**The fix list is closed — five items, max five applications per run:** rewrite
a broken internal link to a valid route (or unlink it), unlink a dead outbound
link, write missing image `alt` text, re-host a hotlinked image through
`upload_image` (the site re-encodes to WebP), and set `site_url` when the
sitemap is empty. Everything else is a *notice*, not a fix. The prompt states
that explicitly, so "my site should also do X" surfaces as a line in the report
instead of quietly becoming bespoke work — the same boundary Site Launch draws.

**What it deliberately does not promise**, because it cannot deliver it:

- **Not 24/7 uptime monitoring.** One cron run per day is one sample per day.
  The report says "availability across the checks performed", never "99.9%
  uptime". Real minute-level monitoring would be an external pinger, not an
  LLM job. Sales copy must match this.
- **Not dependency or security patching.** Every client shares one
  bl-site-package codebase; updating is a redeploy BigLobster performs, not a
  per-instance operation. The agent cannot patch and must not claim to — it
  *detects* an outdated instance (the release-drift check) and notifies
  BigLobster, never the client.
- **No image compression work of its own** beyond re-hosting: uploads are
  already re-encoded to WebP server-side by `optimizeToWebp`.

**The monthly report is not a second cron job.** `bl_site_health` returns
`report_due: true` once per calendar month; the same daily run produces the
report and then calls `action="record_report"` to stamp it. Two jobs would race
for that state and could double-send.

**The contact-form probe is the tool's one write, and it is gated harder than
the report.** It posts a single synthetic message to the *public*
`POST /api/contact` — the same call any visitor makes, touching no config, no
page and no post — then reads `GET /api/contact` back to confirm it persisted.
Because that submission sends a real e-mail through the client's SMTP, it runs
once per calendar month, only when every route answered 200, and the attempt is
stamped into history **before** the POST rather than after. A crash between
sending and verifying therefore costs one month's result, never a second e-mail
in the client's inbox. The prompt forbids the agent from firing it by hand.

What the probe proves is narrower than it looks, and the report must say so:
`src/api/contact.js` catches every SMTP error into `console.error` and answers
`{success: true}` regardless, so a configured mailer that fails at send time is
invisible from outside the instance. The probe therefore confirms the form
**receives and stores** a lead — catching a 500, a broken rate limiter or a
regressed route — and echoes the `smtp_configured` / `notify_email_configured`
booleans from `GET /api/site/status`, which separates "presumably sent" from
"certainly never sent" (the common failure). `email_delivery_verified` stays
`false`; closing the rest of the gap is item (3) below.

### Prerequisites in bl-site-package — (2) shipped; (1) and (4) not built

Four things the SKU would be better with. (2) shipped as bl-site-package PR #32
and the health tool consumes it; (3) is half-closed by the same endpoint. The
rest stay blocked on a bl-site-package change — same treatment as the
`cta_url`/`cta_label` migration the Product Article Agent needs: ship them
there first, then the agent can use them.

1. **`POST /api/site/notify`** (authenticated), body
   `{"subject": "...", "body_markdown": "..."}` → sends through the instance's
   already-configured SMTP to its `notify_email`, returns
   `{"success": true}` or `{"error": "smtp_not_configured"}`. **Until this
   exists the monthly report reaches the CEO through the job's delivery target
   and the CEO forwards it** — the client does not get it automatically. Must
   be rate-limited (a handful per day) so a leaked panel password can't turn a
   client's site into a mailer.
2. **`GET /api/site/status`** (authenticated) — **shipped (bl-site-package
   PR #32) and consumed.** Returns `{version, built_at, last_build_ok,
   smtp_configured, notify_email_configured, posts, last_contact_email}` —
   **presence booleans only, never the credentials**. Of the three checks it
   was scoped to unlock, two are live in `bl_site_health`: release drift
   (the `release` section — deployed version vs `main`'s `package.json`, an
   instance so old it 404s the endpoint reported as `deployed: null`) and the
   mail-configuration booleans echoed into `form_check`. `last_build_ok` /
   `built_at` are not consumed yet — a failed background rebuild is still
   inferred from publish drift.

3. **Contact-form delivery confirmation — half-closed by (2).** The monthly
   probe (above) proves the form persists a lead; it cannot prove the
   notification e-mail arrived, because `src/api/contact.js` swallows every
   SMTP error into `console.error` and still answers `{success: true}`. Two
   fields close it, cheapest first:
   - the `smtp_configured` / `notify_email_configured` booleans of
     `GET /api/site/status` — **shipped and consumed**: `form_check` echoes
     them, turning "we cannot see the mailer" into "the mailer is not
     configured at all", which is the common failure.
   - the `last_contact_email` field on that same endpoint —
     `{"at": "...", "ok": false, "error": "EAUTH"}`, written by the existing
     `catch` block. **Presence and an error *class* only, never the SMTP
     credentials or the recipient.** Shipped in the endpoint but **not
     consumed yet**: reading it after the probe would need care with send
     timing (the mail is sent asynchronously, so an immediate read can race
     it). This is the version that actually answers the question.

   Until `last_contact_email` is consumed, `form_check.email_delivery_verified`
   stays `false` and the report asks the client to confirm the test message
   reached their inbox — unless the booleans already prove it could not have.

4. **Backup verification — no mechanism exists to verify.** This was scoped as
   a check and turned out not to be buildable: bl-site-package has **no backup
   subsystem at all**. There is no scheduled dump, no snapshot API, no
   `GET /api/site/backup`, and nothing in the schema records a backup ever
   having run. The only backup that exists anywhere in the product is a manual
   human step in `RELEASE.md` §2 — *"copy `data/` outside the document root
   before pulling"* — performed once, by hand, during a Plesk deploy. On Zeabur
   the DB is a volume whose snapshots (if any) live in Zeabur's control plane,
   which a client instance cannot query and the maintenance profile has no
   credentials for.

   So there is nothing to verify and nothing to restore-test, and an agent
   "confirming backups" today would be reporting on a mechanism that does not
   exist. What has to ship first, in bl-site-package, is the backup itself:
   a scheduled dump of `data/app.db` (SQLite `VACUUM INTO`, which is
   consistent under WAL) plus `data/uploads/` to storage outside the instance,
   writing a `backups` row — `{at, size_bytes, sha256, destination, ok}` — and
   exposing the last N through the authenticated `GET /api/site/status` in (2).
   Only then does the check become the mechanical thing it was scoped as:
   *did a backup run inside the expected window, is it non-empty, and does its
   checksum match*. Restore-testing is a further step again and probably never
   belongs to a per-client agent — restoring into the live instance to prove a
   backup works is exactly the operation you do not want automated.

None of the four is required to run the SKU as specified above. (2) has shipped
and made the SKU materially better; the rest still would. (1) is what turns the
monthly report into something the client receives rather than something the CEO
relays, and finishing (3) is the difference between "your contact form works"
and "your contact form stores messages, and we hope the e-mail goes out".

## Site Launch — the bounded product

`site-setup` is the agent behind BigLobster's *"crea o moderniza tu web"*
one-shot checkout SKU. It exists because a fixed-price checkout item may not be
custom-scoped project work — a merchant-of-record can only be seller-of-record
for a **product delivered without a human negotiating scope**. The old
"Proyecto puntual" SKU was pulled for exactly that reason. So the boundary has
to be real, not a relabel of manual work. Where it is drawn:

**What the buyer chooses:** the values in a fixed questionnaire. Nothing else.
The schema is `scripts/bl_site_setup.py` — `company_name`, `sector` (one of
seven enum options, never free text), `notify_email`, optional `logo_url`,
`whatsapp_number`, the `legal_*` and `biz_*` identity fields, and an optional
`old_site_url` to draw source material from. An unknown key is a hard failure,
not a silent drop: an extra field means BigLobster's form and this schema
drifted, and the buyer paid for data that would otherwise be discarded.

**What the buyer does not choose:** everything else. bl-site-package has a
**fixed five-page structure** (inicio, quiénes somos, servicios, contacto,
blog), a fixed theme, fixed typography, and — checked in the source — no brand
colour tokens at all. There is no page menu to pick from, because there is no
mechanism for a sixth page. Two buyers get byte-identical structure; only the
values differ. That is what makes it a template product rather than a brief.

**Where the human would have been, and what replaced them:**

| Step | Who does it | How it stays judgment-free |
|---|---|---|
| Deploy a blank instance | nobody, per order | claimed from a pre-deployed pool — see below |
| Complete `/setup` | `scripts/bl_site_setup.py` | form field → config key, deterministic code |
| Identity / legal / business data | `scripts/bl_site_setup.py` | copied **verbatim**; no model in the path |
| Logo | `scripts/bl_site_setup.py` | fetched from the buyer's URL, signature-checked |
| Page copy + one launch post | the `site-setup` cron job | may write **only** the fixed `page_*` field list |

The split matters: the site *build* is 100% deterministic code, and the model
is confined to prose inside a field list it cannot extend. The prompt states
that explicitly — if source material implies a new page, a custom section or a
design change, the agent must refuse and log it as "fuera del alcance del
producto" in its report, so an out-of-scope request surfaces as a line in the
CEO's report instead of quietly becoming bespoke work.

`site-setup` and `onboarding-content` are **mutually exclusive** — both do the
initial page fill and ordering both would have two one-shot jobs racing to
write the same fields. `site-setup` needs no `--old-site-url`: a buyer with no
previous site is the normal case, and the prompt covers it by writing from the
buyer's real data plus sector context, never invented facts.

### The blank-instance pool

A paid checkout cannot wait for someone to click through Zeabur, so blank
bl-site-package instances are deployed **ahead of demand** and claimed per
order. The inventory lives in `~/.hermes/bl_site_instances.json`:

```json
{"instances": [
  {"site_url": "https://bl-blank-01.zeabur.app", "status": "free"},
  {"site_url": "https://bl-blank-02.zeabur.app", "status": "free"}
]}
```

The webhook claims the first `free` entry, marks it `claimed` with the order
id, and releases it again if provisioning fails (so a transient failure cannot
drain the pool one retry at a time). An empty pool bounces the order with
`503 no_instance_available` and pings the CEO — the buyer has paid, so this
must never be silent. The CEO also gets a low-stock warning at ≤2 free.

This is the honest shape of the remaining human work: keeping the pool
stocked. It is **not per-order and not per-customer** — it's restocking
inventory, the same category as a SaaS adding capacity — which is why it
doesn't reintroduce the "human negotiates scope per sale" problem. Custom
domain setup (the buyer pointing their own DNS at the instance) also stays
manual and remains **outside** the SKU: the product delivers a working site on
its own URL.

## Social Shorts — the bounded product

`shorts` is the agent behind BigLobster's short-form video subscription. The
thing that makes it sellable is that **the marginal cost of a video is zero**,
so the margin does not erode with volume:

| Piece | Where it comes from | Cost |
|---|---|---|
| Script | the client's own blog post, via `bl_site_publish` | tokens only |
| Voiceover | `text_to_speech`, default provider Edge TTS | free, no key |
| B-roll | Pexels Videos API, portrait orientation | free (200 req/h, 20k/month) |
| Assembly, captions, loudness | `shorts_render` → ffmpeg, in-container | free |
| *Optional* 4s AI opening hook | existing `video_generate`, fal provider | client's own `FAL_KEY` |

Two licence facts are load-bearing here and should not be quietly changed.
**Edge TTS or Kokoro (Apache-2.0) only** — XTTS-v2 ships under Coqui's CPML and
F5-TTS's published weights are CC-BY-NC-4.0, so both are non-commercial and
cannot legally voice a customer deliverable. And **Pexels clips are used only as
raw material** — its licence permits commercial use and modification but not
redistributing footage unaltered, which is why the renderer never hands a clip
through untouched.

`PEXELS_API_KEY` is the one credential in the whole rental flow that is **not
BYOK**: the API is free, so every profile carries a copy of BigLobster's own
key. Rotating it is safe — it is in the `inject` allowlist in
`docker/cont-init.d/03-biglobster-config`, so every container boot overwrites the
value in each profile's `.env` from the Zeabur env. That is the same mechanism
that stops a rotation stranding profiles on a revoked key, which is exactly what
broke `grow-shop` in the 2026-06-05 `OPENROUTER_API_KEY` rotation. Set it once on
the Hermes service and restart the container; do **not** hand-edit profile `.env`
files. `--pexels-key` at provision time exists so a brand-new client works
immediately, before the next boot. Provisioning without it does not fail; the
videos just render on plain backgrounds instead of footage.

Note the quota is shared across the whole fleet, not per client: 200 requests per
hour and 20,000 per month, against roughly three `stock_search` calls per video.
`stock_search` surfaces a Pexels 429 as an explicit error rather than an empty
result, so throttling shows up in the job report instead of silently producing
background-only videos.

What keeps the SKU bounded:

| Bound | Enforced by |
|---|---|
| One blog post per run, 3–5 videos | the prompt; the `<!-- shorts:auto -->` sentinel makes it idempotent |
| 60 seconds and 12 scenes per video | `plugins/shorts/render.py`, as a hard error rather than a silent trim |
| 1080x1920, 30fps, −14 LUFS | fixed in the renderer; not a per-client decision |
| Never posts to a social network | no credentials exist, and the prompt forbids seeking them |
| Claims come from the post only | the prompt; no invented facts, no medical/legal/financial promises |

The agent goes `[SILENT]` once every post carries the sentinel, so it is safe to
leave scheduled daily on a small blog. Its report is Spanish like the rest of
the fleet, but the **videos and their captions are English** — that was a
deliberate reach for an international Reels/TikTok audience, not an oversight.

Delivery is the folder. A rented profile's `.env` holds only that client's site
credentials — no bot token — so the prompt's `send_message` media hand-off is
conditional and silently skipped there; it is what makes the same prompt useful
when the CEO runs it against BigLobster's own profile, which does have Telegram.

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
  --fal-key <key_id>:<key_secret> \
  --agents gap-hunter,seo,onboarding-content,product-articles \
  --old-site-url https://their-old-site.example.com
```

`--agents` is a comma-separated list from `gap-hunter`, `seo`,
`onboarding-content`, `product-articles`, `infographic`, `maintenance`,
`site-setup`, `shorts`. `shorts` (the Social Shorts subscription) requires no
extra flags either, but takes `--pexels-key` for its stock footage — shared, not
BYOK, and defaulting to `PEXELS_API_KEY` in the environment. It also *uses*
`--fal-key` when one is present, for the optional AI opening hook.
`maintenance` (the Website Maintenance subscription) requires no
extra flags. It *optionally* uses `--old-site-url`: when the profile carries
`OLD_SITE_URL`, the daily check also sweeps the old site's published paths and
reports the ones that now dead-end on the new site. Omit it and that one
section of the report reads "no aplica"; nothing else changes.
`site-setup` (the Site Launch product) additionally requires
`--questionnaire path/to/answers.json` — the buyer's structured form answers,
schema in `scripts/bl_site_setup.py` — and applies the fixed site template
deterministically before any job is scheduled. It cannot be combined with
`onboarding-content`. `--old-site-url` is required if
`onboarding-content` and/or `product-articles` is ordered — omit both if the
client has no existing site. `--model` sets the profile's base/orchestrator
model (defaults to `deepseek/deepseek-v4-flash`, the cheap orchestrator, billed
to the client's own key); override it only if a client wants a different model.
`--fal-key` is the client's own FAL key (BYOK) for image generation — blog
covers and page images are billed to it. Omit it if the client didn't order
image generation; the agents then publish text-only and never block on a
missing image. The FAL image model is taken from `--image-model`, else the
client's panel choice (`GET /api/site/config` `image_model`), else the FAL
default. What the script does, in order:

1. Validates the slug and checks a profile with that name doesn't already exist.
2. Calls the live OpenRouter API to confirm the client's key works **and** that the chosen `--model` is callable on it — both *before* the profile/jobs exist, so a broken key or bad model id fails here instead of every cron run failing silently. (The one-shot `onboarding-content` job auto-removes after its single run, so a first run that 400s can't be re-run by id — validating up front is the only safe order.) If `--fal-key` is given, it's validated against FAL here too (a free token-exchange call, no image generated).
2b. If `site-setup` was ordered: validates the questionnaire against the fixed schema, then applies the site template to the instance (`scripts/bl_site_setup.py`) — completes `/setup`, writes the identity/legal/business fields verbatim, uploads the logo. This runs *before* the profile is created so a failure here (unreachable instance, instance already claimed under another password) leaves no half-built profile behind; it is idempotent, so a retry converges.
3. `hermes profile create <slug> --no-skills` — an isolated `~/.hermes/profiles/<slug>/` (empty, no clone — this client needs none of BigLobster's own skills/config).
4. Writes that profile's `SOUL.md`, matching the terse style of `docker/profiles/grow-shop/SOUL.md` — scope, working boundaries, nothing more.
5. Writes that profile's `config.yaml` with `model.default`/`model.provider: openrouter`, plus `image_gen.model` when a FAL image model was resolved — **without the base model the profile has no model and every cron run 400s with `No models provided`** (the Shoroban bug). Only these blocks are written; all other config is deep-merged from defaults at runtime.
6. Writes that profile's `.env` (mode `0600`): `BL_SITE_URL`, `BL_SITE_PANEL_PASSWORD`, `OPENROUTER_API_KEY` (BYOK — never BigLobster's own key), plus `FAL_KEY` if `--fal-key` was given and `OLD_SITE_URL` if `--old-site-url` was.
7. Creates one cron job per ordered agent, with `profile=<slug>` and `prompt_source=<the shared prompt file above>`. `gap-hunter`/`seo` get a deterministic off-peak daily time staggered by client+agent; `onboarding-content` and `site-setup` get a one-shot run 5 minutes out instead.

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

## Payment-confirmed provisioning webhook

`hermes_cli/bl_rental_webhook.py`, mounted on the Hermes dashboard server
(`blhermes.zeabur.app`). This is the trigger this document used to defer until
a payment gate existed.

**BigLobster owns the Stripe integration.** Stripe never calls this endpoint;
BigLobster's own Node side does, after it has confirmed the order. So the auth
here is a shared secret, not a Stripe signature.

### Endpoint

```
POST https://blhermes.zeabur.app/api/bl/rental/provision
Content-Type: application/json
X-BL-Timestamp: <unix seconds>
X-BL-Signature: sha256=<hex>
```

`X-BL-Signature` is `HMAC-SHA256(BL_RENTAL_WEBHOOK_SECRET, "<X-BL-Timestamp>." + <raw request body>)`,
hex-encoded. Sign the **raw bytes you send** — re-serialising the JSON on
either side changes the digest. Requests more than **300 s** away from the
signed timestamp are rejected, so the signature cannot be replayed later.

The secret is a credential: it lives in the Hermes `.env` as
`BL_RENTAL_WEBHOOK_SECRET`, and in BigLobster's own env. **With no secret set
the endpoint refuses every request** rather than running open — it can never
fail into an unauthenticated provisioning API.

Node reference for the caller:

```js
const raw = JSON.stringify(order);
const ts = Math.floor(Date.now() / 1000).toString();
const sig = crypto.createHmac("sha256", process.env.BL_RENTAL_WEBHOOK_SECRET)
                  .update(ts + "." + raw).digest("hex");
await fetch(url, { method: "POST", body: raw, headers: {
  "Content-Type": "application/json",
  "X-BL-Timestamp": ts,
  "X-BL-Signature": `sha256=${sig}`,
}});
```

### Request body

```json
{
  "order_id": "cs_live_a1b2c3",
  "slug": "bl-cliente-garcia",
  "client_name": "Fontanería García",
  "openrouter_key": "sk-or-v1-...",
  "agents": ["site-setup", "gap-hunter"],
  "questionnaire": {
    "company_name": "Fontanería García",
    "sector": "Instalaciones",
    "notify_email": "hola@garcia.example",
    "logo_url": "https://.../logo.png",
    "legal_name": "García e Hijos SL",
    "legal_id": "B12345678",
    "biz_city": "Vigo"
  },
  "site_url": null,
  "panel_password": null,
  "fal_key": "<key_id>:<key_secret>",
  "old_site_url": null,
  "image_model": null
}
```

| Field | Required | Notes |
|---|---|---|
| `order_id` | yes | The idempotency key. Use the Stripe checkout session id. |
| `slug` | yes | Hermes profile name. Must be unused. |
| `client_name` | yes | Display name only. |
| `openrouter_key` | yes | The buyer's own key (BYOK). Validated live before anything is created. |
| `agents` | yes | Any of `site-setup`, `gap-hunter`, `seo`, `onboarding-content`, `product-articles`, `infographic`, `maintenance`, `shorts`. `site-setup` and `onboarding-content` are mutually exclusive. |
| `questionnaire` | with `site-setup` | Fixed schema — see `scripts/bl_site_setup.py`. Unknown keys are rejected. |
| `site_url` | no | Omit to claim a blank instance from the pool. Pass one only when BigLobster already knows the instance. |
| `panel_password` | no | Omit and one is generated and **returned** — email it to the buyer. |
| `fal_key` | no | The buyer's own FAL key for image generation. Omit → text-only content. With `shorts`, it also buys the optional AI opening hook. |
| `old_site_url` | required for `onboarding-content` / `product-articles` | The buyer's existing site. |
| `image_model` | no | Pins the FAL image model. |

### Responses

`200` — provisioned:

```json
{
  "status": "provisioned",
  "order_id": "cs_live_a1b2c3",
  "profile": "bl-cliente-garcia",
  "site_url": "https://bl-blank-01.zeabur.app",
  "panel_url": "https://bl-blank-01.zeabur.app/panel",
  "panel_password": "generated-or-echoed",
  "jobs": [{"job_id": "...", "name": "...", "schedule": "...", "source": "..."}],
  "site_setup": {"setup_completed": true, "fields_written": ["..."], "logo": "/uploads/logo.png"}
}
```

A **retried webhook for an already-provisioned `order_id` returns the exact
same body** with `"idempotent_replay": true` — same profile, same panel
password (a fresh one would lock out a buyer who was already emailed the
first). Nothing is created twice.

Failures are `{"status": "failed", "code": ..., "detail": ..., "order_id": ...}`.
The status code tells the caller whether to retry:

| Code | HTTP | Meaning | Retry? |
|---|---|---|---|
| `unauthorized` | 401 | Bad/missing/stale signature | no — fix the signing |
| `not_configured` | 503 | `BL_RENTAL_WEBHOOK_SECRET` unset on the engine | after the CEO sets it |
| `invalid_order` | 400 | Malformed payload, unknown agent, missing `old_site_url` | no |
| `invalid_questionnaire` | 400 | Schema violation — unknown field, free-text sector, missing required | no |
| `invalid_api_key` | 400 | The buyer's OpenRouter/FAL key is rejected, or the model isn't callable on it | after the buyer supplies a new key |
| `slug_collision` | 409 | A profile with that slug already exists (different order) | no — pick another slug |
| `site_already_claimed` | 409 | The instance is already configured under a different password | no |
| `no_instance_available` | 503 | The blank-instance pool is empty | yes, once restocked |
| `site_unreachable` / `site_setup_failed` | 502 | The instance didn't answer, or a write failed | yes |
| `internal` | 500 | Unexpected | yes, then check the CEO's Telegram |
| *(in flight)* | 409 | `{"status": "in_progress"}` — a concurrent duplicate of the same order | yes, later |

**Every terminal outcome pings the CEO on Telegram** (success, failure, and a
low-stock warning at ≤2 free instances). A paid order that fails must never be
silent, and the HTTP response alone doesn't reach a human.

Order state is durable at `~/.hermes/bl_rental_orders.json` (mode `0600` — it
holds panel passwords). A previously *failed* order is allowed to retry from
scratch; a *provisioned* one never re-runs.

### Wiring checklist for the BigLobster side

1. Generate a secret, set `BL_RENTAL_WEBHOOK_SECRET` in **both** the Hermes
   Zeabur env and BigLobster's.
2. Deploy blank bl-site-package instances and register them in
   `~/.hermes/bl_site_instances.json`.
3. On `checkout.session.completed` for the Site Launch SKU, POST the body
   above with the buyer's questionnaire answers.
4. On `200`, email the buyer `panel_url` + `panel_password`.
5. On a `4xx`, do **not** retry the same body — the code says what to fix.
6. On a `5xx`, retry with the same `order_id`; idempotency makes that safe.
