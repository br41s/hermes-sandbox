# Content Updater — rental variant

Status: **phases 1, 2 and 4 built and committed on branches 2026-09-19. Nothing pushed, nothing deployed, phase 3 blocked on both.** Continues `tasks/content-updater.md`,
which built and dry-ran the BigLobster variant and closed with:

> 5. Rental variant — blocked on the two site-side primitives (revision
>    history, pending state). Separate piece of work.

This is that piece of work. CEO decision 2026-09-19: **build the site-side
primitives first**, then clone the agent, then provision shoroban, then sell it
at 4,99 €/mes.

---

## Why a prompt clone was never enough

The BigLobster variant is safe because of where it runs, not because of what it
says. git+PR supplies three guarantees that a rented client site supplies none
of. Re-verified in the code 2026-09-19, three days after the original finding:

| Guarantee | BigLobster | Rented site today | Evidence |
|---|---|---|---|
| Undo a bad edit | git history | **none** | `articles` carries `updated_at` and nothing else — `src/db/database.js:147` |
| Human review before live | PR + `auditor/tiers.py` | **none** | `PUT /posts/:id` is a blind COALESCE — `src/api/blog.js:109` |
| Concurrent writers don't clobber | merge conflict | **none** | no fingerprint on `articles` |

The third is already live on shoroban, not theoretical: **Infographic Engineer
(02:31) and Website Maintenance (02:53) both edit article bodies daily.** A
third writer with no etag means last-writer-wins silently discards the others'
work. Nobody would ever see it happen.

`big.html` already sells the fix — *"Cada cambio pasa por ti antes de
publicarse. Tú das el visto bueno."* Today that sentence is true only because
no rented agent rewrites prose. This agent is the one that would make it false.

## The house pattern already solves all three

Nothing here is new design. bl-site-package solved each of these once already:

- **pending → live, server-validated** — `redirects` (`src/api/redirects.js`):
  a proposal always lands `pending` however strong the evidence; publishing is
  a separate explicit call; only `live` rows are served.
- **drift detection via a snapshotted fingerprint** — `product_content.source_fingerprint`,
  compared against the live `products` row to notice the facts moved under us.
- **"facts are the site's, prose is the agent's"** — the server decides
  eligibility; the caller never asserts it.

Applying those three to `articles` is the whole of Phase 1.

---

## Phase 1 — bl-site-package primitives  (repo: `br41s/bl-site-package`)

- [x] `article_revisions` table — pre-edit snapshot captured in the **same
      transaction** as every article write, so it is a true undo and cannot
      drift from what was actually replaced.
- [x] `articles.content_hash` — computed server-side on every write, never
      accepted from a caller.
- [x] `article_edits` table — `pending | applied | rejected` prose proposals,
      carrying `base_hash` + an evidence ledger.
- [x] API: `POST /api/blog/posts/:id/propose`, `GET /api/blog/edits`,
      `POST /api/blog/edits/:id/apply`, `POST /api/blog/edits/:id/reject`,
      `GET /api/blog/posts/:id/revisions`, `POST /api/blog/posts/:id/revert`.
- [x] 409 on a stale `base_hash` — the lost-update guard, covering the existing
      `PUT` too, so Infographic and Maintenance benefit without changing.
- [x] Tests alongside `redirects.test.js` / `product-content.test.js`.
- [x] `npm version minor` + `node scripts/check-version-bump.mjs` before merge.

## Phase 2 — the agent  (repo: `br41s/hermes-sandbox`)

- [x] `content-updater/bl-site-package-content-updater.prompt` — clone of the
      BigLobster prompt retargeted from git+PR onto the new endpoints. The
      sourcing rule, the bounded-diff cap and claim-tier-≠-file-tier carry over
      unchanged; the lane model becomes apply-vs-propose.
- [x] `tools/bl_site_publish_tool.py` — the `propose_edit` action, and `base_hash`
      threaded through `update_blog_post`.
- [x] `scripts/provision_bl_client.py` — `content-updater` in `AGENT_SOURCES`
      with an explicit toolset list.
- [x] Tests, incl. `tests/scripts/test_rental_agent_toolsets.py` coverage.

## Phase 3 — provision shoroban

- [ ] Add the job to `bl-shoroban` (daily, a slot no other profile/workdir job
      holds — the pool is single-threaded).
- [ ] First runs in propose-only mode; read the proposals by hand.

## Phase 4 — sell it  (repo: `br41s/biglobster`)

- [x] `site/big.html` — agent card, 4,99 €/mes.
- [x] `site/agentes-en-alquiler.html` — card + JSON-LD offer.
- [x] `site/agentes/content-updater.html` — detail page.
- [x] EN counterparts.

---

## Flagged, not blocking

**4,99 €/mes makes this the cheapest agent in the catalogue and the most
expensive to run.** Every other price sits at or above 5,99 €; this one does
live web research on every run — the Gap Hunter cost class, and Gap Hunter is
12,99 €. Model spend is BYOK so it lands on the client's own OpenRouter key,
not ours, which is what makes the price defensible. Noted so the margin is a
decision and not an accident.

---

## Where this stopped, 2026-09-19

Three branches, all committed locally, **none pushed**:

| Repo | Branch | Contents |
|---|---|---|
| `br41s/bl-site-package` | `feat/article-revisions-and-proposals` | the primitives, v1.8.0, 307/307 green |
| `br41s/hermes-sandbox` | `feat/content-updater-rental` | tool + prompt + provisioning |
| `br41s/biglobster` | `feat/content-updater-landing` | the three pages, both languages |

Phase 3 cannot start until two deploys land, and they are ordered:

1. **bl-site-package 1.8.0 onto shoroban.com.** `fleet/manifest.json` gives
   that deployment `"driver": "manual"` and `"host": "plesk-passenger"`, so it
   is a hand deployment, not a tag move. Until it happens the site has no
   `/propose` endpoint — the tool 404s and, by design, refuses to fall back to
   publishing.
2. **hermes-sandbox onto Zeabur**, for `propose_edit` to exist in the agent's
   schema at all.

Only then does the cron job go in, as `hermes` and never root, on a slot no
other profile/workdir job holds — that pool is single-threaded and shoroban
already occupies 02:09, 02:31, 02:53 and every odd hour at :12.

## Noticed in passing, not touched

- `fleet/manifest.json` records shoroban's `hermes_profile` as `shoroban`; the
  live profile is `bl-shoroban`. Nothing reads the field (it is documented as
  informational in `fleet/README.md` and only `fleet-check.test.mjs` mentions
  it), so this is a stale doc string rather than a bug.
- `agentes-en-alquiler.html` sells six agents; `AGENT_SOURCES` now has ten.
  `product-sheets`, `shorts`, `onboarding-content` and `product-articles` are
  provisionable but not on the page, and `Off-Site GEO Scout` is on the page
  with no `AGENT_SOURCES` entry. Pre-existing drift, worth its own pass.
