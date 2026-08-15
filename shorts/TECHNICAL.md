# Social Shorts — technical design

The eighth rentable bl-site-package SKU. One blog post per run becomes 3–5 vertical
MP4s for Instagram Reels and TikTok, plus a `captions.md` of per-network copy.

This document is written for review: it records not just what was built but what was
*rejected*, what is *proven* versus merely believed, and where the sharp edges are.

---

## 1. Scope

**In.** Read one not-yet-processed post from the client's blog, write 3–5 short-form
scripts on distinct viral archetypes, render each to a finished 1080×1920 MP4 with
voiceover and burned-in captions, write per-network caption copy, mark the post done.

**Out.** Posting to Instagram or TikTok. The agent produces upload-ready files; a human
uploads them. See §9 for why this is not laziness.

**Language.** Videos and caption copy are English (international Reels/TikTok audience).
The cron report stays Spanish, like the rest of the fleet.

---

## 2. The build/reuse decision

The first useful finding was how little needed building. An inventory of what upstream
already ships:

| Need | Already present | Verdict |
|---|---|---|
| Text-to-speech | `tools/tts_tool.py` — 11 providers, **Edge TTS default, free, no key** | reuse |
| AI video generation | `tools/video_generation_tool.py` + `plugins/video_gen/fal` — Veo 3.1, Kling v3, LTX-2.3, Pixverse, all 9:16, t2v **and** i2v | reuse |
| Blog read/write | `tools/bl_site_publish_tool.py` — `list_posts`, `get_post`, `update_blog_post` | reuse |
| ffmpeg | `Dockerfile:31` apt-installs it | reuse |
| Client isolation, cron, BYOK keys | `hermes_cli/profiles.py`, `cron/jobs.py`, `scripts/provision_bl_client.py` | reuse |
| **Assembly of the above into a video** | — | **build** |
| **Stock B-roll source** | — | **build** |

So the deliverable is one plugin and one prompt, not a video stack.

### Why a plugin, not a core tool

`CLAUDE.md` states the invariant: *"The core is a narrow waist; capability lives at the
edges. Every core tool is sent on every API call."* Only profiles that rented this SKU
need `shorts_render`. As `plugins/shorts/` (`kind: backend`) its schema costs zero tokens
for every other profile in the fleet. `plugins/spotify/` is the shape it copies.

---

## 3. Architecture

```
cron job (profile=<client>, daily, off-peak stagger)
  │
  └─ shorts/bl-site-package-shorts.prompt
       │
       ├── bl_site_publish  list_posts → get_post        ── pick oldest post lacking the sentinel
       ├── shorts_render    action=stock_search          ── free portrait B-roll (Pexels)
       ├── video_generate   9:16, 4s          [optional] ── AI hook clip, client's FAL_KEY
       ├── shorts_render    action=render                ── ── ── ── ── ── ── ── ── ──┐
       ├── write_file       captions.md                                               │
       ├── bl_site_publish  update_blog_post             ── append <!-- shorts:auto -->│
       └── send_message     MEDIA:…          [optional]                               │
                                                                                      │
   plugins/shorts/render.py ◄──────────────────────────────────────────────────────────┘
       ├── voice.py     synthesize each scene line separately → ffprobe its duration
       ├── (per scene)  normalise B-roll to 1080×1920, loop/trim to the line's length
       ├── (per scene)  pad the voiceover to the exact scene length
       ├── concat       scenes → silent.mp4 ; scene audio → voice.wav
       ├── captions.py  → captions.ass
       └── final        burn captions, duck music, loudnorm −14 LUFS, faststart
```

### Module responsibilities

| File | Responsibility |
|---|---|
| `plugins/shorts/__init__.py` | Tool schema, dispatch, output-path bounding |
| `plugins/shorts/stock.py` | Pexels Videos API client, portrait rendition selection |
| `plugins/shorts/voice.py` | TTS via `tools.tts_tool`, `ffprobe` duration measurement |
| `plugins/shorts/captions.py` | ASS subtitle generation, wrapping, cue timing |
| `plugins/shorts/render.py` | ffmpeg orchestration |

---

## 4. The render pipeline

All ffmpeg work is subprocess argv. There is no `moviepy`, `pydub`, `imageio` or
`ffmpeg-python` anywhere in this repo, and none was added — `tools/tts_tool.py` and
`tools/transcription_tools.py` both shell out, and that is the convention.

Every invocation runs with the temp directory as its **cwd** and references bare
filenames. This is not cosmetic: `-vf "subtitles=..."` needs its path escaped for colons
and backslashes inside a filtergraph, and getting that wrong fails in ways that only show
up on certain paths. Using relative names removes the problem class entirely.

### 4.1 Scene length follows speech

The pipeline synthesises **each scene's line separately**, then measures it:

```python
vo_path  = synthesize(vo_text, workdir / f"vo_{index:02d}.mp3")
duration = probe_duration(vo_path) + SCENE_TAIL_PAD   # 0.35s
```

This is the load-bearing design decision. It buys frame-accurate captions with **no
word-level timestamps from the TTS engine**. Edge TTS does emit `WordBoundary` events, but
consuming them would weld the pipeline to Edge specifically — and the licensing analysis
(§6) says the engine has to stay swappable. Per-scene measurement works identically across
all 11 providers.

`synthesize()` returns the *actual* output path rather than the requested one, because
`text_to_speech_tool` transcodes to Opus when the session platform is Telegram, changing
the extension out from under the caller.

### 4.2 Visual normalisation

```
-stream_loop -1 -i clip -t <dur>
-vf scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,setsar=1
```

`-stream_loop -1` before `-i` loops a clip shorter than its scene; `-t` bounds it either
way, so short and long sources both work with one command. `increase` + `crop` fills the
frame rather than letterboxing — pillarboxed footage reads as reposted content on both
platforms. A scene with no clip renders on a flat dark background (`0x101418`).

No Ken Burns `zoompan`: the sources are video and already move, and `zoompan` on a video
input is disproportionately expensive.

### 4.3 Audio

Each scene's voiceover is padded to the exact scene length (`apad` + `-t`), so the audio
and video concat lists stay in lockstep by construction rather than by arithmetic. The
optional music bed is ducked with `sidechaincompress` keyed on the voice, and the final
mix is normalised to **−14 LUFS**, the level both Instagram and TikTok normalise toward —
delivering louder just gets attenuated, and quieter sounds amateur.

### 4.4 Captions

Generated as ASS and burned in. Muted autoplay is the default viewing mode on both
platforms, so captions are the product, not an accessibility afterthought.

- Canvas is `PlayResX/Y = 1080×1920`, matching the render canvas, so no coordinate scaling.
- `MarginV = 520`, keeping text clear of the bottom ~20% where platform UI sits.
- At most two lines on screen; longer scene text splits into successive cues whose
  durations are **proportional to character count**, so words track speech without needing
  per-word timings.
- Caption text is escaped (`{`, `}`, `\`) so post content can never inject ASS override
  blocks.

---

## 5. Guardrails

Enforced in `render.py`, not in the prompt — a prompt-level rule is a suggestion:

| Guard | Value | Rationale |
|---|---|---|
| `MAX_TOTAL_DURATION` | 60s | Beyond this it stops being a short. **Hard error, not a trim** — a silently truncated video ships with its punchline missing and nobody notices |
| `MAX_SCENES` | 12 | Rejected before any synthesis happens |
| `MAX_CLIP_BYTES` | 80 MB | Bounded download |
| `FFMPEG_TIMEOUT` | 300s | Per invocation |
| Output path | inside `HERMES_HOME` | `tools/path_security.py`; relative paths resolve under `workspace/` |
| Clip URLs | `tools/url_safety.py` | Blocks private/link-local targets (SSRF) |

The cron scheduler's timeout is **600s of inactivity, not wall clock**
(`cron/scheduler.py:3870` — *"the job can run for hours if it's actively calling tools"*).
A whole video renders inside one `shorts_render` call, so the prompt renders **one video at
a time** to keep any single call far from that ceiling. Measured: ~75s for a 30s video.

---

## 6. Licensing — two constraints that are load-bearing

This is a product rented to paying customers, so licence terms are engineering
constraints, not footnotes.

**Text-to-speech.** XTTS-v2 ships under Coqui's CPML (non-commercial), and Coqui shut down
in January 2024, so no commercial licence can be bought at any price. F5-TTS has MIT
*code* but **CC-BY-NC-4.0 weights**. Neither can legally voice a customer deliverable.
Baseline is therefore Edge TTS (free, no key); the upgrade path is Kokoro (Apache-2.0),
reachable with **zero code** via `tts.providers.<name>` `type: command` in `config.yaml`.
This is why §4.1 refuses to depend on engine-specific timing metadata.

**Stock footage.** The Pexels licence permits commercial use and modification but forbids
redistributing content unaltered. The renderer only ever consumes clips into a composite
with voiceover and captions; it never hands one through untouched.

---

## 7. Tenancy and keys

Every credential is the client's own — the fleet's standing "your tokens, your bill" rule.

| Key | Owner | Required |
|---|---|---|
| `OPENROUTER_API_KEY` | client | yes (all SKUs) |
| `PEXELS_API_KEY` | client | **yes for `shorts`** (`AGENTS_REQUIRING_PEXELS`) |
| `FAL_KEY` | client | no — buys the optional AI hook |

**Pexels is required, not optional.** Free does not mean shareable: Pexels issues one key
per account at 200 req/hour and 20k/month. A single BigLobster key across the fleet would
cap every client at once and let one heavy tenant throttle the rest into background-only
videos. Requiring it also beats degrading — a video product that ships without footage is
worse than one we declined to sell. Provisioning validates the key live before creating
anything, and fails on a `429` as well as `401/403`: a key with no headroom is as useless
here as a rejected one.

The boot hook needs **both halves** of the contract:

```python
inject  = [..., "OPENROUTER_API_KEY", "PEXELS_API_KEY", ...]   # BigLobster's own profiles
_exclude = ("OPENROUTER_API_KEY", "PEXELS_API_KEY")            # withheld from rented tenants
```

`inject` alone is the bl-shoroban bug (2026-07-31): every boot overwrote the client's key
with BigLobster's, silently billing tenant runs to us. `_exclude` alone would lose the
rotation repair for BigLobster's own profiles that the grow-shop incident (2026-06-05)
added. `tests/test_biglobster_github_token_propagation.py` now asserts both together and
parses the lists out of the boot script rather than copying them — the previous
hand-maintained copy had already drifted on `GSC_SERVICE_ACCOUNT_B64`.

`--pexels-key` deliberately does **not** default to `PEXELS_API_KEY` in the environment.
That default is the shared-key behaviour in disguise: omit the flag on a rental and the
client's searches silently bill to BigLobster's quota.

---

## 8. Cost

| Component | Cost |
|---|---|
| Script writing | LLM tokens (`deepseek/deepseek-v4-flash`, the existing rental default) |
| Voiceover — Edge TTS | €0 |
| Stock B-roll — Pexels | €0 (client's free tier) |
| Assembly — ffmpeg on CPU already paid for | €0 |
| **Baseline per video** | **€0 beyond tokens** |
| Optional 4s AI hook — fal | ~$0.20–0.60 (`ltx-2.3` cheapest, `veo3.1` dearest), client's `FAL_KEY` |

The AI hook is not a separate concept in the code — it is scene 1's `clip_url`. The agent
calls the existing `video_generate` tool and passes the URL through, falling back to stock
when the key is absent or generation fails. One codepath, graceful degradation, mirroring
the established `PORTADA` pattern in `gap-hunter/bl-site-package-gap-hunter.prompt`.

---

## 9. Rejected alternatives

| Rejected | Why |
|---|---|
| **Wan2GP self-hosted** | Needs an NVIDIA GPU with ≥6 GB VRAM. Zeabur Frankfurt is CPU-only (`nvidia-smi` absent). Would mean renting a second GPU host and building an API in front of it |
| **HuggingFace `video_gen` provider** | Its free-tier model is `damo-vilab/text-to-video-ms-1.7b` — a 2023 model at 576×320. Unusable for content meant to perform |
| **`optional-skills/creative/hyperframes`** | Renders by capturing a Chromium page frame-by-frame: ~900 frames for a 30s/30fps clip, minutes of CPU each, ×5 videos ×N clients daily, plus a runtime `npx` install. Direct ffmpeg filtergraphs are an order of magnitude cheaper |
| **XTTS-v2 / F5-TTS** | Non-commercial licences (§6) |
| **Auto-posting to IG/TikTok** | Instagram requires a two-step container publish from a *publicly reachable URL* and caps at 25 API posts/24h; TikTok's Content Posting API needs per-app approval for `video.publish`. A separate SKU with its own OAuth onboarding |
| **Word-level caption timing** | Ties the pipeline to one TTS engine for a marginal gain (§4.1) |
| **Video upload to the client panel** | `bl_site_publish` has `upload_image` but no video action; adding one is a bl-site-package change, not a hermes-sandbox one |

---

## 10. Verification — what is proven, and what is not

### Proven

- **A real render.** ffmpeg installed in a sandbox and the pipeline run end to end.
  `ffprobe` confirms exactly 1080×1920, 30fps, h264 + stereo AAC @48 kHz, duration
  12.90s against a computed 12.90s (four scenes of measured speech + tail pad).
- **Extracted frames** confirm captions burn in legibly inside the safe area, cue
  splitting works on long text, a 1920×1080 landscape source crops correctly to portrait,
  and a 2s clip loops to fill a 3.75s scene.
- **130 tests pass** across the affected suites, 26 of them new for this plugin: ffmpeg
  argv construction, ASS timing arithmetic, the duration/scene caps, Pexels rendition
  selection, path-security rejection, SSRF refusal, temp-dir cleanup on failure.
- **Plugin registration** through the real plugin manager: `shorts_render` lands in the
  `shorts` toolset.
- **Provisioning both ways**, with a decoy `PEXELS_API_KEY` in the environment: ordering
  `shorts` without a client key is refused and leaves no profile behind; ordering with one
  writes the client's value, and the environment's value does not leak in.
- **Boot hook** shell syntax and all four embedded Python heredocs parse.

### Not proven

- **The live Edge TTS call.** The sandbox's egress proxy blocks its websocket to
  `speech.platform.bing.com` (a real 403 handshake, not a code fault). The render was
  proven with locally generated audio of deliberately varying lengths substituted at the
  `synthesize()` boundary. **This is the first thing to watch on the first production run.**
- **A live Pexels query.** No key was available; the client is covered by unit tests
  against a recorded response shape.
- **Whether the scripts are actually funny.** The archetypes and hook rules encode known
  short-form practice, but the guardrails are mechanical. Watch the first five videos
  before pointing this at a paying customer.

### Reproducing the render check

```bash
pytest tests/plugins/test_shorts_plugin.py -q
python3 -c "
import json, plugins.shorts as s
print(s.handle_shorts_render({
  'action': 'render',
  'output_path': 'shorts/probe/01.mp4',
  'scenes': [{'vo': 'Stop watering your plants at night.',
              'caption': 'STOP watering at night'}]}))"
ffprobe -v error -show_entries stream=width,height,r_frame_rate \
        -show_entries format=duration -of default=noprint_wrappers=1 <path>
```

---

## 11. Deployment

Plugins are baked into the image, so this is not a config-only change:

1. `gcloud builds submit --config=cloudbuild.yaml` → GHCR
2. Restart the **container** in Zeabur — not "Restart Gateway" in the panel, which does not
   re-run `cont-init` and so never applies the boot-hook change
3. `PEXELS_API_KEY` on the Hermes service is only needed if **BigLobster itself** runs
   `shorts`; rented clients bring their own via `--pexels-key`
4. Prompt edits reach already-scheduled jobs only through
   `scripts/sync_prompt_drift.py --source shorts/bl-site-package-shorts.prompt --yes`

**Before selling it:** run the prompt as a one-shot against the `biglobster` profile,
watch the videos, and diff the post body to confirm only the sentinel line changed.

---

## 12. Known risks

| Risk | Mitigation / status |
|---|---|
| Edge TTS is an undocumented Microsoft endpoint with no SLA | Provider is swappable in config; Kokoro (Apache-2.0) is the paid-quality fallback with no code change |
| Pexels 429 under a client's own burst | Surfaced as an explicit error, so it lands in the job report rather than silently producing background-only video |
| Stock footage is generic | Inherent to the free tier; the AI hook clip exists partly to give the opening 4s something specific |
| Render cost grows with fleet size | CPU-bound and serialised by cron's per-profile sequencing; 5 videos ≈ 6 min of CPU per client per day |
| First-run TTS failure | Unproven path (§10). Job reports the error and marks nothing, so it retries next day |
