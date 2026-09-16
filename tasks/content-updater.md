# Content Updater Agent — planning

Status: **planning, nothing built.** Step 1 of the plan: use-case catalogue.
Target: build → run on biglobster.top → sell as a bl-site-package rental.

## What makes this agent different from every other rental agent

Every existing rental agent is **additive**: gap-hunter creates a new post,
product-sheets writes a sheet that did not exist, infographic *inserts* a block
and is forbidden from touching a single word of existing prose, maintenance
explicitly refuses to rewrite text (`duplicate_content` → "Avisar — NUNCA
reescribas el texto") and refuses to invent legal data (`empty_legal_fields` →
"NUNCA los inventes").

The Content Updater is the first agent whose **entire job is mutating prose that
is already live**. Every guard in this codebase was designed for additive agents.
They do not transfer. See "Structural tensions" below.

---

## Use-case catalogue

Risk tiers:
- **T0** — mechanical, verifiable, no judgement. Safe to auto-apply.
- **T1** — editorial judgement, reversible in practice, commercially harmless if wrong.
- **T2** — commercially or legally consequential. Proposal only, human publishes.

### A. Temporal decay — the content is wrong because time passed

| # | Use case | Trigger | Tier |
|---|---|---|---|
| A1 | Year-stamped titles/H1/slug: "Mejores CRM **2026**" → 2027 | calendar | T1 |
| A2 | Body copy that repeats the year ("a lo largo de 2026…", "este año") | calendar | T1 |
| A3 | `dateModified` / "Actualizado en …" freshness stamp after any edit | any edit | T0 |
| A4 | Decaying phrasing: "recientemente lanzado", "actualmente en beta", "el nuevo X" | age of claim | T1 |
| A5 | Seasonal/campaign pages (Black Friday, rebajas) left live out of season | calendar | T1 |
| A6 | Prices/plans quoted inside an article ("desde 29 €/mes") | vendor price change | T2 |

Note on A1: renaming the title must **not** change the slug — that silently 404s
the live URL. Overlaps `bl_site_redirect`; decide who owns it (see tensions).

### B. Recommendation drift — the advice is wrong

| # | Use case | Trigger | Tier |
|---|---|---|---|
| B1 | A recommended tool was discontinued / shut down | vendor news | T1 |
| B2 | A recommended tool was acquired or renamed | vendor news | T1 |
| B3 | A recommended tool changed pricing model (free tier killed) | vendor news | T1 |
| B4 | A new entrant now deserves a slot in a "Top N" | market scan | T1 |
| B5 | Comparison/feature tables gone stale | vendor changelog | T1 |
| B6 | Outbound/affiliate link 404s or redirects somewhere unrelated | link check | T0 |
| B7 | Ranking order no longer defensible given B1–B4 | derived | T1 |

### C. Software & product change

| # | Use case | Trigger | Tier |
|---|---|---|---|
| C1 | How-to steps broken: menu renamed, setting moved, feature removed | vendor docs | T1 |
| C2 | API/SDK deprecation in a technical article | vendor changelog | T1 |
| C3 | New major version released → article covers only the old one | vendor release | T1 |
| C4 | Screenshots/figures showing a UI that no longer exists | derived from C1 | T2* |
| C5 | **Our own** product changed (bl-site-package ships a feature) → every page describing it | our release | T1 |
| C6 | Client's own catalogue/services changed → pages still sell the old one | site data | T1 |

*C4 is T2 because we cannot regenerate a real screenshot; the only honest
actions are "remove the stale image" or "flag it" — never fabricate a UI shot.

### D. Legal / compliance / regulatory — **the sharp edge**

| # | Use case | Trigger | Tier |
|---|---|---|---|
| D1 | Privacy policy vs. new regulation (AI Act, cookie guidance, GDPR enforcement) | law change | T2 |
| D2 | Privacy policy vs. **actual** subprocessors/data flows on the site | site drift | T2 |
| D3 | Terms & conditions vs. consumer-law change (withdrawal period, guarantees) | law change | T2 |
| D4 | Cookie policy vs. the trackers actually loaded by the site | site scan | T2 |
| D5 | Aviso legal: company data, VAT, registry entry out of date | client data | T2 |
| D6 | Accessibility statement (EAA, applies to EU e-commerce) | law change | T2 |
| D7 | Sector claim rules — e.g. grow-shop cannabis claims, FinView investment disclaimers | law change | T2 |
| D8 | New payment method / carrier added → shipping & returns text stale | site drift | T2 |

**The whole D family is proposal-only.** In D, the prose *is* the fact — the
"facts are the site's, prose is the agent's" split that makes the other tools
defensible does not hold. An agent that silently edits a privacy policy creates
liability for BigLobster and for the client. See Q2.

### E. Factual correctness

| # | Use case | Trigger | Tier |
|---|---|---|---|
| E1 | Statistics with a stale vintage ("el 62 % de las pymes en 2025…") | age | T1 |
| E2 | Cited source URL is dead → re-source or drop the claim | link check | T1 |
| E3 | Hard facts changed: VAT rate, shipping threshold, opening hours, company renamed | external | T2 |
| E4 | Claims contradicted by the client's own current data | site drift | T1 |

### F. Site-level consistency

| # | Use case | Trigger | Tier |
|---|---|---|---|
| F1 | Internal links to pages that moved | health check | T0 |
| F2 | Two pages contradict each other (price on landing vs. pricing page) | cross-read | T1 |
| F3 | NAP consistency (name/address/phone) across pages + JSON-LD | client data | T0 |
| F4 | CTAs pointing at a discontinued offer | site drift | T1 |

F1 is already owned by the maintenance agent. Do not build it twice.

### G. SEO/GEO maintenance of *existing* content

| # | Use case | Trigger | Tier |
|---|---|---|---|
| G1 | Meta title/description drift after a body update | any edit | T0 |
| G2 | JSON-LD `dateModified` refresh so the update is seen | any edit | T0 |
| G3 | Decayed pages losing rankings → targeted refresh | GSC | T1 |

G3 needs Search Console. The GSC connector today is read-only and
BigLobster-only — a client rental has no equivalent signal. Either the client
grants GSC access at onboarding, or G3 ships BigLobster-only.

### H. Multilingual parity

| # | Use case | Trigger | Tier |
|---|---|---|---|
| H1 | biglobster.top is bilingual — an ES edit must propagate to EN or the pair drifts | any edit | T1 |

Do not re-implement translation. Hand off to the existing translation-engineer,
or the update is only half-done on bilingual sites.

---

## Structural tensions to resolve before writing any code

**1. There is no "propose an edit and hold it for review" primitive.**
Verified in the code, not assumed:
- `bl_site_publish` / `create_blog_post` hardcodes `status: "published"` and
  returns *"Published immediately — live on the blog now."*
- `update_blog_post` deliberately never sends `status`, so editing a live post
  publishes the edit **instantly**.
- `update_page_text` has no status concept at all.

So today every T2 use case has no safe write path. The only precedent for a
review queue is `redirects` (`status` pending → live, in bl-site-package's
`src/api/redirects.js`). A content equivalent would have to be built server-side.

**2. There is no revision history — an edit is irreversible.**
`bl-site-package/src/db/database.js`: the `articles` table carries `updated_at`
and nothing else. No revisions table, no previous_content column. A bad rewrite
destroys the original with no undo. For an additive agent that is tolerable; for
a rewriting agent it is a blocker. Minimum viable fix: store the pre-edit body
before writing (site-side revision row, or agent-side snapshot).

**3. Where does "already reviewed" state live?**
Infographic uses an in-content sentinel (`<!-- infographic:auto -->`) because its
work is done-once. Content decay is **continuous** — a page reviewed today needs
reviewing again next year. The state is not a boolean; it is
`(page_id, last_reviewed_at, content_hash, findings)`. This has no home today.

**4. Lost updates between agents.** `update_blog_post` is a blind PUT with no
fingerprint or etag. Infographic, maintenance and this agent can all edit the
same post on the same day; last writer wins and silently discards the others'
work. `bl_site_product` already solved this with a server-side
`source_fingerprint`; blog posts have no equivalent.

**5. "What changed in the world" is the expensive half.** The value of B, C, D
and E depends entirely on real research, and `agent.log` cannot prove research
happened (parallel tool batches log nothing — see the Langfuse lesson). This
needs an evidence ledger per change and Langfuse verification, exactly like
gap-hunter.

**6. Scale does not fit in one run.** "Review all pages" is not a bounded task;
the agent loop hard-stops at 90 iterations. Like every other agent here, this
must be one prioritized page (or a small N) per run, with a queue that surfaces
what is most decayed.

---

## Open questions for the CEO

- **Q1 — Approval model.** Auto-apply T0/T1 and propose T2? Or propose
  everything in v1 and earn auto-apply later (the remediation-loop
  apprenticeship pattern: gated → auto after K clean runs)?
- **Q2 — Is the legal/compliance family (D) in v1 at all?** It is the strongest
  sales story and the largest liability. Options: exclude; include as
  detect-and-alert only (never drafts text); or include as drafts a human
  publishes.
- **Q3 — One agent or two?** "Refresh my guides" and "keep my legal pages
  compliant" are different products, different buyers, different risk. Selling
  them separately may be worth more than one big agent.
- **Q4 — BigLobster-first scope.** biglobster.top is git+PR, so on our own site
  every change is a reviewable PR and the tensions above (no history, no review
  queue) **do not exist**. Do we ship the BigLobster variant first and only then
  build the site-side primitives the rental needs?

---

## Decisions (CEO, 2026-09-16)

- **Approval model:** auto-apply T0/T1, propose T2 for a human.
- **Legal family (D):** in scope — agent drafts, a human publishes.
- **Packaging:** one agent, all families.
- **First target:** BigLobster (git + PR), rental variant after.

## Architecture — BigLobster variant

### The approval model already exists. Do not build it.

`auditor/tiers.py` classifies a PR's changed files and the content tier
auto-merges. For `br41s/biglobster` the content allowlist is exactly:

    site/blog/          site/en/blog/          web/blog/images/       web/assets/

Everything else on that repo is **system tier — advisory review, human merge**.

Which lands the chosen approval model for free:

| Target | Path | Tier | Result |
|---|---|---|---|
| Blog articles (A, B, C, E, G, H) | `site/blog/`, `site/en/blog/` | content | **auto-merges** = T0/T1 |
| Legal pages (D) | `site/privacidad.html`, `condiciones.html`, `reembolsos.html`, `uso-de-ia.html` | system | **human merges** = T2 |
| Pricing / marketing pages (A6, F2) | `site/precios.html`, `index.html`, `services.html`, … | system | **human merges** = T2 |
| Legacy frozen copies | `web/*.html` | system | flagged as stale-workflow — never write here |

So the agent does not need a review queue, a pending state, or an approval
flag. It needs to put the right change in the right file and open one PR per
change set. The tiering does the rest. **One consequence to respect: never mix
a blog edit and a legal edit in the same PR** — one system-tier file drags the
whole PR out of auto-merge.

### The content surface

- `site/blog/` — 105 ES articles. `site/en/blog/` — 106 EN.
- Format: `---json` frontmatter + body HTML (no chrome).
- Frontmatter fields that matter: `title`, `description`, `ogTitle`,
  `ogDescription`, `permalink`, `date`, `dateModified`, `badges`, `excerpt`.
- **88 of 105 ES articles carry `2026` in the slug** — the A1 year-rollover
  case is the single biggest batch of work on the site.

### Invariants for the prompt

1. **`permalink` is immutable.** It is an explicit frontmatter field, decoupled
   from the filename, so a title can roll to 2027 while the URL stays
   `-2026.html`. Changing it 404s a live URL and throws away its SEO equity.
   Never rename the file either.
2. **`dateModified` is mandatory on every edit**, and is the only date field
   ever touched. `date` (first published) is immutable.
3. **Bounded edit, never a rewrite.** The agent changes the claims that are
   wrong. A diff that touches most of the body is a bug, not a refresh — cap it
   and bail out.
4. **Every changed claim needs a cited source in the PR body** (the evidence
   ledger, as gap-hunter does). Verify in Langfuse, never in `agent.log`.
5. **ES and EN are one unit of work.** An article and its translation change in
   the same PR, or the pair drifts. Both dirs are content tier, so this does not
   cost the auto-merge.
6. **One article per run.** 90-iteration ceiling; the queue is ~100 articles.
7. **Never write `web/*.html`** — legacy frozen copies, and the auditor reads a
   write there as the agent following the pre-migration workflow.

### Open design question — the refresh queue

With ~100 articles and one run a day, *which* article next is the whole product.
Needs a decay score over: year in title vs. now, `dateModified` age, density of
decaying phrasing, presence of a "top N" list, traffic (GSC, BigLobster-only).
Where that state lives is unresolved — a committed ledger file in the repo is
the cheapest option and matches `translation-ledger.md`, which already exists.

## Build plan

1. Decay scanner script (deterministic, committed, testable) — scores the
   corpus, emits the queue. No LLM.
2. `content-updater/biglobster-content-updater.prompt` — the agent.
3. Cron job on the biglobster profile, git workdir, daily, one article per run.
4. Dry runs on a branch; review the diffs by hand before letting it auto-merge.
5. Rental variant — blocked on the two site-side primitives (revision history,
   pending state). Separate piece of work.

---

## Open issue found while writing the prompt — the ledger breaks Lane A

The refresh ledger (`content-updater-ledger.json`) has to be written on every
run, including runs that change nothing (a reviewed-and-fine article must enter
cooldown or the scanner re-serves it forever). But the ledger sits at the repo
root, which is **system tier** — so a Lane A article PR that also carries the
ledger update stops auto-merging, and the whole approval model collapses into
"a human merges everything".

Three ways out:

1. **Add it to `_REPO_EXTRA_CONTENT_FILES` in `auditor/tiers.py`** ← recommended.
   That dict exists for exactly this ("per-repo exact files that are safe
   content despite living outside the publish dirs") and is currently empty.
   One line. The change itself is system tier, so it gets a deep review and a
   human merge — appropriate for something that widens an auto-merge allowlist.
   Blast radius if the agent ever corrupts its own ledger: the scanner picks the
   wrong articles. Self-correcting, never a live-site change.
2. Move the ledger under `site/blog/`. Cheapest, but Eleventy may pick up a
   stray `.json` as a data file — an unforced build risk for no gain.
3. A second, ledger-only PR each run. Doubles PR volume for nothing.

**RESOLVED 2026-09-16 — option (4), which was not in the original list.**

Checking `auditor/tiers.py` properly turned up a fourth option that beats all
three above: `_PROFILE_CONTENT_SUFFIXES` includes `.md`, so **any Markdown file
on a profile repo is content tier at any depth**. Making the ledger
`content-updater-ledger.md` needs no auditor change at all.

Verified empirically:

    python3 -m auditor.tiers --repo br41s/biglobster content-updater-ledger.json   -> system
    python3 -m auditor.tiers --repo br41s/biglobster content-updater-ledger.md     -> content
    ... site/blog/foo.html site/en/blog/foo.html content-updater-ledger.md          -> content

This repo already solved the same problem this way: `translation-ledger.md` is a
root-level Markdown table written append-only by the Translation Engineer cron,
and it classifies as content.

Why it beats option (1): identical end state, but (1) permanently widens the
auto-merge allowlist on the revenue site — a standing grant to every future
agent that writes that path — whereas (4) grants nothing new. When a convention
in the same repo already solves the problem, matching it costs less than
extending the rule, and leaves nothing for anyone to audit later.

Cost paid: the scanner parses Markdown instead of `JSON.parse`. Mitigated with a
strict row format that throws on a malformed, mis-dated or duplicate row rather
than skipping it — a skipped row is a lost cooldown and a silent re-refresh.

## Prompt drafted

`content-updater/biglobster-content-updater.prompt` — 243 lines, house style
(CONFIG → STEP 0-9 → guardrails → failure handling), modelled on
`translation/translation-engineer.prompt`.

Three things in it worth re-reading before it ships:

- **The sourcing rule is the spine.** The agent may only assert a fact it
  verified this run with a pasteable URL. Removing a stale claim never needs a
  source; replacing one always does. Its own memory is explicitly not a source.
- **Claim tier ≠ file tier.** A penalty amount quoted inside a blog post is T2
  even though blog posts are Lane A — so the agent fixes the T1 findings and
  leaves the price alone rather than smuggling it into an auto-merging PR.
- **Bounded-diff guard at ~25% of body words.** Past that it is a rewrite, not
  a refresh; the agent stops and hands it to a human.

---

## Verified corpus facts (2026-09-16)

Independently checked, not taken from the inventory agent's report:

| Fact | Value |
|---|---|
| ES articles / EN articles | 105 / 106 |
| Carry `2026` in title, description, ogTitle or permalink | **99 of 105** |
| Have a `dateModified` field | **42** — so **63 do not** |
| Paired to an EN counterpart via langmap | **105 of 105** |
| Pairs whose ES and EN slugs DIFFER | **75 of 105** |
| Front-matter format | 102 `---json`, 2 bare-fence JSON, **1 real YAML** |
| Listicles (mejores / top N / comparativa) | 7 |
| Mean body length | ~2,025 words |

Topic mix: general advice 60, legal-compliance 13, pricing-costs 11, case
study 10, marketing-seo 5, subsidies-grants 3, software-tools 3.

### Three of these change the design

- **60% of articles have no `dateModified`.** The scanner's fallback to `date`
  is the COMMON path, not an edge case. It also means the site currently shows
  Google "published in May 2026, never touched since" on most of the corpus —
  so simply stamping `dateModified` on every refreshed article is a real SEO
  gain independent of the content change.
- **Three front-matter shapes exist**, not one: 102 `---json`, 2 a bare `---`
  fence containing JSON (valid YAML flow mapping, so Eleventy accepts it), and
  1 real YAML block. A JSON-only parser drops the last one with no error. The
  scanner handles all three and fails loudly on anything else — an article that
  silently never enters the queue never gets refreshed and nobody finds out.
- **75 of 105 ES/EN pairs have different slugs.** Path substitution
  (`/es/blog/` → `/en/blog/`) is wrong three times out of four, and the EN
  permalinks are at root `/blog/` anyway. `langmap.json` is the only authority.
  This was already a guardrail in the prompt; it is now a measured one.

### Corpus problems worth a separate decision

- **99 articles carry 2026 in their metadata.** On 2027-01-01 essentially the
  whole blog reads as last year's. That is a scheduled cliff, not a discovery —
  at one article per day it cannot be absorbed by this agent alone. Either the
  agent gets a burst budget for Q4/January, or the year rollover is handled as
  a one-off batch job and the agent maintains it afterwards.
- **Light external linking**, ~24 distinct domains across 105 articles, most
  cited once. Weak authority signals and few sources to re-verify against.
  Out of scope here; worth its own agent or a prompt change to the Gap Hunter.

---

## Scanner: BUILT (2026-09-16)

`br41s/biglobster`, branch `feat/content-decay-scanner`, committed, **not pushed**.

- `scripts/content-decay/scan.mjs` — 550 lines, Node built-ins only, every
  function exported and pure. `WEIGHTS` centralised at the top.
- `scripts/content-decay/scan.test.mjs` — 29 tests, all passing
  (`node --test scripts/content-decay/scan.test.mjs`).
- `scripts/content-decay/README.md` — the scoring rationale.
- `content-updater-ledger.json` — `{}`, ready for first use.

Validated against hand-checked ground truth: 105 rows, 63 missing
`dateModified`, 105/105 paired. Exits 0 on the clean corpus.

### Defect found and fixed during review

An unparseable article was pushed to `warnings` and skipped, and the process
still exited 0 — the silent-omission failure. Escalated to a fatal `errors`
list with a non-zero exit, plus two tests pinning it. This is the difference
between "scanned 105" and "scanned 104 and said nothing".

### Calibration finding — the score barely discriminates today, and that is correct

First real run: scores span 24.2–78.3, median 55.9, but the **top 23 articles
sit within 10 points of each other**. The reason is structural, not a bug:

- S1 (years behind) is **0 for all 105 articles** — it is 2026 and they say 2026.
- S2 (Q4 rollover) is **0 for all 105** — it is September; it starts firing in October.
- reach is 1.0 for all (no GSC export wired yet).

So the ranking is currently carried by S3, S6 and S7 alone. What it surfaces is
legal/subsidy/tax articles with dense hard claims — `ayudas-transformacion-
digital-galicia-2026`, `reforma-ley-prl-2026`, `factura-electronica-b2b-
obligatoria-2026`. That is the right answer for September: those are the pages
where being wrong actually costs someone money, and grant deadlines really do move.

Do not re-tune the weights against this run. The ranking changes character in
October (S2) and again in January (S1). Tune then, with real data.

## Wiring it up

`hermes cron create` does **not** expose `enabled_toolsets` or `prompt_source`,
so the job goes through the same `cron.jobs.create_job()` API that
`scripts/provision_bl_client.py:632` uses:

```python
from cron.jobs import create_job
from pathlib import Path

job = create_job(
    prompt=Path("<hermes-sandbox clone>/content-updater/biglobster-content-updater.prompt").read_text(encoding="utf-8"),
    schedule="<a slot no other profile/workdir job occupies>",
    name="Content Updater — BigLobster",
    deliver="telegram",
    profile="biglobster",
    workdir="<the biglobster clone>",
    prompt_source="content-updater/biglobster-content-updater.prompt",
    enabled_toolsets=["file", "terminal", "web", "skills", "todo", "gsc"],
)
```

Run it as `hermes`, never root — root flips `jobs.json` ownership.

Four things that will bite otherwise:

- **The prompt and the workdir are in DIFFERENT repos.** `workdir` is the
  biglobster clone; `prompt_source` is a path inside hermes-sandbox. A later
  `hermes cron sync-prompt <job_id> --prompt-source …` therefore has to run from
  the *hermes-sandbox* clone, because that flag resolves relative to cwd.
- **There is no `§6d` for this prompt.** The boot-time resync at
  `docker/cont-init.d/03-biglobster-config:1297` is hardcoded to the auditor
  (`job.get("profile") == "auditor"`). Nothing propagates this one automatically,
  so a prompt change reaches the live job only via an explicit `sync-prompt`.
  That also means a deploy is NOT required for a prompt-only change here.
- **Naming `gsc` makes the MCP list an allowlist.** Per
  `cron/scheduler.py:181`, a per-job `enabled_toolsets` that names no MCP server
  gets every globally-enabled one unioned in; naming one restricts it to exactly
  that. Listing `gsc` gives this job GSC and no other MCP server, which is
  intended.
- **Pick the schedule against the other jobs.** Every job setting `profile` or
  `workdir` runs on a single-thread sequential pool, so this one blocks all the
  others while it runs, and a queued job is indistinguishable from a dead one.
  `hermes cron list` shows what is already scheduled.

## Next

1. ~~Ledger tiering~~ — DONE, Markdown ledger.
2. ~~Wire GSC clicks into the scanner~~ — DONE, `--gsc` takes the raw response.
3. ~~Scanner PR~~ — DONE, merged.
4. Merge this PR, then create the cron job (above).
5. First run with `DRY_RUN: true` — read the diff by hand. Note that consecutive
   dry runs re-pick the SAME article, because nothing is pushed and the ledger
   row never commits.
6. Flip `DRY_RUN` to false and `sync-prompt` once the diffs look right.
