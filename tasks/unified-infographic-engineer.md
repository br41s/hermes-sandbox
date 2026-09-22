# Unified Infographic Engineer

One agent, one prompt, one canvas contract, deterministic verification before
anything is written. Replaces two prompts that had drifted into different
formats, different failure protocols and different quality.

Plan: `~/.claude/plans/effervescent-finding-stream.md` (approved 2026-09-22).

## Why

The CEO reported four defects: too simple, spelling errors, colour codes
rendered, edges cut off. Measured across the 82 SVG infographics published to
biglobster.top:

| Defect | Count |
|---|---|
| `<style>`/`<defs>` inside the SVG (theme-fighting, stripped on client sites) | 37 |
| `viewBox` not `0 0 800 H` (type renders up to 3x bigger on one article than another) | 30 |
| Emoji used as an icon (renders as a tofu box) | 18 |
| CSS token that does not exist (silently renders black) | 6 |
| Text overflowing its own card — the visible "cut off" | 11 articles |
| Text outside the canvas | 2 articles |

Root cause was never the model. The prompt asked the agent to do layout
arithmetic in its head, on a graphic it cannot render, with nothing downstream
checking the result.

The rendered colour codes were the **client** agent: it generated raster images
with a diffusion model, which misspells its own labels and draws hex codes as
pixels. Removing raster generation deletes that whole class.

## Status

### hermes-sandbox — branch `feat/unified-infographic-engineer`
- [x] `infographic/validate_infographic.py` — the pre-publish gate, stdlib-only,
      invoked by path (cron denies `execute_code`). Checks canvas contract, text
      vs. canvas, **text vs. its own container box**, type floor, forbidden
      elements, `var()` in geometry, multi-value geometry, per-stack design
      tokens, emoji, Spanish spelling.
- [x] `infographic/inter-metrics.json` + `measure_inter_metrics.js` — real Inter
      advance widths measured in the browser, replacing the prompt's 0.55 guess.
- [x] `infographic/infographic-engineer.prompt` — the single shared prompt. Lane
      resolved at runtime from `BL_SITE_URL`.
- [x] `infographic/bl-site-package-infographic.prompt` — deleted.
- [x] `scripts/provision_bl_client.py` — points at the shared prompt, `image_gen`
      dropped from the toolset.
- [x] `tests/scripts/test_bl_prompts_no_customer_names.py` — glob extended so the
      renamed shared prompt stays covered by the leak check.
- [x] `Dockerfile` — `hunspell hunspell-es` (neither the binary nor any Python
      spell library was in the image).
- [x] `tests/infographic/test_validate_infographic.py` — 36 tests.
- [x] `AGENT_RENTAL_SETUP.md` — the "inserts one inline SVG" row was stale; the
      comment-sentinel note was wrong for rendered pages.
- [ ] `CLAUDE.md` — record the unification and the validator.

### biglobster — branch `feat/infographic-guard-and-viewer`
- [x] `lib/infographic-guard.mjs` — build guard for the rules that need no font
      metrics. Skips legacy raster figures (171 of 253 are PNGs). Keyed on the
      path relative to `site/`, because `blog/x.html` and `en/blog/x.html` are
      different articles sharing a basename.
- [x] `lib/infographic-baseline.txt` — 65 article/code pairs, the ratchet.
- [x] `scripts/infographic-guard.test.mjs` — 25 tests, incl. the "typography is
      not emoji" cases that would otherwise fail correct articles.
- [x] Registered in `eleventy.config.mjs`; `npm run build` exits 0, and exits 1
      with a deliberate new defect.
- [ ] Ampliar viewer: fit-to-width + fullscreen zoom control, replacing the
      `min-width: 680px` / `overflow-x: auto` mobile rule.
- [ ] `projects/biglobster-web/INFOGRAPHICS.md`, `web/DESIGN.md`, `CLAUDE.md`.

### bl-site-package
- [ ] Same Ampliar viewer in `web/style.css` + `web/site.js`.
- [ ] Version bump 1.8.4 → 1.8.5.

### Prod rollout — each step needs a go-ahead at the time
- [ ] Deploy the hermes image (hunspell + validator + prompt).
- [ ] `sync_prompt` both jobs; normalise Shoroban's absolute `prompt_source` to
      repo-relative; drop `image_gen` from its live `enabled_toolsets`.
- [ ] A/B one manual BigLobster run on `xiaomi/mimo-v2.6-pro` vs the current
      `deepseek/deepseek-v4.1-flash`; compare quality and cost per run.
- [ ] Pin both jobs via `cron.jobs.update_job()` (the CLI has no model flag).
- [ ] Ship bl-site-package 1.8.5 to Shoroban.

## Open

- **The review turn truncates.** The last run ended
  `max_iterations_reached(16/16)` on its second (`bg-review`) turn; the main turn
  stops cleanly at ~35/90. That second turn is exactly the refinement pass that
  would catch a bad layout. Find where the 16 comes from and raise it.
- **mimo has never run an agentic tool loop here** — only one-shot judging, as
  the auditor's system gate. That is what the A/B is for.
