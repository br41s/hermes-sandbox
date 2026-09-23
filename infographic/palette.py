#!/usr/bin/env python3
"""Derive a stack's art palette from the tokens the SITE ACTUALLY RENDERS.

Never read a client's palette from bl-site-package/web/style.css. That file
ships defaults; each client overrides --accent in an inline <style> on its own
page. Shoroban's real accent is #b0ba1c (olive) against the package default
#b8391c (biglobster's terracotta) — two characters apart, and five posters
shipped in the wrong brand colour before anyone looked.

Also computes the accent's TEXT variant. An accent is not automatically legible:
terracotta works as both a fill and a label colour, which hid the rule; olive
scores 1.96:1 on its own plate ground and is a fill colour only. `atext` is the
accent darkened until it clears 4.5:1, and `on_accent` is whichever of ink or
ground is legible on top of an accent fill.

  python3 palette.py --stack biglobster
  python3 palette.py --stack bl-site-package --url https://example.com/blog/a-post
"""
import argparse, json, re, sys, urllib.request

CONTRAST_TARGET = 4.5

# biglobster is a repo we control, so its values are pinned rather than fetched.
BIGLOBSTER = {
    "ground": "#F4EDE6", "pale": "#DDE1E9", "slate": "#5B6375",
    "ink": "#1B1A19", "accent": "#B8391C", "deep": "#D4622A",
}
# Shipped defaults for a client site. Only used when a page cannot be read, and
# then only for the neutrals — the accent MUST come from the page.
CLIENT_FALLBACK = {"pale": "#DDE1E9", "slate": "#5B6375", "ink": "#111318"}


def rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def hexs(t):
    return "#%02X%02X%02X" % tuple(max(0, min(255, round(v))) for v in t)


def luminance(h):
    c = []
    for v in rgb(h):
        v /= 255
        c.append(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4)
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def mix(colour, pct, other):
    return hexs(tuple(pct * a + (1 - pct) * b for a, b in zip(rgb(colour), rgb(other))))


def text_variant(accent, ground):
    """Darken the accent until it is legible as text on the plate ground."""
    for pct in range(100, 4, -5):
        candidate = mix(accent, pct / 100, "#000000")
        if contrast(candidate, ground) >= CONTRAST_TARGET:
            return candidate
    return "#000000"


def read_page_accent(url):
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-infographic/1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", "replace")
    found = None
    for block in re.findall(r"<style[^>]*>(.*?)</style>", html, re.S):
        for name, value in re.findall(r"(--[\w-]+)\s*:\s*(#[0-9A-Fa-f]{3,8})", block):
            if name == "--accent":
                found = value  # last wins: later blocks override earlier ones
    return found


def build(stack, url=None):
    if stack == "biglobster":
        p = dict(BIGLOBSTER)
    else:
        if not url:
            sys.exit("--url is required for a client stack: the accent lives on the page, "
                     "not in the package stylesheet.")
        accent = read_page_accent(url)
        if not accent:
            sys.exit(f"No --accent found in an inline <style> on {url}. Do NOT fall back to "
                     "the package default: that is biglobster's colour, not this client's.")
        if len(accent.lstrip('#')) == 3:
            accent = "#" + "".join(ch * 2 for ch in accent.lstrip("#"))
        p = dict(CLIENT_FALLBACK)
        p["accent"] = accent.upper()
        p["ground"] = mix(p["accent"], 0.12, "#FFFFFF")   # --accent-light
        p["deep"] = mix(p["accent"], 0.82, "#000000")     # --accent-hover
    p["atext"] = text_variant(p["accent"], p["ground"])
    p["on_accent"] = max((p["ink"], p["ground"]), key=lambda c: contrast(c, p["accent"]))
    p["_contrast"] = {
        "ink_on_ground": round(contrast(p["ink"], p["ground"]), 2),
        "slate_on_ground": round(contrast(p["slate"], p["ground"]), 2),
        "accent_on_ground": round(contrast(p["accent"], p["ground"]), 2),
        "atext_on_ground": round(contrast(p["atext"], p["ground"]), 2),
        "on_accent": round(contrast(p["on_accent"], p["accent"]), 2),
    }
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stack", required=True, choices=["biglobster", "bl-site-package"])
    ap.add_argument("--url", help="a rendered article URL on the client's site")
    args = ap.parse_args()
    p = build(args.stack, args.url)
    bad = [k for k, v in p["_contrast"].items() if k != "accent_on_ground" and v < CONTRAST_TARGET]
    print(json.dumps(p, indent=2))
    if bad:
        print(f"\nFAIL: {', '.join(bad)} below {CONTRAST_TARGET}:1", file=sys.stderr)
        return 1
    print("\nART palette (6): " + "  ".join(p[k] for k in ("ground","pale","slate","ink","accent","deep")),
          file=sys.stderr)
    print(f"TEXT: ink {p['ink']} · muted {p['slate']} · accent-as-text {p['atext']} "
          f"· on an accent fill {p['on_accent']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
