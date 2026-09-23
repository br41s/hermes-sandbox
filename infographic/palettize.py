#!/usr/bin/env python3
"""Snap a generated art plate onto a stack's exact art palette.

This is what actually produces a house style. Measured on 2026-09-23: two plates
generated back to back with an identical style contract came back with different
borders, different shading and different off-palette hues. Quantised, they read
as one system. No model call, so it cannot introduce a new failure mode.

Pillow only — the production image has PIL but no numpy, and this has to run
inside a cron agent with nothing to install.

  python3 palettize.py --src raw.jpg --dst plate.png --palette pal.json
"""
import argparse, json, sys
from PIL import Image, ImageFilter

ORDER = ("ground", "pale", "slate", "ink", "accent", "deep")


def load_palette(path):
    with open(path, encoding="utf-8") as fh:
        p = json.load(fh)
    missing = [k for k in ORDER if k not in p]
    if missing:
        sys.exit(f"palette is missing {', '.join(missing)} — generate it with palette.py")
    return [tuple(int(p[k].lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)) for k in ORDER]


def palette_image(colours):
    flat = [c for rgb in colours for c in rgb]
    img = Image.new("P", (1, 1))
    img.putpalette(flat + [0] * (768 - len(flat)))
    return img


def _lab(c):
    """sRGB -> CIELAB. Plain Python: this runs on <=64 colours, never per pixel."""
    out = []
    for v in c:
        v /= 255.0
        out.append(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4)
    r, g, b = out
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = (0.2126 * r + 0.7152 * g + 0.0722 * b)
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
    f = lambda t: t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116
    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def _nearest(colour, targets):
    cl = _lab(colour)
    return min(targets, key=lambda t: sum((a - b) ** 2 for a, b in zip(cl, _lab(t))))


def palettize(src, dst, colours, smooth=3):
    """Snap in CIELAB, not sRGB, without numpy.

    Median-cut reduces the plate to at most 64 colours in C, then each of those
    64 is mapped to its nearest target in Lab in plain Python and written back
    into the palette. Perceptual quality, 64 iterations instead of a million.
    sRGB-nearest is visibly worse: on one plate it sent 10% of the pixels that
    belong on ink to the accent instead, because a dark olive is numerically
    closer to olive than to near-black while looking nothing like it.
    """
    im = Image.open(src).convert("RGB")
    if smooth:
        # Flatten diffusion noise first, or single stray pixels survive the snap
        # and speckle the flat fields.
        im = im.filter(ImageFilter.MedianFilter(smooth))
    q = im.quantize(colors=64, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    flat = q.getpalette() or []
    mapped = []
    for i in range(0, min(len(flat), 64 * 3), 3):
        mapped.extend(_nearest(tuple(flat[i:i + 3]), colours))
    q.putpalette(mapped + [0] * (768 - len(mapped)))
    out = q.convert("RGB")
    out.save(dst, optimize=True)
    total = out.width * out.height
    counts = dict((c, n) for n, c in (out.getcolors(maxcolors=4096) or []))
    return [(hex_of(c), counts.get(c, 0) / total) for c in colours]


def hex_of(c):
    return "#%02X%02X%02X" % c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True, help="explicit: deriving it by swapping '.png' "
                                                 "silently overwrote a .jpg source in place")
    ap.add_argument("--palette", required=True, help="JSON from palette.py")
    args = ap.parse_args()
    shares = palettize(args.src, args.dst, load_palette(args.palette))
    print(args.dst.split("/")[-1] + "  " + "  ".join(f"{h}:{s:.0%}" for h, s in shares))


if __name__ == "__main__":
    sys.exit(main())
