# Art direction — illustrated infographics

Rules for the raster artwork the Infographic Engineer commissions, and for how
it combines with the SVG data layer. One set of rules for biglobster and every
rented site; only the palette values differ, and they are derived, not chosen.

Status: **live, and the agent produces it, 2026-09-23.** Four biglobster
articles (ES+EN) and seven Shoroban articles carry the format; both stacks render
and enlarge it (bl-site-package 1.8.8). The prompt, the pipeline scripts and the
client-lane upload path are all shipped and synced to both cron jobs. Everything
published so far was still built by hand through these scripts — the first
agent-produced posters are the next scheduled runs.

---

## The rule everything else hangs off

**The artwork never carries information. Every fact is SVG.**

No text, no number, no chart, no axis, no map, no logo inside the generated
image. The illustration carries mood, subject and metaphor; the SVG layer
carries the argument.

This is what makes unattended illustration defensible, and it is the same
principle as "facts are the site's, prose is the agent's" in `CLAUDE.md`. Raster
generation was removed from this agent on 2026-09-22 because a diffusion model
misspells its own labels, draws hex codes as if they were text and crops its
composition. Every one of those failures is a failure *about text inside the
image*. An image with no information in it cannot commit any of them — the worst
case is a picture that is merely ugly, never a picture that is wrong.

A corollary that is easy to lose: the artwork is decoration with a job, so a
plate that fails its checks is **discarded and regenerated, never patched**. The
data layer is untouched by any of it.

---

## Consistency is enforced in three layers, not one

The prompt alone does not produce a house style. Measured on 2026-09-23, two
plates generated back to back through the same route, with the identical style
contract appended verbatim to both:

| | plate A | plate B |
|---|---|---|
| self-drawn white border frame | yes | no |
| shading | soft painterly gradients | flatter, brighter |
| off-palette hues | teal | yellow |
| pseudo-text on props | yes (notice board) | no |

Same model, same settings, adjacent calls. This is exactly the "five unrelated
styles in five runs" failure, and it is reproducible. So:

**Layer 1 — the prompt contract.** Necessary, insufficient on its own. Sets
subject and asks for the style.

**Layer 2 — deterministic palette quantisation.** Every plate is snapped to the
stack's fixed art palette in CIELAB before it is used at all
(`palettize`, no model call, cannot introduce a new failure mode). This is what
actually produces the house style: after quantisation the same two plates above
read as one system, the teal and the yellow are gone, and the painterly shading
becomes flat posterised blocks — the screen-print look the prompt asked for and
did not get. Measure the result, do not assume it: print the share of pixels
that landed on each palette entry.

**Layer 3 — measured post-conditions.** A plate that fails any of these is
regenerated with a new seed, not accepted:

| Check | Threshold | Why |
|---|---|---|
| largest flat field | **25–75%** of pixels on the single most common colour | below the floor nothing on the plate rests; above it the subject is tiny, off-centre or cropped. Both bounds caught real failures — 25% too dense, 82% with a figure's head cut off. Measuring the GROUND colour instead was the first cut and it condemned two good full-bleed illustrations, so it measures the largest field whatever colour that is |
| no self-drawn frame | the outer 2% band is ≥90% one colour AND differs from the ring inside it | the model likes to add its own border, which fights the slot's rounded corner. Testing "the outer band is not the ground colour" condemns every full-bleed plate |
| textless | **UNSOLVED — see below** | |

All three live in `plate_check.py`. The first two are measured and hold; the
third does not work and is off by default.

**The textless check is not solved, and a passing result means nothing.**
Measured against a positive control — a 1600×600 crop whose top half is a 56px
headline — `gemini-3.1-flash-lite`, `claude-haiku-4.5` and `gpt-5.4-mini` all
answered "no text". The image does arrive (1573 prompt tokens against 19 with
none, and the description is accurate) but it is downscaled to about one tile and
the text does not survive. A check that passes a page of text is worse than no
check, so `--ocr` is advisory and off. A YES is still worth acting on; a NO
proves nothing.

What makes that tolerable is the architecture rather than luck: the artwork
carries no information, so text baked into a plate is **ugly, never wrong**. In
the old raster design the text in the image *was* the information.

### What a failing plate does to the run — CEO decision, 2026-09-23

Retry roughly **two** further seeds. If the plate still fails, **drop the artwork,
ship the graphic as a plain token-based SVG, and report it as a third distinct
outcome** — not as a normal success and not as `RUN FAILED`:

```
✅ Infographic added — artwork dropped
- Article: …
- Artwork: 3 plates rejected (ground share 0.22 < 0.35) — shipped as plain SVG
```

The article still gets a graphic, and the downgrade is visible the moment it
happens. Reporting it as an ordinary success is the failure mode that already bit
this job: on 2026-09-07 and 2026-09-09 it aborted at the git step and reported so
mildly that both runs were recorded as fine, and nobody noticed until someone
counted by hand. A fleet that silently stops being illustrated is the same bug
wearing a nicer hat.

A plain SVG is a correct graphic, so `RUN FAILED` would be wrong here — that is
reserved for a run that ships nothing. This third line has to be added to the
prompt's reporting section alongside `[SILENT]` and `RUN FAILED`.

---

## Palette

Per stack, **derived from the tokens the site ACTUALLY RENDERS** — never picked
by hand, never per article, and never read from the package stylesheet.

**Read the palette off the client's own rendered page, not off
`bl-site-package/web/style.css`.** That file ships defaults; each client
overrides `--accent` in an inline `<style>` on the page. Shoroban's real accent
is `#b0ba1c`, an olive. The package default is `#b8391c`, biglobster's
terracotta. Two characters apart, and five posters shipped in the wrong brand
colour before anyone looked. Fetch the article URL and read the custom
properties out of the page.

**An accent is not automatically usable as text.** Terracotta happened to work
both as a fill and as a label colour, which hid the rule. Measured against
Shoroban's plate ground:

| | contrast on `#F6F7E4` | verdict |
|---|---|---|
| accent `#B0BA1C` as text | **1.96** | unusable |
| accent darkened to 60%, `#6A7011` | **4.91** | the accent-text colour |
| ink `#111318` | 17.12 | body text |
| pale ground on an olive fill | 1.96 | unusable |
| ink on an olive fill | 8.75 | text on a bar |

So each stack needs two accent roles, computed not assumed: **ACCENT** for
fills, and **ATEXT** — the accent darkened until it clears 4.5:1 on the ground —
for any accent-coloured label. Text sitting *on* an accent fill takes whichever
of ink or ground clears 4.5:1 there. Compute both at build time and assert them;
do not eyeball a swatch.

biglobster, for reference:

| Role | Value | Source |
|---|---|---|
| ground | `#F4EDE6` | warm cream (fixed) |
| pale | `#DDE1E9` | `--border` |
| slate | `#5B6375` | `--text-muted` |
| ink | `#1B1A19` | warm near-black (fixed) |
| accent | `#B8391C` | `--accent` |
| warm | `#D4622A` | `--accent-teal` |

Shoroban, derived the same way and deliberately nothing like it:

| Role | Value | Source |
|---|---|---|
| ground | `#F6F7E4` | `--accent-light` = color-mix(accent 12%, white) |
| pale | `#DDE1E9` | `--border` |
| slate | `#5B6375` | `--text-muted` |
| ink | `#111318` | `--text-primary` |
| accent | `#B0BA1C` | `--accent`, olive — **fills only** |
| deep | `#909917` | `--accent-hover` |
| atext | `#6A7011` | accent darkened to 60% — accent-coloured **text** |

Six entries in the art palette, no more: quantisation to a larger palette stops
flattening and the posterised look is lost. `atext` is a seventh colour for the
SVG layer only, never for the artwork.

The style contract has to name the palette in words too. Asking for "terracotta
red, warm orange" and then quantising to olive maps every warm region onto
whatever is nearest and the illustration loses its structure — the plates were
regenerated with "olive green, dark olive, no warm colours, no red, no orange"
rather than re-quantised.

**The plate does not follow the theme. CEO decision, 2026-09-23, explicit.**
A raster has fixed pixels. The choices were a fixed printed plate, or two
generations per slot with no guarantee they match. The plate is fixed, so it
reads as a printed insert: correct in light mode, a warm card on a dark page in
dark mode. Rendered in both before the call was made.

The consequence is that the **SVG data layer over a plate uses these fixed values
too, not `var(--token)`** — it sits on the plate's cream, so it cannot follow the
page. That inverts today's token rule for poster-format figures and the
validator has to learn the difference.

---

## Style contract

Appended verbatim to every subject, in every run, on every site:

> Flat vector editorial illustration, screen-print risograph look. Bold even
> outlines, solid flat colour fills, no gradients, no shading, no texture, no
> 3D, no photorealism. Five colours only: warm cream, near-black ink, terracotta
> red, warm orange, muted slate grey. No text, no letters, no numbers, no
> signage, no logos. Subject fully inside frame, generous empty margin, plain
> flat cream background.

"Generous empty margin" is load-bearing twice: it is what makes a centre-crop to
the slot's aspect ratio safe, and it is what keeps the ground share above the
floor.

## Subject rules

**May contain**: people at work, hands, tools, machines, vehicles, buildings,
interiors, furniture, plants, crates, everyday objects, an empty premises.

**May not contain**: anything with a glyph on it, and anything that invites one —
signage, screens showing content, documents face-on, book covers, keyboards,
packaging, number plates, clocks with numerals. Also: no chart, graph, axis,
arrow-as-data, map, territory outline or flag; no logo or brand mark; no frame,
border or vignette of its own; nobody identifiable as a real person.

A map is worth naming separately. Territory shapes are *facts*. If a graphic
needs Galicia's provinces, the outline is SVG path data we control, never
generated art.

---

## Composition

**The system is fixed; the composition is not.** Palette and its roles, type
sizes, the 800 canvas, the flat quantised plate with its 8-unit corner, the
hairline rules and the closing accent rule are identical on every run — that is
what makes a corpus look like one publication. What must vary is the shape of
the page.

A wide art band on top and three labelled card rows underneath, every time, is a
template with different words in it. The first six posters built by hand all
used it, and by the sixth the repetition was the most visible thing about them.
So each run varies two axes against the previous graphic on the same site:

| Axis | Options |
|---|---|
| Schematic | comparison matrix · quantity · process with state · decision tree · timeline · before/after · unit chart · two scales |
| Layout archetype | hero band · split · bookend · inset · full bleed · diptych · spine |

The archetype decides the plate count — diptych and bookend need two, the rest
one — so it has to be chosen **before** generating, at $0.04 a plate.


**Canvas** `viewBox="0 0 800 <height>"`, unchanged from today. Portrait for a
poster; 1100–1300 is the working range.

**The template owns the geometry; the artwork fills slots in it.** This is the
part that is easy to get backwards. In every reference the CEO gave, the
illustration *is* the layout — the winding road positions the stages, the
footprint silhouette packs the bubbles. If the model draws the scene freely, the
SVG cannot know where the road bends and the labels cannot land on it; you get
art behind and unrelated data floating on top, which looks worse than a clean
chart. So the metaphor's skeleton stays in SVG, where we control it exactly, and
the raster supplies the characters and props at known coordinates.

**Slots are composited into one image before use.** A client site cannot position
two `<img>` elements: the sanitizer allows no `style` attribute, no `div`, no
`span`, and a class cannot carry per-article coordinates. So plates are pasted
onto the poster ground at template coordinates by a committed script, and one
image is uploaded. That also removes alignment risk entirely — we did the
compositing, at numbers we chose.

**Markup**, identical on both stacks:

```html
<figure class="article-infographic article-infographic--poster">
  <img src="…" alt="" width="800" height="1260" loading="lazy">
  <svg viewBox="0 0 800 1260" role="img" aria-labelledby="…">…</svg>
  <figcaption>…</figcaption>
</figure>
```

`alt=""` is deliberate: the artwork is decorative, and the `<svg>`'s `<title>` +
`<desc>` carry the accessible description of the whole graphic, exactly as today.

**CSS**, to be added to `web/style-blog.css` and bl-site-package's `web/style.css`:

```css
.article-infographic--poster { position: relative; }
.article-infographic.article-infographic--poster > img {
  display: block; width: 100%; height: auto;
  border: 0; border-radius: 12px; box-shadow: var(--shadow-md);
}
.article-infographic.article-infographic--poster > svg {
  position: absolute; top: 0; left: 0;
  width: 100%; height: auto;
  font-family: var(--font-body);
}
```

Two details in there cost a debugging pass each, and both fail *silently*:

- **`height: auto`, never `height: 100%`.** `inset: 0` stretches the SVG over the
  figcaption as well; the viewBox then letterboxes rather than distorts, and
  every label lands ~32px below the artwork it belongs to. Measured: svg 1183px
  against an img of 1118px. With `width:100%` + `height:auto` the SVG takes its
  height from its own viewBox ratio and the two layers match to the pixel.
- **No border on the img in this variant.** A border is drawn outside the 100%
  width, so the two layers stop sharing an origin. Radius and shadow are fine.

**The existing lightbox drops the data layer.** `initInfographicZoom` in
`web/main.js` clones `fig.querySelector(':scope > svg, :scope > img')` — one
node. On a poster figure that is the `<img>`, so "Ampliar" would open the
artwork alone, with every label and number gone, and nothing would look broken.
Both stacks' viewers have to clone the art stack before any of this ships. On a
phone the poster's smallest label renders at ~6px, so the viewer is not a nicety
here; it is the only way the graphic is readable at all.

---

## Which model actually draws this, per lane

Already settled in the shipped config — no key to buy, nothing to build:

| Lane | Provider | Model | Key |
|---|---|---|---|
| biglobster (and any profile with no `FAL_KEY`) | OpenRouter | `bytedance-seed/seedream-4.5` | the shared OpenRouter key |
| rented tenant provisioned with `--fal-key` | in-tree FAL path | `fal-ai/flux-2/klein/9b` | the client's own `FAL_KEY` |

`docker/cont-init.d/03-biglobster-config` stamps `image_gen.provider: openrouter`
onto the main config and every profile on every boot, and **skips that override
for any profile carrying its own `FAL_KEY`** (`byok_images`), which keeps the
in-tree FAL path. It also deletes the key from a tenant reconciled before that
rule existed. The absence of a `FAL_KEY` on biglobster is not a gap — it is what
routes biglobster to OpenRouter, deliberately, so a tenant's bill never lands on
us and ours never lands on them.

The OpenRouter image lane is an existing plugin, `plugins/image_gen/openrouter/`.
Nothing here needs a new provider.

**The prototype was drawn by FLUX.1-dev on fal, not by seedream.** That was the
route available locally. It is representative of the *tenant* lane and not of
biglobster's, so the specific look of the plates will differ in production. The
architecture does not: layer 2 quantisation and the layer 3 post-conditions are
model-agnostic by construction, and the style-drift finding that motivates them
is if anything more likely to hold across two different model families. Re-run
the two-plate drift check on seedream before locking the style contract.

## Cost and latency

Measured 2026-09-23 against FLUX.1-dev on fal — this is the **tenant** lane:

- **Latency** 1.7–2.8s per plate. Two or three plates is a few seconds against a
  cron budget already measured in minutes. Not a factor.
- **Area** ~0.5 MP per run for two plates (624×480 + 560×400, generated at 2x the
  slot size for retina). At any plausible per-megapixel rate this is cents at
  most against a ~$0.10 agent run — call it +10–20% per run. The exact fal rate
  has not been re-verified today; the megapixel figure is measured, the price is
  not.
- **Payload** 800×1260 at 2x: 384 KB as PNG, **90 KB as WebP**. Ship WebP; the
  client sanitizer allows it.

seedream's per-image cost on OpenRouter has **not** been measured — it is served
from OpenRouter's Images API, so it does not appear in `/api/v1/models` with the
per-token `image_output` price the chat-completion image models carry. Measure it
on the first real run rather than quoting a number here.

---

## Known limits

- **Character continuity is not controlled.** The prototype's two plates happen
  to show a recognisably similar man because the subject descriptions matched,
  not because anything enforced it. A recurring character across plates needs a
  committed reference image and an image-to-image or style-reference lane. Until
  then, do not write a subject that depends on "the same person as the other
  plate".
- **Composition still varies between plates** after quantisation — plate A is a
  wide flat scene, plate B a tight two-shot. The ground-share floor catches the
  worst of it; camera distance in the subject line is the rest.
- **`check_design_tokens` does not forbid hex literals.** It only checks that a
  `var(--x)` names a known token, so a figure drawn entirely in hex passes the
  gate clean today — verified against this prototype, which exits 0. The prompt
  forbids hex in prose and nothing enforces it. That hole predates this work and
  should be closed whichever way the poster decision goes.
