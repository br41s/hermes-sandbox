#!/usr/bin/env python3
"""Force a generated plate onto the stack's exact art palette.
Deterministic: no model call, so it cannot introduce a new failure mode.
Median-filter first to flatten diffusion noise, then snap every pixel to the
nearest palette entry in CIELAB (perceptual, so shadows snap to ink rather
than to whatever hue happens to be numerically closest in sRGB)."""
import sys
from PIL import Image, ImageFilter
import numpy as np

# biglobster light-mode token values, plus a warm cream ground and warm ink.
PALETTE = [
    (0xF4, 0xED, 0xE6),  # ground   warm cream
    (0xDD, 0xE1, 0xE9),  # border   pale grey   (--border)
    (0x5B, 0x63, 0x75),  # slate    (--text-muted)
    (0x1B, 0x1A, 0x19),  # ink      warm near-black
    (0xB8, 0x39, 0x1C),  # accent   terracotta  (--accent)
    (0xD4, 0x62, 0x2A),  # warm     (--accent-teal)
]

def srgb_to_lab(a):
    a = a.astype(np.float64) / 255.0
    m = a <= 0.04045
    a = np.where(m, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    x = (0.4124*r + 0.3576*g + 0.1805*b) / 0.95047
    y = (0.2126*r + 0.7152*g + 0.0722*b) / 1.00000
    z = (0.0193*r + 0.1192*g + 0.9505*b) / 1.08883
    def f(t):
        return np.where(t > 216/24389, np.cbrt(t), (24389/27 * t + 16) / 116)
    fx, fy, fz = f(x), f(y), f(z)
    return np.stack([116*fy - 16, 500*(fx - fy), 200*(fy - fz)], axis=-1)

def palettize(src, dst, smooth=3):
    im = Image.open(src).convert("RGB")
    if smooth:
        im = im.filter(ImageFilter.MedianFilter(smooth))
    px = np.asarray(im)
    lab = srgb_to_lab(px)
    pal = np.array(PALETTE, dtype=np.uint8)
    pal_lab = srgb_to_lab(pal.reshape(1, -1, 3))[0]
    d = ((lab[:, :, None, :] - pal_lab[None, None, :, :]) ** 2).sum(-1)
    out = pal[d.argmin(-1)]
    Image.fromarray(out).save(dst)
    counts = np.bincount(d.argmin(-1).ravel(), minlength=len(PALETTE))
    share = counts / counts.sum()
    print(f"{dst}  " + "  ".join(
        f"#{r:02X}{g:02X}{b:02X}:{s:.0%}" for (r, g, b), s in zip(PALETTE, share)))

if __name__ == "__main__":
    # Explicit src/dst: deriving the destination by swapping ".png" silently
    # overwrote a .jpg source in place on the first real run.
    if len(sys.argv) != 3:
        sys.exit("usage: palettize.py <src> <dst.png>")
    palettize(sys.argv[1], sys.argv[2])
