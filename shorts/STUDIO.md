# Shorts Studio — BigLobster's daily shorts, from Hermes, for free

Hermes turns each new blog post (EN and ES) into a ~60s branded vertical video
and publishes it to YouTube Shorts, Instagram Reels + Stories and Facebook. X is
handed to a human on Telegram.

It merges two earlier attempts:

| | grokbot checklist (v1, manual/desktop) | Social Shorts (PR #175, rental) | **Studio (this)** |
|---|---|---|---|
| Look | Remotion graphics, karaoke, titled covers | B-roll + white captions | Remotion template: 7 beat types, karaoke, palettes, motifs, covers |
| Timing | .vtt (sentence-level in edge-tts 7.x) | per-scene duration | Edge `WordBoundary` — word-exact karaoke |
| Where it renders | the agent's desktop | Hermes container (ffmpeg) | GitHub Actions (Node + Chromium, free) |
| QA | eyeballed | duration cap only | automatic: loudness, freeze, black, silence, format, covers |
| Facts | "don't invent" in prose | prose | every figure checked against the article by the tool |
| Publishing | Zernio + browser (blocked by "verify it's you") | none | YouTube Data API + Meta Graph API; X → Telegram |
| State | LEDGER.md edited by the agent | sentinel in the post | ledger owned by the tool |

The rental SKU (`shorts/bl-site-package-shorts.prompt`, `shorts_render`) is
unchanged and keeps rendering in-container; see §9 for how it moves onto the studio.

---

## 1. Architecture

```
 Hermes cron (default profile, no workdir — off the shared profile thread)
 ┌──────────────────────────────── Shorts Producer (daily) ────────────────────────┐
 │ shorts_studio posts(en) → article → submit ─┐   then the same for es            │
 └─────────────────────────────────────────────┼───────────────────────────────────┘
          validate package · re-read article ·  │ workflow_dispatch (package inline)
          check figures · check style · ledger  ▼
                       GitHub Actions  .github/workflows/shorts-studio.yml
                       plugins/shorts/studio/build.py
                         Edge TTS (WordBoundary) → frame-aligned timeline
                         Pexels / Mixkit footage + Mixkit music bed
                         Remotion (muted, bt709) → ffmpeg duck + 2-pass loudnorm
                         Story cut on a beat · covers · SRT · QA → artifact
                                                │
 ┌──────────────────────────── Shorts Publisher (twice daily) ─────────────────────┐
 │ shorts_studio status (download artifact, read QA)                               │
 │   live:   publish youtube → instagram_reel → facebook → instagram_story         │
 │   shadow: nothing published                                                     │
 │ shorts_studio handoff → Telegram: X post + video (live) / everything (shadow)   │
 └─────────────────────────────────────────────────────────────────────────────────┘
```

Why the render is not in Hermes: a Remotion render is ~1,800 browser frames a
minute of video. The Zeabur pod is CPU-only, and every profile/workdir cron job
shares one thread, so a 4-minute render there would stall the fleet. Actions
minutes are free, isolated and come with Chromium. Neither agent waits on a
render: the producer submits and exits, the publisher collects what finished.

| File | Role |
|---|---|
| `plugins/shorts/studio/package.py` | The package contract + grounding check. Shared by both sides. |
| `plugins/shorts/studio/build.py` | Render pipeline (runs in Actions) |
| `plugins/shorts/studio/{voice,media,sources,qa}.py` | TTS + captions, ffmpeg, footage/music, QA |
| `shorts/studio/remotion/` | The template (`Short`, `Cover`, `Thumb`) |
| `plugins/shorts/studio_tool.py` | `shorts_studio` tool (Hermes side) |
| `plugins/shorts/{ledger,github_studio}.py` | Ledger; dispatch/collect client |
| `plugins/shorts/social/{youtube,meta}.py` | Publishers |
| `shorts/biglobster-shorts-{producer,publisher}.prompt` | The two agents |
| `scripts/setup_shorts_jobs.py` | Creates the two cron jobs |
| `scripts/youtube_oauth.py` | Mints the YouTube refresh token (run locally, once) |
| `shorts/CHECKLIST.md` | The manual checklist, revised (Spanish) |

## 2. What is enforced in code (not in the prompt)

- **Facts.** `submit` re-fetches the article and rejects any figure (a number
  with %, currency, a multiplier, or above 10) that the article body does not
  contain. Counting words ("3 mistakes") are exempt.
- **Look.** Palette and motif must differ from the last two shorts, which
  includes the other language's twin.
- **Shape.** 4–10 beats, hook first, CTA last and on the article's own domain,
  70–200 spoken words, on-screen ≤8 words, X ≤280, and so on. All problems come
  back in one list.
- **No repeats.** A post with a live, ready or published short is never picked
  again; footage and music IDs used by recent shorts are passed as avoid-lists.
- **QA gate.** Only `ready` (QA passed) shorts can be published. See the table in
  `CHECKLIST.md` §9.
- **Shadow mode.** Nothing is published unless `SHORTS_PUBLISH_MODE=live`.
- **Avatars.** An avatar beat must use a library clip and speak its stored line,
  so captions always match the mouth.

## 3. One-time setup

Everything below is free. Keys go into **Zeabur service env vars** — never
into chat, a commit, or a printed env table (CLAUDE.md, *Secrets*).

### 3.1 GitHub (render farm)

1. Merge to `main`: `workflow_dispatch` only works for a workflow on the default branch.
2. Fine-grained PAT, repository `br41s/hermes-sandbox` only, permissions
   **Actions: read & write**, **Contents: read & write** (the latter only for
   avatar clip uploads) → Zeabur `SHORTS_STUDIO_GITHUB_TOKEN`.
3. Repo secret **`PEXELS_API_KEY`** (Settings → Secrets → Actions): BigLobster's
   own free key. Without it footage falls back to Mixkit, which is mostly
   landscape and matched only 2 of 6 queries in the first smoke run.

Optional: `SHORTS_STUDIO_REPO` / `SHORTS_STUDIO_REF` (defaults
`br41s/hermes-sandbox` / `main`), `SHORTS_FEEDS` (JSON `{"en": url, "es": url}`).

Actions cost: ~5 min per short (the first run took 4m08s), 2 shorts a day, so
~300 min/month, inside the free private-repo allowance.

### 3.2 YouTube

1. Google Cloud project → enable **YouTube Data API v3**.
2. OAuth consent screen: External, set to **In production**. In "Testing",
   refresh tokens die after 7 days.
3. Credentials → OAuth client ID → **Desktop app**.
4. On your own machine: `python3 scripts/youtube_oauth.py --client-id … --client-secret …`,
   and sign in as the channel owner.
5. Zeabur: `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`.
   Optional: `SHORTS_YOUTUBE_PRIVACY` (default `public`) and
   `SHORTS_YOUTUBE_CATEGORY` (default `27`, Education).
6. **Audit.** Until the project passes Google's free *YouTube API Services*
   audit, API uploads are forced to private. File it from the API's quota page.
   The tool reports the privacy YouTube actually applied.
7. Custom thumbnails need a phone-verified channel. Without one the upload
   still works and only the thumbnail is skipped, with a warning.

### 3.3 Facebook + Instagram

1. Instagram must be a **professional** account linked to the BigLobster Facebook Page.
2. developers.facebook.com → create an app (type Business). Development mode is
   enough while only accounts with a role on the app post: no App Review.
3. Graph API Explorer → user token with `pages_show_list`,
   `pages_read_engagement`, `pages_manage_posts`, `instagram_basic`,
   `instagram_content_publish`, `business_management`.
4. Exchange it for a long-lived user token, then `GET /me/accounts`. The Page's
   `access_token` from a long-lived user token does not expire.
5. `GET /<page-id>?fields=instagram_business_account` gives the IG user id.
6. Zeabur: `META_PAGE_ID`, `META_PAGE_ACCESS_TOKEN`, `META_IG_USER_ID`.
   Optional: `META_GRAPH_VERSION` (default `v23.0`).

### 3.4 Turn it on

1. Deploy (`scripts/deploy.sh`): the plugin is baked into the image.
2. As `hermes`, from the hermes-sandbox clone:
   `.venv/bin/python3 scripts/setup_shorts_jobs.py --dry-run`, then without `--dry-run`.
   It prints the Hermes timezone, and the default schedules (`30 8 * * *` producer,
   `20 9,13 * * *` publisher) are in it. Adjust with `--producer-schedule` /
   `--publisher-schedule`.
3. **Week 1 = shadow mode** (leave `SHORTS_PUBLISH_MODE` unset). Each short arrives
   on Telegram with the video, cover and all copy. Grokbot keeps publishing.
4. When the shadow shorts are at or above the grokbot's level:
   `SHORTS_PUBLISH_MODE=live`, and stop the grokbot's publishing routine the same day.
   Two publishers on one account means double posts.

Prompt edits reach the live jobs only through
`scripts/sync_prompt_drift.py --source shorts/biglobster-shorts-producer.prompt`
(and the same for the publisher), or by re-running `setup_shorts_jobs.py`.

## 4. Avatars (Google Flow: Martín, Lucía)

Flow has no API; programmatic Veo access is pay-per-second. So a human still
generates the clips, but only once per batch, not once per short:

1. In Flow, generate 9:16 clips of 3–8 s in which the avatar **speaks** a
   reusable line. Per language: one opener ("Soy Martín, de BigLobster…"),
   one closer ("Tienes la guía completa en el enlace"), and 2–3 short
   reactions. One batch covers weeks.
2. Send each clip to Hermes on Telegram with its avatar, language and exact line.
   Hermes calls `shorts_studio avatar_upload`, which checks it (video + audio,
   1–15 s), stores it as a release asset the render farm can fetch, and adds it
   to the library with its line. The chat platform needs the `shorts` toolset
   enabled (`hermes tools`).
3. The producer may then use at most one avatar beat per short, with a library
   clip and its line word for word, and only when it fits. The render plays the
   avatar full-frame with its own voice; captions follow the stored line;
   YouTube gets `containsSyntheticMedia: true`.

Per-article avatar lines, i.e. the old hybrid pattern of avatar → edu → avatar,
work the same way. Upload that article's clips, then ask Hermes in chat to build
the short with them. Automating the Flow step itself would mean paid Veo 3.1
through the Gemini API with Martín/Lucía as reference images. That is a cost
decision, not a code one.

## 5. Costs

| Piece | Cost |
|---|---|
| Script writing | LLM tokens for two short agent runs a day |
| Voice (Edge TTS), footage (Pexels/Mixkit), music (Mixkit) | €0 |
| Render (GitHub Actions) | €0 inside the free minutes |
| YouTube Data API, Meta Graph API | €0 |
| X | €0 (posted by hand) |
| Remotion | €0 while BigLobster has ≤3 employees. Above that, or when selling renders to clients at scale, the Company licence ("Automators", $0.01/render, $100/month minimum) applies |

## 6. Proven vs not yet proven

**Proven.** Full renders on Actions with real Edge TTS, in both languages. EN: 137
words, 63.0s, Remotion 193s. ES (Álvaro): 148 words, 66.0s, footage found for 6
of 6 queries after the Mixkit slug fallback, Mixkit bed, Remotion 388s at one core.
Covers, ducking, two-pass loudness at −14.0 LUFS and QA passed on both. The same pipeline also ran
offline in the dev sandbox. 45 unit tests cover the package contract,
grounding, ledger, tool flows, GitHub client, publishers (mocked HTTP) and the
ffmpeg loudness/Story path.

**Not yet proven.** Live YouTube and Meta uploads (no credentials in development),
including Instagram's resumable upload for Stories and the unpublished-photo
cover trick. They are written to the documented API, but the first live-mode run
is the real test, which is why shadow mode comes first. Also unproven: a Pexels
key in the render farm.

## 7. Operating it

- `shorts_studio ledger` (from chat) shows the last shorts, their state and URLs.
- A short stuck in `rendering`: open its `run_url`. After an hour with no run it
  becomes `render_failed`, and the producer retries the post the next day.
- `qa_failed`: `qa.errors` says why. The post is retried with a fresh script.
- Instagram `pending`: Instagram was still processing. The second publisher run finishes it.
- The render farm alone: Actions → *Shorts Studio* → Run workflow, with a
  `request_id` and a package (JSON → gzip → base64).

## 8. Tuning the look

Palettes and motifs live in `shorts/studio/remotion/src/theme.ts` and are mirrored in
`package.py` (a test keeps them in step). Preview with
`cd shorts/studio/remotion && npm ci && npm run studio`. Any change to the studio
triggers the smoke render of both samples on push.

## 9. Next: the rental version

The studio is multi-tenant by construction: packages carry the article and
style, and publishing credentials are env per profile. Moving the `shorts`
rental SKU onto it means:

1. a client-lane producer prompt that reads posts through `bl_site_publish`
   instead of RSS;
2. per-client Meta/YouTube credentials (BYOK), excluded from the boot hook's
   shared `inject`, like `PEXELS_API_KEY`;
3. a render-farm quota per tenant;
4. the Remotion Company licence once BigLobster renders for clients.
