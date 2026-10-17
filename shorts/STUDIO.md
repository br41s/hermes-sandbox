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
 Hermes cron (default profile, no workdir — main's unbounded pool)
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
minute of video. The Zeabur pod is CPU-only and runs every profile's agents at
once, so a 4-minute render there would stall the fleet. Actions
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
- **Look.** Every short uses the `brand` palette (owner, 2026-10-14: cream
  cards, red accent, ink text, sampled from VideoDesk's shorts in Buzz, over
  the article's own cover, blurred); it is exempt from the old rule that the
  palette must differ from the last two shorts, which still applies to the
  other palettes. The motif still rotates (it is barely visible under the cover).
- **The promise, written and spoken, within Lucía's first 10 s (owner,
  2026-10-17).** Her joke comes first by design, so the tip format drops the
  classic hook's ~3 s rule (first sentence ≤8 words) for a window: `opener.line`
  plus the hook's `vo` fit in `package.TIP_PROMISE_SECONDS` (10) at her pace on
  the take (`TIP_TAKE_WORDS_PER_SECOND`, presenter.WORDS_PER_SECOND ×
  TAKE_TEMPO: ~29 words in Spanish, ~34 in English). Today the paid on-camera cap
  (32 words with the handoff) is usually the tighter bound; the window is what
  holds if that cap is raised. The hook's on-screen line must share a key word or
  the figure with the hook (`package._check_promise`; the classic format compares
  the first sentence): the viewer reads what they hear. Then a **critic**
  (`plugins/shorts/critic.py`, one cheap model call per clean submission,
  `SHORTS_CRITIC_MODEL`, bar `SHORTS_CRITIC_MIN`, default 7/10) scores whether the
  promise is said by the end of the hook, what the viewer gains or what pain goes
  away, in the second person, with the article's figure or deadline and the tool
  or trade named; below the bar `submit` refuses with its reasons and a better
  hook, prefixed `guionista:`. No key,
  a network error or a nonsense answer never block a short (`SHORTS_CRITIC=off`
  switches it off). The rubric is Guionista's evidence in
  `~/fizz/salida/guionista/alcance/`.
- **Hook patterns rotate (2026-10-17).** Every package names how its hook grabs
  the viewer: `hook_pattern`, one of `package.HOOK_PATTERNS` (`result-first`,
  `pain-first`, `contrarian`, `number-payoff`, `question`, `warning`). `submit`
  refuses a package without one, and refuses a pattern used by the last 3 shorts
  in the same language (`ledger.HOOK_PATTERN_WINDOW`; a `render_failed` short
  never aired and takes no slot). The ledger records it per short, and the
  `ledger` action lists `hook_patterns_taken` per language. Where the form is
  mechanical the label must match the hook: a first sentence that asks is
  `question` and nothing else, and `number-payoff` needs a figure in digits in
  that sentence. The field is optional in `validate_package` on purpose: the
  renderer validates packages from whichever Hermes is deployed, and Actions
  picks up a merge before the pod does. The per-pattern performance read-back
  waits on a YouTube re-authorisation (§3.2).
- **The article's own pictures.** The renderer fetches the article and puts its
  cover (`og:image`) behind the cards; a card marked `"visual": "infographic"`
  (one per short, never the hook or the CTA) shows the article's raster
  infographic (`figure.article-infographic img`). Only pictures on the
  article's host; a missing one is a log line, never a failed render.
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
- **Generated scenes.** At most 3 per short, only on hook/point/stat/list/quote
  beats, and a `scene` that asks for text, letters, numbers, charts, logos or
  brands is rejected (`package.py`): the model garbles lettering, and a figure it
  drew would bypass the grounding check. Spend is capped in `genvideo.py` (§3.5).
- **Presenter.** Only the hook and the CTA are said to camera, in ≤24 words, and
  the presenter never introduces themself (`package.py`). It shares the scenes'
  budget and plans first (§3.7).

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

Actions cost: ~7 min per short, 2 shorts a day, ~450 min/month. `br41s/hermes-sandbox`
is a **public** repo, and GitHub-hosted runners are free and unmetered for public repos,
so these minutes never draw on the account's Actions allowance or spending limit. The
flip side: run logs and the `short-*` artifacts are readable by anyone signed in to
GitHub for their 7-day retention. Packages and renders carry only published blog content.

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
8. **Analytics needs a re-minted token.** `scripts/youtube_oauth.py` now asks
   for `yt-analytics.readonly` and `youtube.readonly` as well as
   `youtube.upload` and `youtube.force-ssl`: YouTube Analytics `reports.query`
   (views, average view duration and percentage, shares) reads with those two.
   The live refresh token was minted before that and still carries only the
   upload scopes (checked 2026-10-17). To read performance back, enable the
   **YouTube Analytics API** on the same Cloud project, re-run the script from
   main, replace `YOUTUBE_REFRESH_TOKEN` in Zeabur and restart the service.

### 3.3 Facebook + Instagram

**Skip this section when shorts go to Buzz (§3.6):** Facebook and Instagram are
then SocialBot's, and the tool ignores `META_*` even if they are set, so the
two systems can never post the same short twice.

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

### 3.5 Generated scenes (paid, optional)

The producer may mark up to 3 beats per short with a `scene`: the hook and the
most visual ones. The render farm generates those backgrounds with
`heygen/heygen-video-1` through OpenRouter's video API (text to video, 5–15 s,
768p; not an avatar model, no lip sync) and every other beat keeps its free
stock footage. Text and figures are always Remotion's, drawn on top.

1. On openrouter.ai, create a key **used for nothing else**, with a **daily
   limit of $2** (since the presenter, 2026-10-10; it was $1). The agents' shared key has a weekly limit that a runaway
   render would otherwise drain, and the key's own limit is the hard stop.
2. Repo secret **`SHORTS_OPENROUTER_API_KEY`** (Settings → Secrets → Actions).
   Only a real dispatch passes it to the render; the push smoke test never spends.
3. Optional repo *variables*: `SHORTS_AI_DAILY_CAP_USD` (default 2.00),
   `SHORTS_AI_SHORT_BUDGET_USD` (default 1.00), `SHORTS_AI_PRICE_PER_SECOND`
   (default 0.015, the 768p launch price, which ends after October; raise it to
   the real price then, or fewer scenes fit than the budget allows).

How the cap holds: each short plans its presenter clips and then its scenes
against its own budget ($1.00, so the two daily shorts stay within $2
together), then against the day's cap using what OpenRouter
reports the key has spent today (UTC). Paid renders queue in one concurrency
group, so the second short starts after the first and reads its spend. They
used to run together: on 2026-10-13 six Avatar IV takes were pending at once
and OpenRouter refused the third take of each with a 402 while the account
had credit. The render log now prints the key's limit and remaining balance
at start, and a 402 carries OpenRouter's own reason; the next test, alone,
showed it: "Insufficient credits" (`limit_source: openrouter_credits`) on the
third take, the key $1.91 from its limit. OpenRouter holds credit for every
video in progress, so Lucía's takes now go one at a time, and the first one
refused stops the rest (the short cannot pass QA without her). A scene that does not fit, a 402, a model
error or a 10-minute timeout leaves that beat on its `broll`: a short never
fails for a paid clip. The manifest records each scene's id, status, real cost
and reason; the ledger keeps `ai_scenes` and `ai_spend_usd`, and the publisher's
summary on Telegram shows the day's spend. A short with a generated scene is
uploaded with YouTube's `containsSyntheticMedia: true`.

Not set through the Meta API: Meta's own "AI info" label. Check how Instagram and
Facebook label the first live shorts with generated scenes before relying on it.

### 3.6 Who publishes where: Hermes and Buzz (2026-10-10)

BigLobster runs two agent teams. One factory and one owner per network:

| | Owner | Why |
|---|---|---|
| Shorts from blog posts | **Hermes** (this studio) | runs in the cloud 24/7, checks every figure, QA-gates every render. VideoDesk in Buzz no longer makes them |
| Bespoke videos, edits of Brais's recordings, clips | **VideoDesk** + **Guionista** (Buzz) | creative work that needs conversation and Brais's "aprobado vN" |
| YouTube | **Hermes** | official API (§3.2) |
| Facebook, Instagram, X | **SocialBot** (Buzz) | already runs those accounts: Zernio, community replies, DMs, UTMs, its calendar |

The relay accepts only canonical media: a file with metadata or a
non-canonical metadata channel is refused with a 422 (found on the first live
hand-off, 2026-10-15). `buzz_share.canonical` attaches stripped copies
(`master.buzz.mp4`, streams copied, no tags; `cover.buzz.jpg`, no APPn/COM
segments); a PNG is refused whatever is done to it, so the cover stays a JPEG.

The hand-over is one Buzz message. Once a short is live on YouTube, the
publisher's `handoff` posts it to the shorts channel: `master.mp4`,
`story.mp4` and `cover.jpg` attached, the Instagram, Facebook and X copy, the
YouTube link, an AI-content note when the short has generated scenes or an
avatar, and `@SocialBot`. SocialBot publishes and replies in that thread with
the links. **Shadow mode never posts to Buzz**, so nothing reaches SocialBot
until `SHORTS_PUBLISH_MODE=live`. A failed post is not marked and is retried on
the next run. Code: `plugins/shorts/buzz_share.py`, through Block's `buzz` CLI,
which the image builds from a pinned commit (Dockerfile, `buzz_cli` stage).

Setup, once:

1. In the Fizz repo, create Hermes's own Buzz identity with
   `~/fizz/identidad-hermes.sh` (its own key, Brais's attestation, profile
   "Hermes"). Never reuse a Fizz bot's key.
2. In Buzz, create the channel `#shorts` and add Hermes and SocialBot.
3. Zeabur service env: `BUZZ_RELAY_URL`, `BUZZ_PRIVATE_KEY`, `BUZZ_AUTH_TAG`
   (from step 1) and `SHORTS_BUZZ_CHANNEL` (the channel's UUID). Optional:
   `SHORTS_BUZZ_MENTION` (default `SocialBot`). Not in `INJECT`: they are
   BigLobster's identity, never a profile's.
4. Deploy. The same variables also bring up Hermes's Buzz gateway adapter
   (`website/docs/user-guide/messaging/buzz.md`). Set `BUZZ_CHANNELS` to the
   shorts channel so it only watches that one. With `BUZZ_ALLOWED_USERS`
   empty it answers nobody; add Brais's npub there to talk to Hermes in Buzz.

#### Series episodes: "Agentes BigLobster" (2026-10-16)

- **What they are:** long horizontal explainers (2–3 min, ES and EN), one per agent BigLobster rents. Guionista and VideoDesk make them in Buzz at zero cost (Fizz repo, `tasks/video-serie-agentes.md`); YouTube stays Hermes's.
- **The request:** when an episode is finished in both languages, VideoDesk posts one message in the series channel. Its first line is `#publicar-episodio`, followed by a JSON block:
  - `episodio` and `agente` (the page slug);
  - per language, `video` (the relay `/media/` URL of the master), optional `srt`, `titulo`, `descripcion` and `etiquetas`.
- **The Publisher's run:** it calls `shorts_studio` action `series`, and `plugins/shorts/series.py` does the rest:
  1. Reads the requests, only from the pubkeys in `SERIES_BUZZ_FROM`.
  2. Downloads each master with `buzz media get`. This path has no 20 MB cap; the gateway's inbound attachments do.
  3. Checks that it is horizontal, has audio and lasts 30–900 s.
  4. Uploads it as a regular video (`youtube.upload(..., short=False)`: no `#Shorts`, a `watch?v=` link), with the SRT as captions.
  5. Replies in the request's thread with both links, mentioning the sender.
- **Safety:**
  - A ledger keyed by the message id (`workspace/shorts/series/ledger.json`) means nothing is uploaded twice, and a run that stops halfway is finished by the next one.
  - A malformed request gets one reply listing every problem.
  - After 3 failed attempts the sender is told once.
  - Shadow mode publishes nothing.
- **Quota:** an episode is about 4,000 units (two uploads and two caption tracks) and the shorts use about 4,100, so `SERIES_MAX_PER_DAY` defaults to 1.
- **Where the links end up:** VideoDesk records the ids in the series queue. The website shows each video once its id is in `site/_data/videosAgentes.json` (biglobster repo).
- **Optimisation (SEODesk):** once an episode is up, SEODesk posts `#actualizar-video` in the same channel, with a JSON block:
  - `episodio` and `agente`;
  - per language it rewrites (either or both), `id` (the YouTube id), `titulo`, `descripcion` (chapters, links, hashtags), `etiquetas` and optional `portada` (the relay `/media/` URL of a JPEG or PNG of 2 MB at most).
  - The same `series` run applies it with `youtube.update` (`videos.update` plus `thumbnails.set`, about 100 units) and replies in the thread.
  - Only from `SERIES_BUZZ_FROM` and only for ids in the series ledger, so it can never touch the blog Shorts or another video. Each request is applied once (`workspace/shorts/series/updates.json`), with the same 3 attempts. Privacy is never changed.
  - A custom thumbnail needs a phone-verified channel; if YouTube refuses it, the reply says so and the rest still applies.

Setup, once:

1. Add Hermes and SEODesk to the series channel (Media Forgery, the video team's channel), so Hermes can read it and SEODesk can post there.
2. Zeabur service env:
   - `SERIES_BUZZ_CHANNEL` (that channel's UUID);
   - `SERIES_BUZZ_FROM` (VideoDesk's and SEODesk's hex pubkeys, comma-separated);
   - optional: `SERIES_YOUTUBE_PRIVACY` (default `public`) and `SERIES_MAX_PER_DAY` (default 1).
   - The Buzz credentials are the shorts hand-off's. Nothing goes in `INJECT`.
3. Deploy and sync the Publisher prompt (`scripts/sync_prompt_drift.py --source shorts/biglobster-shorts-publisher.prompt`).

### 3.7 The presenter: Lucía and Martín (paid, optional)

The hook and the CTA can be said to camera by BigLobster's presenter: Lucía in
the Spanish short, Martín in the English one. The producer marks those beats
`presenter: true`; the render farm sends the line and the presenter's fixed
photo (`shorts/studio/presenters/{lucia,martin}.jpg`) to `heygen/avatar-iv`
through the same OpenRouter key, and HeyGen voices it and animates the face and
hands (`presenter.py`). The clip's own audio is the beat's voice, so the beat
lasts as long as they speak; Remotion plays it full-frame, keeps the face clear
and puts the beat's words on a card over the chest, with a name tag.

Budget: the same per-short and per-day caps as the scenes, and the presenter
plans first: it is what people stay for. Estimates use
`SHORTS_PRESENTER_PRICE_PER_SECOND` (default 0.05) on ~2.4 words a second, so a
20-word hook is ~$0.47 and a 12-word CTA ~$0.30. A beat with a presenter clip
needs no background, so its `scene` is generated only if the clip is not made.
Anything refused, failed or late puts the line back on the Edge voice-over over
the template: a short never fails for a presenter clip.

Voice: HeyGen's stock voices, required by Avatar IV for a script — Amelia
(Spanish, female) for Lucía and Andrew (English, male) for Martín
(`presenter.VOICES`). Repo variables `SHORTS_PRESENTER_VOICE_ES` /
`SHORTS_PRESENTER_VOICE_EN` override them with another HeyGen voice id; the 400
OpenRouter returns without a voice lists the stock ones. The manifest's
`presenter` block records every clip's id, status, cost and reason; the ledger adds the presenter's spend to
`ai_spend_usd` and counts `presenter_beats`. A short with a presenter clip is
uploaded with `containsSyntheticMedia: true`.

Proven 2026-10-10 by the first dispatch: without a voice the request is refused
(400), `voice_id` goes directly under `provider.options.heygen`, and the refusal
fell back cleanly (voice-over, the hook's scene generated instead, QA passed).

A new presenter photo: vertical, front-facing, three-quarter body with the face
in the top third, plain background, no text on the clothes (the model redraws
lettering badly).

### 3.8 The tip format: Lucía opens, Martín narrates

The Producer writes every short in the tip format (`"format": "tip"`), and
`shorts_studio submit` refuses any other. Owner's rules: real new videos, one
valuable tip worth saving or sharing, **each presenter with the same voice in
every video**, Spain Spanish, never a Latin American accent, and pay a model
only for what changes from short to short (reviews of 2026-10-11). The
two-presenter design is the owner's.

What a tip short is, in order:

1. **Opener** (`opener.line`, ≤18 words): Lucía's joke, to camera, in a
   **setting** that fits the article (`opener.setting`, English: she in front,
   he at work behind her: a workshop, a factory floor, a kitchen...). An
   image model builds it from both photos (`presenter.setting_image`, ~$0.09).
   Models are tried in order until one answers: `SHORTS_SETTING_IMAGE_MODEL`
   (comma-separated) or `presenter.SETTING_MODELS`. Gemini refused both paid
   tests with a 403 "violation of provider Terms Of Service" and FLUX.2 Pro
   answered, so FLUX goes first; a refusal costs nothing. Rules here stay
   general, because every short must look different (owner): the picture
   obeys physics. Avatar IV moves her hands and leaves the rest still, so
   the setting prompt keeps her hands free and objects on surfaces (the
   first paid test's tray of bread stayed floating in the air).
2. **Hook**, Lucía on camera, in the same setting.
3. **Handoff** (`opener.handoff`, ≤8 words, names Martín). Since 2026-10-14
   the joke, the hook and the handoff are **one Avatar IV take** (owner: more
   natural than three cuts, and one OpenRouter credit hold instead of three).
   She speaks to the camera with her name tag on screen during the joke, the
   promise card comes in where the joke ends (`overlayFrom`, by the joke's
   share of the words) and on her last sentence she turns to Martín and says
   it to him (`HANDOFF_MOTION`); the take keeps 0.6 s after the words so the
   turn is seen, then it cuts to his first card. There is no shot of Martín
   reacting: a frozen last frame with a push-in read as a stall (owner,
   2026-10-12 and 10-13), and a generated nod cost $0.65 (Hailuo 3).
4. **3–5 cards** (before/after, steps, a figure, a key line, the caveat that
   avoids the usual mistake; 60–110 spoken words in all, ~40–55 s with the
   take and the outro: the free part is where the tip becomes something the
   viewer can apply, owner 2026-10-15; Lucía's take is the paid part and does
   not grow, 32 words still), narrated by
   Martín from a bubble playing **his loop, recorded once** (look up, point,
   look back; `presenter.bubble_loop`). The cards are paper (`brand` palette:
   cream, red, ink; `Tip.tsx` `CardZone` wraps them in a `Panel`) over the
   article's blurred cover (`Backdrop`), and one of them may be the article's
   infographic (`InfographicCard`, the beat's `visual`). The karaoke runs three
   words a page in white over the dark backdrop (`voice.paginate`, `Karaoke.tsx`).
5. **CTA**, a card too, in Martín's voice: save it or share it (checked in
   `package.py`).
6. **Outro**, added by the renderer and the same in every short: the full
   article on biglobster.top, then "follow us for more tips", as an animated
   card (`Tip.tsx`, `OutroCard`). No exclamation marks in its text: Edge
   reads them as a jump in pitch, and it sounded overexcited.

**Martín's English voice is Kokoro, on the runner, for free; his Spanish
voice is Azure's `es-ES-Tristan`, Spain Spanish.** The engine is chosen per
language (`voice.DEFAULT_ENGINES`; `SHORTS_TTS_ENGINE_ES` / `_EN` override one
language, `SHORTS_TTS_ENGINE` both): Kokoro's Spanish male voices read with a
Latin American accent to the owner on the first live shorts (2026-10-15), and
Martín speaks Spain Spanish (owner's rule, 2026-10-11). Kokoro (since 2026-10-14:
the owner compared VideoDesk's shorts in Buzz, voiced by Kokoro at its own
pace, with the Studio's and chose that sound). `voice.py` runs Kokoro-82M
(Apache-2.0, `kokoro-onnx`, int8 model and voice pack from the kokoro-onnx
release, cached by the workflow) at speed 1.0, with no tempo shift, and gets
the word timings for the karaoke by reading the audio back with faster-whisper
(`small`, CPU) and aligning what it heard to the script, so the captions show
the script's words at the heard times (`voice.align_words`, the way
VideoDesk's `subtitular --guion` does it). `SHORTS_TTS_ENGINE=azure` (repo
variable) switches the narrator to Azure Speech (official REST, the same
neural voices; the free F0 tier gives 500k characters a month) when
`SHORTS_AZURE_SPEECH_KEY` (Actions secret) and `SHORTS_AZURE_SPEECH_REGION`
are set, with Edge TTS behind it: Microsoft changed the Edge endpoint's
anti-abuse token on 2026-10-10/12 and edge-tts 7.2.8 got a 403 on nearly
every call (edge-tts issue #490). If Kokoro itself fails, the line falls
through to Azure, then Edge. Every free line is voiced before anything is
paid (`build.prevoice`), so a voice outage costs nothing.

**Voices.** Lucía speaks with her HeyGen voice (`presenter.HOST_VOICES`,
chosen by the owner: es `ec971490…` (the first, `a78e0a4d…`, sounded too
young on the avatar), en `47aff89a…`; override with
`SHORTS_HOST_VOICE_ES`/`_EN`). Avatar IV takes a script and a `voice_id`;
it does NOT take a voice track as a data URL (it wants a public HTTPS URL),
which is why her lines are voiced by HeyGen and not by our TTS. Martín
narrates with Kokoro: `voice.KOKORO_VOICES`, es `em_alex`, en `am_michael`;
the owner picks between the Spanish male voices (`em_alex`, `em_santa`) after
hearing the smoke renders, with the repo variables `SHORTS_KOKORO_VOICE_ES` /
`_EN`. The package still carries Martín's neural name (`package.TIP_VOICES`,
the HD `es-ES-Tristan:DragonHDLatestNeural` and `en-US-AndrewMultilingualNeural`),
which Kokoro maps to its voice for the language and Azure uses as is; an HD
voice takes no prosody rate and is sped up after synthesis (`voice.HD_TEMPO`,
1.12x, Martín read slowly to the owner on 2026-10-13), and each of Lucía's
takes plays faster too, lips and voice together (`presenter.TAKE_TEMPO`).
A line of Lucía's whose take was not made is said by her free voice of the
same kind (`voice.KOKORO_HOST_VOICES`, `ef_dora`/`af_heart`, or
`presenter.HOST_FALLBACK_VOICES` under Azure). Two characters, two voices,
each the same in every short.

**What is paid.** Only Lucía's one take (opener + hook + handoff ≤32 words,
~12 s at $0.05/s, about $0.5–0.6), budgeted before it is sent
(`presenter.host_takes`). If the take is not made (a refusal, a 402, a model
error, the 10-minute timeout), **the short still goes out**: her lines are
said by her free voice over the hook card, QA records a warning
(`build.presenter_missing_warning`) and the manifest's `presenter` block says
why (owner, 2026-10-14). Until then a missing take failed QA with the setting
and part of the day's budget already spent, and no daily short passed from
12 to 14 October: OpenRouter holds credit for every video in progress, and
with three takes the second or third was refused "Insufficient credits"
(`limit_source: openrouter_credits`) while the key's daily limit still had
room. One take is one hold. A render without a key (the push smoke test)
keeps its card-only fallback and reports nothing.

**The narrator's loop is recorded by the owner, once** (Google Flow, frames
to video with the presenter's photo as first and last frame), and committed as
`shorts/studio/presenters/assets/<slug>-loop.mp4`, without audio. Martín's is
in (2026-10-12; 8 s, he looks up to the left at the cards and points up), and
so is Lucía's (`lucia-loop.mp4`, same gesture, for when she narrates).
`presenter.LOOP_FRAMING` says how it sits in the bubble (scale, and the point
centred, left of his face so the pointing hand stays in). The renderer never
generates a loop (`presenter.bubble_loop` only looks it up); without one the
bubble shows the photo. A loop for Lucía is for when she narrates; the roles
are fixed (she opens, he narrates) until the owner says otherwise.

**Cuts.** The take opens and closes with a still, silent second; the
timeline cuts it at its speech (`media.speech_bounds`) and runs his first
card after 0.06 s, so the handoff reads as one conversation instead of a face
that freezes between lines.

**Music.** Jazz and lounge only (`sources.ELEGANT_MUSIC_PAGES`, Mixkit), at
`media.BED_VOLUME` 0.06 under the voices, ducked further while anyone speaks.

Cost per short: setting ~$0.07 + Lucía's one take ~$0.5–0.6 (her three
takes on 2026-10-12 cost $0.24, $0.14 and $0.17; one take of the same words
is about the same money and one credit hold) ≈ $0.6–0.7, inside the $1 per
short. Everything else is free. QA accepts the silence of Martín's turn
(`qa.check_master(quiet=...)`).

Framing: `presenter.FACE_Y` is where each photo's face is (Martín's loop),
and `SETTING_FACE_Y` where the setting prompt puts Lucía's; the zoom and the
bubble centre on them.

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
| Voice (Kokoro on the runner; Azure Speech F0 and Edge as fallbacks), footage (Pexels/Mixkit), music (Mixkit) | €0 |
| Presenter (HeyGen Avatar IV) and generated scenes (HeyGen Video 1), both via OpenRouter | ≤ $1 per short and $2/day together, capped in code and on the key (§3.5, §3.7); ~$0.05/s said to camera, ~$0.10–0.15 per 8 s scene at the launch price |
| Render (GitHub Actions) | €0 — public repo, unmetered runners |
| YouTube Data API, Meta Graph API | €0 |
| X | €0 (posted by hand) |
| Remotion | €0 while BigLobster has ≤3 employees. Above that, or when selling renders to clients at scale, the Company licence ("Automators", $0.01/render, $100/month minimum) applies |

## 6. Proven vs not yet proven

**Proven.** Full renders on Actions with real Edge TTS, in both languages, with
footage for 6 of 6 queries (after the Mixkit slug fallback) and a Mixkit bed.
EN: 137 words, 63.0s. ES (Álvaro): 148 words, 66.0s. Remotion takes 300–340s
per short with all cores on the 2-vCPU runner (388s at Remotion's default of one
core), ~7 min end to end. Covers, ducking, two-pass loudness at −14.0 LUFS and QA
passed on every run. The same pipeline also ran
offline in the dev sandbox. 45 unit tests cover the package contract,
grounding, ledger, tool flows, GitHub client, publishers (mocked HTTP) and the
ffmpeg loudness/Story path.

**Not yet proven.** Live YouTube and Meta uploads (no credentials in development),
including Instagram's resumable upload for Stories and the unpublished-photo
cover trick. They are written to the documented API, but the first live-mode run
is the real test, which is why shadow mode comes first. Also unproven: a Pexels
key in the render farm. And generated scenes: `genvideo.py` follows the request
shape `generar-plano` uses in `br41s/fizz` (OpenRouter `/api/v1/videos`: submit,
poll, download) and is covered with a mocked OpenRouter, but no paid clip has
been rendered yet. The first dispatch with the key set is the test; its log's
`[studio] ai:` lines say what happened to each scene.

## 7. Operating it

- `shorts_studio ledger` (from chat) shows the last shorts, their state and URLs.
- A short published on YouTube whose hand-off to SocialBot failed stays listed by
  `status` as `published` with `handoff_pending: true` until a run hands it off.
- **Going live:** the shorts handed to a human in shadow mode during the last
  3 days (`studio_tool.LIVE_CATCHUP_DAYS`) become `ready` on the next `status`
  and the publisher takes them; older shadow hand-offs stay as they are.
- A short stuck in `rendering`: open its `run_url`. After an hour with no run it
  becomes `render_failed`.
- `qa_failed`: `qa.errors` says why.
- A failed post is not tried again (`ledger.MAX_FAILED_ATTEMPTS`, owner
  2026-10-13): the producer takes another post the next day.
- Instagram `pending`: Instagram was still processing. The second publisher run finishes it.
- The render farm alone: Actions → *Shorts Studio* → Run workflow, with a
  `request_id` and a package (JSON → gzip → base64).

## 8. Tuning the look

Palettes and motifs live in `shorts/studio/remotion/src/theme.ts` and are mirrored in
`package.py` (a test keeps them in step). Preview with
`cd shorts/studio/remotion && npm ci && npm run studio`. Any change to the studio
triggers the smoke render of both samples on push, once per voice engine
(`short-smoke-<lang>-kokoro` and `-azure`): download the two Spanish ones from
the run and listen to Martín before picking `SHORTS_KOKORO_VOICE_ES`.

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
