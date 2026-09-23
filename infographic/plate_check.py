#!/usr/bin/env python3
"""Gate a generated art plate before it is composited.

Layer 3 of the consistency model in ART-DIRECTION.md. The prompt contract alone
does not produce a house style and quantisation only fixes colour; these are the
measured post-conditions that catch a plate which is the right colour and still
unusable.

  python3 plate_check.py --plate p.png --palette pal.json [--ocr]

Exits 0 when the plate may be used, 1 with a named finding otherwise. A failing
plate is REGENERATED with a new seed, never patched.

THE TEXTLESS CHECK IS NOT SOLVED, and --ocr is off by default because of it.
Measured 2026-09-23 against a positive control — a 1600x600 crop whose top half
is a 56px headline — google/gemini-3.1-flash-lite, anthropic/claude-haiku-4.5
and openai/gpt-5.4-mini ALL answered "NO, there is no text". The image does
arrive (1573 prompt tokens against 19 with no image, and the description is
accurate) but it is downscaled to roughly one tile, and the text does not
survive. A check that passes a page of text is worse than no check: it grants
false assurance on the exact failure this whole design exists to prevent.

What makes that tolerable, and it is the point of the architecture: the artwork
carries NO information, so text baked into a plate is ugly, never wrong. Every
fact lives in the SVG layer. In the old raster design the text in the image WAS
the information, which is why a misspelling there was a defect a reader could
act on. Here the residual risk is cosmetic.
"""
import argparse, base64, json, os, sys, urllib.request
from PIL import Image

# Pillow only: the production image has PIL but no numpy, and this has to run
# inside a cron agent with nothing to install.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from palettize import load_palette, palette_image  # noqa: E402

# What this actually guards is BREATHING ROOM, so it measures the largest flat
# field — the single most common colour — and not the ground colour specifically.
# Measuring the ground failed two perfectly good plates where the model used the
# accent as a full-bleed background: legitimate, and scored 26%.
#
# Below the floor the plate is cluttered with no calm area. Above the ceiling the
# subject is tiny, off-centre or cropped — a plate at 82% had the seated figure's
# head cut off and half the frame empty. Both bounds caught real failures.
FIELD_MIN, FIELD_MAX = 0.25, 0.75
FRAME_BAND = 0.02          # outer 2% of each edge
FRAME_UNIFORMITY = 0.90    # a drawn border is a near-uniform band
OCR_MODEL = os.environ.get("HERMES_PLATE_OCR_MODEL", "google/gemini-3.1-flash-lite")


def check_geometry(plate, palette, findings):
    im = Image.open(plate).convert("RGB")
    q = im.quantize(palette=palette_image(palette), dither=Image.Dither.NONE)
    w, h = q.size
    px = q.load()

    counts = q.histogram()[:len(palette)]
    share = max(counts) / float(w * h)
    if share < FIELD_MIN:
        findings.append(("field-too-busy", f"the largest flat field is {share:.0%} of the plate "
                         f"(floor {FIELD_MIN:.0%}). Nothing on it rests; it will read as clutter "
                         f"beside another plate."))
    elif share > FIELD_MAX:
        findings.append(("field-too-empty", f"the largest flat field is {share:.0%} of the plate "
                         f"(ceiling {FIELD_MAX:.0%}). The subject is tiny, off-centre or cropped."))

    # A self-drawn border is a near-UNIFORM outer band whose colour differs from
    # the ring just inside it. Testing "the outer band is not the ground colour"
    # instead condemns every full-bleed illustration — it failed two good plates.
    band = max(1, int(min(w, h) * FRAME_BAND))

    def ring(lo, hi):
        seen = []
        for y in range(h):
            for x in range(w):
                d = min(x, y, w - 1 - x, h - 1 - y)
                if lo <= d < hi:
                    seen.append(px[x, y])
        return seen

    outer, inner = ring(0, band), ring(band, 2 * band)
    if outer and inner:
        o_dom = max(set(outer), key=outer.count)
        i_dom = max(set(inner), key=inner.count)
        uniform = outer.count(o_dom) / len(outer)
        if uniform >= FRAME_UNIFORMITY and o_dom != i_dom:
            findings.append(("self-drawn-frame", f"the outer {FRAME_BAND:.0%} band is "
                             f"{uniform:.0%} one colour and differs from the ring inside it. The "
                             f"model has drawn its own border, which fights the slot's rounded "
                             f"corner."))
    return share


def check_textless(plate, findings):
    """ADVISORY ONLY — see the module docstring. It misses obvious text.

    Kept because a YES is still worth acting on; a NO means nothing at all.
    """
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        print("note: OPENROUTER_API_KEY is not set, skipping the advisory text probe.",
              file=sys.stderr)
        return
    b64 = base64.b64encode(open(plate, "rb").read()).decode("ascii")
    body = json.dumps({
        "model": OCR_MODEL, "max_tokens": 120,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Does this illustration contain ANY readable text, letters, "
             "digits, words, signage or logos? Ignore abstract marks. Answer strictly as "
             "NO or YES: <what it says>."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}]}).encode()
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=body,
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=120))
        answer = (r["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:                                   # noqa: BLE001 — advisory
        print(f"note: the advisory text probe could not run ({exc}).", file=sys.stderr)
        return
    if not answer.upper().startswith("NO"):
        findings.append(("text-in-artwork", f"The model reports text in the plate: {answer[:160]}. "
                         f"Artwork never carries information — regenerate it."))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plate", required=True)
    ap.add_argument("--palette", required=True, help="JSON from palette.py")
    ap.add_argument("--ocr", action="store_true",
                    help="ALSO ask a vision model about text. Advisory only: it under-reports "
                         "badly (see the module docstring). A NO from it proves nothing.")
    args = ap.parse_args()

    findings = []
    share = check_geometry(args.plate, load_palette(args.palette), findings)
    if args.ocr:
        check_textless(args.plate, findings)

    name = args.plate.split("/")[-1]
    if not findings:
        print(f"OK  {name}: largest field {share:.0%}, no self-drawn frame.")
        return 0
    print(f"FAIL  {name}: {len(findings)} finding(s).\n")
    for code, msg in findings:
        print(f"  [{code}] {msg}\n")
    print("Regenerate the plate with a new seed. Never patch it.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
