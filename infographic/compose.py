#!/usr/bin/env python3
"""Composite palettized art plates onto the poster ground at template
coordinates. One output image per figure, because a client site cannot
position two <img> elements (no style attribute, no div, no per-article CSS)."""
import json, sys
from PIL import Image, ImageDraw

SCALE = 2           # retina source; the figure displays at ~720 CSS px

# Per-stack ground. Never hardcode one: it is derived from the CLIENT's own
# rendered tokens, and two sites can differ completely — biglobster's accent is
# terracotta, Shoroban's is olive. The spec carries it as "ground".
DEFAULT_GROUND = "#F4EDE6"


def _rgb(value):
    v = value.lstrip("#")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))

def cover(im, w, h):
    """Centre-crop to fill w x h without distorting. The art direction's
    'generous empty margin' rule is what makes this crop safe."""
    s = max(w / im.width, h / im.height)
    im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
    l, t = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((l, t, l + w, t + h))

def compose(spec, dst):
    W, H = spec["canvas"]
    canvas = Image.new("RGB", (W * SCALE, H * SCALE), _rgb(spec.get("ground", DEFAULT_GROUND)))
    for slot in spec["slots"]:
        x, y, w, h = (v * SCALE for v in slot["rect"])
        plate = cover(Image.open(slot["src"]).convert("RGB"), w, h)
        if slot.get("radius"):
            r = slot["radius"] * SCALE
            mask = Image.new("L", (w, h), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), r, fill=255)
            canvas.paste(plate, (x, y), mask)
        else:
            canvas.paste(plate, (x, y))
    canvas.save(dst, optimize=True)
    print(f"{dst}  {canvas.width}x{canvas.height}  ({W}x{H} @{SCALE}x)")

if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as fh:
        spec = json.load(fh)
    compose(spec, sys.argv[2])
