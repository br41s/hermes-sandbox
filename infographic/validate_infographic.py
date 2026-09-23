#!/usr/bin/env python3
"""Validate an infographic SVG before it is published. Stdlib-only, no network.

WHY THIS EXISTS: the Infographic Engineer cannot render what it draws. The old
prompt acknowledged that and asked the model to compensate with ~30 lines of
mental arithmetic ("average character width is about 0.55 x font-size"). It does
not work. Across the 50 infographics shipped to biglobster.top, 16 had text or
shapes outside the viewBox by that prompt's own formula - one badge reached
production reading "OMPRAR E INSTALA".

So the arithmetic moves here, where it is exact and cannot be skipped. Character
advances are measured from the real webfont (infographic/inter-metrics.json),
not estimated.

WHY A COMMITTED SCRIPT: the cron sandbox denies `execute_code` and
`python3 -c/-e`, so the agent cannot compute this inline. Invoking a committed
file by path is a normal execution, clears the approval gate, and is versioned
and auditable. Same convention as onsite-seo/build_sitestate.py.

WHY IT SERVES BOTH STACKS: one agent now draws for biglobster.top (git + PR) and
for bl-site-package client sites (HTTP + immediate publish). The drawing rules
are identical; only the design-token names and the sanitizer differ, so those are
the only things `--stack` switches.

USAGE
    python3 validate_infographic.py --stack biglobster < figure.svg
    python3 validate_infographic.py --stack bl-site-package --file article.html
    python3 validate_infographic.py --stack biglobster --file a.html --json

EXIT CODES
    0  clean
    1  findings (they are printed; fix them and run again)
    2  usage or parse error
    3  no infographic found in the input
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
METRICS_PATH = os.path.join(HERE, "inter-metrics.json")

# ---------------------------------------------------------------- the contract

# One canvas width for every infographic, on every stack. Before this, viewBox
# widths ran 360 -> 1000, so the same font-size rendered ~3x bigger on one
# article than another: some looked sparse and huge, others were illegible.
CANVAS_WIDTH = 800
MIN_CANVAS_HEIGHT = 200
MAX_CANVAS_HEIGHT = 1400

# 15 units on an 800 canvas renders at 14.1px in biglobster's 750.7px column,
# which is the 14px floor lib/type-scale-guard.mjs already enforces site-wide.
MIN_FONT_SIZE = 15

# Never resolved by the browser inside an SVG geometry attribute: these parse as
# SVG data types, not CSS properties, so var() is ignored and the attribute
# silently falls back to its default. Mirrors lib/svg-token-guard.mjs.
GEOMETRY_ATTRS = (
    "x y width height rx ry cx cy r x1 y1 x2 y2 dx dy points d viewbox offset"
).split()
# ...and each of these takes exactly ONE number. rx="8 8 0 0" is not the CSS
# border-radius shorthand; the browser discards it whole and draws rx=0.
SINGLE_VALUE_ATTRS = "x y width height rx ry cx cy r x1 y1 x2 y2".split()

# Stripped by the bl-site-package sanitizer (src/content/format-content.js), and
# on biglobster they are what produced infographics carrying their own
# prefers-color-scheme block, fighting the site's data-theme switch.
FORBIDDEN_TAGS = {
    "style", "script", "defs", "marker", "use", "image", "foreignobject",
    "lineargradient", "radialgradient", "stop", "clippath", "mask", "pattern",
    "filter", "animate", "animatetransform", "set",
}

# Design tokens each stack actually defines. An unknown token resolves to
# nothing and the attribute falls back to its initial value - black fill, no
# stroke - with no error anywhere.
STACK_TOKENS = {
    "biglobster": {
        "bg-base", "bg-surface", "bg-subtle", "text-primary", "text-secondary",
        "text-muted", "accent", "accent-hover", "accent-teal", "accent-light",
        "border", "font-body", "font-display", "shadow-sm", "shadow-md",
    },
    "bl-site-package": {
        "bg", "bg-subtle", "text-primary", "text-secondary", "text-muted",
        "border", "accent", "accent-hover", "accent-light", "font",
        "font-display", "radius",
    },
}

# Non-emoji symbols already used correctly across the corpus. A naive
# Extended_Pictographic match flags these, which would fail the build on
# articles that are fine.
SYMBOL_ALLOWLIST = set("→←↑↓↔⇒✓✔✗✕×·•−–—≈≤≥≠°±∞▲▼◆◀▶■□●○")

SVG_EMPTY_TAGS = {
    "rect", "circle", "ellipse", "line", "polyline", "polygon", "path",
    "use", "image", "stop", "br", "img", "animate", "set",
}

TOL = 0.5  # user units of slack before a coordinate counts as outside


class Finding:
    def __init__(self, code, message, detail=None):
        self.code = code
        self.message = message
        self.detail = detail

    def as_dict(self):
        return {"code": self.code, "message": self.message, "detail": self.detail}

    def __str__(self):
        line = f"  [{self.code}] {self.message}"
        if self.detail:
            line += f"\n      {self.detail}"
        return line


# ------------------------------------------------------------------- measuring

def load_metrics():
    with open(METRICS_PATH, encoding="utf-8") as fh:
        doc = json.load(fh)
    return doc["advances_em"], doc.get("fallback_advance_em", 0.55)


ADVANCES, FALLBACK_ADVANCE = load_metrics()


def text_width(text, font_size, weight):
    """Advance width of `text` in user units. Exact for Inter, per the measured table."""
    table = ADVANCES["600"] if weight > 450 else ADVANCES["400"]
    total = 0.0
    for ch in text:
        if unicodedata.combining(ch):
            continue  # accents composed onto the previous glyph add no advance
        total += table.get(ch, FALLBACK_ADVANCE)
    return total * font_size


# -------------------------------------------------------------------- parsing

_NUM = r"-?\d*\.?\d+(?:[eE][-+]?\d+)?"
_TRANSLATE_RE = re.compile(rf"translate\(\s*({_NUM})\s*[, ]\s*({_NUM})?\s*\)")
_SCALE_RE = re.compile(rf"scale\(\s*({_NUM})\s*(?:[, ]\s*({_NUM}))?\s*\)")
_UNTRACKABLE_RE = re.compile(r"\b(rotate|matrix|skewX|skewY)\s*\(")


def parse_transform(value, parent):
    """Fold a transform onto the parent CTM. Returns (tx, ty, sx, sy, tracked)."""
    ptx, pty, psx, psy, ptracked = parent
    if not value:
        return parent
    if _UNTRACKABLE_RE.search(value):
        # rotate/matrix/skew move geometry in ways a bounds check cannot follow
        # without a real matrix stack. Mark the subtree untracked and stay quiet
        # rather than emit confident nonsense about it.
        return (ptx, pty, psx, psy, False)
    tx, ty, sx, sy = 0.0, 0.0, 1.0, 1.0
    m = _TRANSLATE_RE.search(value)
    if m:
        tx = float(m.group(1))
        ty = float(m.group(2)) if m.group(2) else 0.0
    m = _SCALE_RE.search(value)
    if m:
        sx = float(m.group(1))
        sy = float(m.group(2)) if m.group(2) else sx
    return (ptx + psx * tx, pty + psy * ty, psx * sx, psy * sy, ptracked)


def num(attrs, key, default=None):
    raw = attrs.get(key)
    if raw is None:
        return default
    try:
        return float(str(raw).strip().rstrip("px"))
    except ValueError:
        return default


def length(raw, font_size, default=0.0):
    """Parse an SVG length that may be in em (used by dy on multi-line labels)."""
    if raw is None:
        return default
    raw = str(raw).strip()
    try:
        if raw.endswith("em"):
            return float(raw[:-2]) * font_size
        return float(raw.rstrip("px"))
    except ValueError:
        return default


class TextRun:
    __slots__ = ("text", "x", "y", "font_size", "weight", "anchor", "tracked")

    def __init__(self, text, x, y, font_size, weight, anchor, tracked):
        self.text = text
        self.x = x
        self.y = y
        self.font_size = font_size
        self.weight = weight
        self.anchor = anchor
        self.tracked = tracked


_CSS_RULE_RE = re.compile(r"\.([A-Za-z0-9_-]+)\s*\{([^}]*)\}")


def parse_css_classes(css):
    """Map class name -> text style, for SVGs that style via a <style> block.

    <style> is forbidden in new work, but 20 of the legacy infographics use it,
    and their font-size lives ONLY there. Reading it is what keeps the audit of
    the existing corpus honest: measuring those labels at the default 16 instead
    of their real 13 invents overflows that are not there.
    """
    out = {}
    for name, body in _CSS_RULE_RE.findall(css or ""):
        props = {}
        for decl in body.split(";"):
            if ":" not in decl:
                continue
            key, _, value = decl.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "font-size":
                try:
                    props["font_size"] = float(value.rstrip("px").strip())
                except ValueError:
                    pass
            elif key == "font-weight":
                props["weight"] = 700 if value in ("bold", "bolder") else int(
                    re.sub(r"\D", "", value) or 400)
            elif key == "text-anchor":
                props["anchor"] = value
        if props:
            out.setdefault(name, {}).update(props)
    return out


class SvgParser(HTMLParser):
    """Walk the SVG, resolving inherited text style and translate/scale CTMs."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.viewbox = None
        self.runs = []
        self.shapes = []          # (tag, x0, y0, x1, y1, tracked)
        self.tags_seen = []       # (tag, attrs) in document order
        self.raw_attrs = []       # (tag, attr, value) for regex-style checks
        self.css = {}             # class name -> text style, set before feed()
        self._ctm = [(0.0, 0.0, 1.0, 1.0, True)]
        self._style = [{"font_size": 16.0, "weight": 400, "anchor": "start", "family": None}]
        self._text_stack = []     # active <text> frames
        self._pending = None      # run being accumulated

    # -- style / ctm helpers

    def _push(self, attrs):
        parent = self._style[-1]
        st = dict(parent)
        if "font-size" in attrs:
            fs = num(attrs, "font-size")
            if fs is not None:
                st["font_size"] = fs
        if "font-weight" in attrs:
            raw = str(attrs["font-weight"]).strip()
            st["weight"] = 700 if raw in ("bold", "bolder") else int(num(attrs, "font-weight", 400) or 400)
        if "text-anchor" in attrs:
            st["anchor"] = str(attrs["text-anchor"]).strip()
        if "font-family" in attrs:
            st["family"] = attrs["font-family"]
        # A CSS rule beats a presentation attribute in SVG, so classes apply last.
        for cls in str(attrs.get("class", "")).split():
            st.update(self.css.get(cls, {}))
        self._style.append(st)
        self._ctm.append(parse_transform(attrs.get("transform"), self._ctm[-1]))

    def _pop(self):
        if len(self._style) > 1:
            self._style.pop()
        if len(self._ctm) > 1:
            self._ctm.pop()

    def _pt(self, x, y):
        tx, ty, sx, sy, tracked = self._ctm[-1]
        return tx + sx * x, ty + sy * y, tracked

    def _scale(self):
        return self._ctm[-1][2]

    # -- flushing text

    def _flush(self):
        if self._pending is None:
            return
        raw = self._pending.text
        text = raw.strip()
        if text:
            self._pending.text = text
            self.runs.append(self._pending)
            # Advance the pen so an unpositioned <tspan> that follows starts
            # where this run ended instead of back at the parent's x. Measure
            # the text AS DRAWN — raw, not stripped — or the trailing space
            # that separates the two runs is lost and they appear to collide.
            if self._text_stack:
                self._text_stack[-1]["pen"] = self._pending.x + text_width(
                    raw.lstrip(), self._pending.font_size, self._pending.weight)
        self._pending = None

    def _begin_run(self, x, y):
        self._flush()
        st = self._style[-1]
        ax, ay, tracked = self._pt(x, y)
        self._pending = TextRun(
            "", ax, ay, st["font_size"] * self._scale(), st["weight"], st["anchor"], tracked
        )

    # -- parser callbacks

    def handle_starttag(self, tag, attrs):
        self._start(tag, dict(attrs))
        if tag in SVG_EMPTY_TAGS:
            self._end(tag)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, dict(attrs))
        self._end(tag)

    def handle_endtag(self, tag):
        if tag in SVG_EMPTY_TAGS:
            return  # already closed at start
        self._end(tag)

    def _start(self, tag, attrs):
        self.tags_seen.append((tag, attrs))
        for k, v in attrs.items():
            self.raw_attrs.append((tag, k, v if v is not None else ""))

        if tag == "svg" and self.viewbox is None:
            vb = attrs.get("viewbox") or attrs.get("viewBox")
            if vb:
                try:
                    self.viewbox = [float(p) for p in re.split(r"[,\s]+", vb.strip())]
                except ValueError:
                    self.viewbox = None

        self._push(attrs)

        if tag == "text":
            st = self._style[-1]
            x = num(attrs, "x", 0.0)
            y = num(attrs, "y", 0.0)
            self._text_stack.append({"x": x, "y": y, "pen": x})
            self._begin_run(x, y)
        elif tag == "tspan" and self._text_stack:
            frame = self._text_stack[-1]
            st = self._style[-1]
            positioned = "x" in attrs or "dx" in attrs
            if "x" in attrs:
                frame["x"] = num(attrs, "x", frame["x"])
            if "y" in attrs:
                frame["y"] = num(attrs, "y", frame["y"])
            elif "dy" in attrs:
                frame["y"] = frame["y"] + length(attrs.get("dy"), st["font_size"])
            if "dx" in attrs:
                frame["x"] = frame["x"] + length(attrs.get("dx"), st["font_size"])
            if positioned:
                frame["pen"] = frame["x"]
                self._begin_run(frame["x"], frame["y"])
            else:
                # Continuation run: it starts where the previous one ended, not
                # at the parent's x. With a non-start anchor the whole <text> is
                # placed as a unit and the per-run split is not recoverable, so
                # the run is left untracked rather than measured wrongly — the
                # same rule the transform handling uses.
                self._flush()
                self._begin_run(frame.get("pen", frame["x"]), frame["y"])
                if self._pending is not None and self._style[-1]["anchor"] != "start":
                    self._pending.tracked = False
        else:
            self._shape(tag, attrs)

    def _end(self, tag):
        if tag == "text":
            self._flush()
            if self._text_stack:
                self._text_stack.pop()
        elif tag == "tspan":
            self._flush()
        self._pop()

    def handle_data(self, data):
        if self._pending is not None:
            self._pending.text += data

    # -- shape bounds

    def _shape(self, tag, attrs):
        pts = []
        if tag == "rect":
            x, y = num(attrs, "x", 0.0), num(attrs, "y", 0.0)
            w, h = num(attrs, "width"), num(attrs, "height")
            if None in (x, y, w, h):
                return
            pts = [(x, y), (x + w, y + h)]
        elif tag in ("circle", "ellipse"):
            cx, cy = num(attrs, "cx", 0.0), num(attrs, "cy", 0.0)
            rx = num(attrs, "r") if tag == "circle" else num(attrs, "rx")
            ry = num(attrs, "r") if tag == "circle" else num(attrs, "ry")
            if None in (cx, cy, rx, ry):
                return
            pts = [(cx - rx, cy - ry), (cx + rx, cy + ry)]
        elif tag == "line":
            x1, y1 = num(attrs, "x1"), num(attrs, "y1")
            x2, y2 = num(attrs, "x2"), num(attrs, "y2")
            if None in (x1, y1, x2, y2):
                return
            pts = [(x1, y1), (x2, y2)]
        elif tag in ("polygon", "polyline"):
            nums = [float(n) for n in re.findall(_NUM, attrs.get("points", ""))]
            pts = list(zip(nums[0::2], nums[1::2]))
        elif tag == "path":
            pts = absolute_path_points(attrs.get("d", ""))
        if not pts:
            return
        xs, ys, tracked = [], [], True
        for px, py in pts:
            ax, ay, tr = self._pt(px, py)
            xs.append(ax)
            ys.append(ay)
            tracked = tracked and tr
        self.shapes.append((tag, min(xs), min(ys), max(xs), max(ys), tracked))


_PATH_CMD_RE = re.compile(r"([MLHVCSQTAZmlhvcsqtaz])([^MLHVCSQTAZmlhvcsqtaz]*)")


def absolute_path_points(d):
    """Coordinate pairs from ABSOLUTE path commands only.

    Relative commands need a full pen-state machine to resolve; skipping them
    means a relative-only path is not bounds-checked, which is a miss rather
    than a false alarm. The rounded-corner card idiom the prompt teaches
    (`M 8 0 h 324 a 8 8 0 0 1 8 8 ...`) starts with an absolute M, so its
    origin is always checked.
    """
    pts = []
    for cmd, body in _PATH_CMD_RE.findall(d or ""):
        if cmd.islower() or cmd in "Zz":
            continue
        nums = [float(n) for n in re.findall(_NUM, body)]
        if cmd == "H":
            pts.extend((n, 0.0) for n in nums)
        elif cmd == "V":
            pts.extend((0.0, n) for n in nums)
        elif cmd == "A":
            # rx ry rot laf sf x y -- only the endpoint is a coordinate
            for i in range(0, len(nums) - 6, 7):
                pts.append((nums[i + 5], nums[i + 6]))
        else:
            pts.extend(zip(nums[0::2], nums[1::2]))
    return pts


# --------------------------------------------------------------------- checks

def check_canvas(p, findings):
    if not p.viewbox:
        findings.append(Finding("canvas-missing", "The <svg> has no viewBox.",
                                f'Use viewBox="0 0 {CANVAS_WIDTH} <height>".'))
        return None
    if len(p.viewbox) != 4:
        findings.append(Finding("canvas-malformed", f"viewBox has {len(p.viewbox)} values, expected 4."))
        return None
    x0, y0, w, h = p.viewbox
    if (x0, y0) != (0.0, 0.0) or w != CANVAS_WIDTH:
        findings.append(Finding(
            "canvas-width",
            f'viewBox is "{x0:g} {y0:g} {w:g} {h:g}", must be "0 0 {CANVAS_WIDTH} <height>".',
            "One canvas width for every infographic on every stack: font-size in "
            "user units only means a predictable rendered size if the scale is fixed.",
        ))
    if not (MIN_CANVAS_HEIGHT <= h <= MAX_CANVAS_HEIGHT):
        findings.append(Finding(
            "canvas-height",
            f"viewBox height {h:g} is outside {MIN_CANVAS_HEIGHT}-{MAX_CANVAS_HEIGHT}.",
        ))
    return (x0, y0, w, h)


def check_text(p, box, findings):
    if not box:
        return
    x0, y0, w, h = box
    for run in p.runs:
        if run.font_size < MIN_FONT_SIZE:
            findings.append(Finding(
                "font-too-small",
                f'font-size {run.font_size:g} on "{clip(run.text)}" is below {MIN_FONT_SIZE}.',
                f"On an {CANVAS_WIDTH}-wide canvas that renders under 14px.",
            ))
        if not run.tracked:
            continue
        width = text_width(run.text, run.font_size, run.weight)
        anchor = run.anchor
        left = run.x if anchor == "start" else (run.x - width / 2 if anchor == "middle" else run.x - width)
        right = left + width
        if left < x0 - TOL or right > x0 + w + TOL:
            findings.append(Finding(
                "text-overflow-x",
                f'"{clip(run.text)}" spans x={left:.0f}..{right:.0f}, outside 0..{w:g}.',
                f"{len(run.text)} chars at font-size {run.font_size:g} measures "
                f"{width:.0f} units. Shorten it, or split across "
                f'<tspan x="{left:.0f}" dy="1.2em"> lines.',
            ))
        top = run.y - 0.72 * run.font_size
        bottom = run.y + 0.21 * run.font_size
        if top < y0 - 1 or bottom > y0 + h + 1:
            findings.append(Finding(
                "text-overflow-y",
                f'"{clip(run.text)}" sits at y={run.y:g}, outside 0..{h:g}.',
                "Grow the viewBox height, or move the row up.",
            ))


def check_text_in_box(p, findings):
    """Text must fit the card, pill or cell it sits on - not merely the canvas.

    This is the check that catches what readers actually see. On
    automatizacion-industrial-pyme-galicia-ayudas-2026 a pill reading
    "COMPRAR E INSTALAR" measures 146 units centred at x=104, so it spans
    31..177 inside a pill drawn from 40 to 168. The overhang is white text on
    the white page, so it renders as "OMPRAR E INSTALA" - and it never crosses
    the viewBox, so a canvas-only check reports the graphic as clean.
    """
    rects = [s for s in p.shapes if s[0] == "rect" and s[5]]
    if not rects:
        return
    for run in p.runs:
        if not run.tracked or not run.text:
            continue
        width = text_width(run.text, run.font_size, run.weight)
        anchor = run.anchor
        left = run.x if anchor == "start" else (run.x - width / 2 if anchor == "middle" else run.x - width)
        right = left + width

        # The container is the smallest rect the baseline sits inside. A rect
        # shorter than the type is a rule or a divider, not a box.
        container, area = None, None
        for _tag, rx0, ry0, rx1, ry1, _tracked in rects:
            if ry1 - ry0 < run.font_size:
                continue
            if rx0 <= run.x <= rx1 and ry0 <= run.y <= ry1:
                size = (rx1 - rx0) * (ry1 - ry0)
                if area is None or size < area:
                    container, area = (rx0, ry0, rx1, ry1), size
        if container is None:
            continue
        rx0, _ry0, rx1, _ry1 = container
        over_left = rx0 - left
        over_right = right - rx1
        if over_left > 1 or over_right > 1:
            spill = max(over_left, over_right)
            findings.append(Finding(
                "text-overflow-box",
                f'"{clip(run.text)}" is {spill:.0f} units wider than the '
                f"{rx1 - rx0:.0f}-unit box it sits in.",
                f"Text spans {left:.0f}..{right:.0f}, box is {rx0:.0f}..{rx1:.0f}. "
                f"Widen the box, shorten the label, or split it across "
                f"<tspan> lines. If the fill matches the page the overhang is "
                f"invisible and the label just looks truncated.",
            ))


# Inter's cap height is ~0.727em and its descender ~0.21em. A box of
# ascent 0.72 / descent 0.20 around the baseline is deliberately a little
# tighter than the full em square: two lines set 16 units apart at font-size
# 15 are normal typography, not a collision, and a full-em box would report
# every one of them.
ASCENT, DESCENT = 0.72, 0.20

# A real collision overlaps vertically by a meaningful fraction of the type.
# Measured against the case this check exists for: "275%" at font-size 52 sat
# 13.8 units into a 15-unit line beside it. Tight leading produces ~2.
VERTICAL_COLLISION_RATIO = 0.25
HORIZONTAL_TOL = 2.0


def run_box(run):
    """Baseline-relative bounding box of one text run, in canvas units."""
    width = text_width(run.text, run.font_size, run.weight)
    if run.anchor == "middle":
        left = run.x - width / 2
    elif run.anchor == "end":
        left = run.x - width
    else:
        left = run.x
    return (left, run.y - ASCENT * run.font_size,
            left + width, run.y + DESCENT * run.font_size)


def check_text_collisions(p, findings):
    """Two labels must not sit on top of each other.

    check_text_in_box measures text against RECTS, so a label colliding with
    another label is invisible to it: both can be inside their boxes, or
    inside no box at all, and the graphic still renders as one word printed
    over another. That shipped - a "275%" set at font-size 52 overlapped the
    15-unit line beside it by 13.8 units and the validator returned exit 0.

    Only overlaps in BOTH axes count, and the vertical one has to be worth
    reporting: adjacent lines of a label and its sub-label routinely share a
    descender with an ascender, and flagging those would train everyone to
    ignore this check.
    """
    runs = [r for r in p.runs if r.tracked and r.text and r.text.strip()]
    boxes = [(r, run_box(r)) for r in runs]
    for i, (ra, (ax0, ay0, ax1, ay1)) in enumerate(boxes):
        for rb, (bx0, by0, bx1, by1) in boxes[i + 1:]:
            ox = min(ax1, bx1) - max(ax0, bx0)
            oy = min(ay1, by1) - max(ay0, by0)
            if ox <= HORIZONTAL_TOL or oy <= 0:
                continue
            floor = VERTICAL_COLLISION_RATIO * min(ra.font_size, rb.font_size)
            if oy <= floor:
                continue
            findings.append(Finding(
                "text-collision",
                f'"{clip(ra.text)}" and "{clip(rb.text)}" overlap by '
                f"{ox:.0f}x{oy:.0f} units.",
                f"One label is printed over the other. Move one of them, or "
                f"shorten it: at font-size {ra.font_size:g} and {rb.font_size:g} "
                f"they span x {ax0:.0f}..{ax1:.0f} and {bx0:.0f}..{bx1:.0f}.",
            ))


# A wrapped pair: two runs at the same x, same size and weight, one line apart.
WRAP_MIN, WRAP_MAX = 0.9, 1.8      # multiples of font-size between baselines
CLAUSE_END = ",.:;!?\u2014\u2026"  # a break AFTER one of these is a clause boundary
# A clause boundary is only a BETTER break than the one chosen if it sits at a
# sensible line length. Breaking at a comma that lands a third of the way across
# leaves a worse rag than running on to the next natural pause.
GOOD_BREAK_MIN, GOOD_BREAK_MAX = 0.60, 1.00


def check_unneeded_breaks(p, box, findings):
    """A line must not break mid-clause when a clause boundary would have fitted.

    Hand-splitting text across two <text> elements puts the break wherever the
    author stopped typing rather than at a pause a reader can feel. This shipped
    on a client site: "Primero mira lo que ya tienes. Despues separa lo basico /
    de lo prescindible. El precio es lo ultimo." broke after "basico", in the
    middle of a clause, when "...de lo prescindible." ends a sentence at 98% of
    the width and was available the whole time.

    Two things deliberately stay quiet. A break AFTER a comma or a full stop is
    already a clause boundary and is correct. And a long line with no internal
    pause at all — a two-line headline like "Comprar cinco veces al mes / te
    cuesta mas por exactamente lo mismo." — has no better break to offer, so
    flagging it would just be noise.
    """
    if not box:
        return
    _x, _y, canvas_w, _h = box
    runs = [r for r in p.runs if r.tracked and r.text and r.text.strip()]
    for a, b in zip(runs, runs[1:]):
        if a.anchor != "start" or b.anchor != "start":
            continue
        if abs(a.x - b.x) > 1 or abs(a.font_size - b.font_size) > 0.5 or a.weight != b.weight:
            continue
        gap = b.y - a.y
        if not (WRAP_MIN * a.font_size <= gap <= WRAP_MAX * a.font_size):
            continue
        if a.text.rstrip()[-1:] in CLAUSE_END:
            continue                       # already broken at a pause

        joined = f"{a.text.rstrip()} {b.text.strip()}"
        available = canvas_w - a.x * 2     # the layouts use a symmetric margin
        chosen = text_width(a.text, a.font_size, a.weight)

        best = None
        for m in re.finditer(r"[" + re.escape(CLAUSE_END) + r"](?=\s)", joined):
            prefix = joined[: m.end()]
            width = text_width(prefix, a.font_size, a.weight)
            ratio = width / available if available else 0
            if GOOD_BREAK_MIN <= ratio <= GOOD_BREAK_MAX and width > chosen:
                best = (prefix, ratio)
        if best:
            prefix, ratio = best
            findings.append(Finding(
                "break-not-at-clause",
                f'"{clip(a.text)}" breaks mid-clause at {chosen / available:.0%} of the '
                f"width when a pause was available at {ratio:.0%}.",
                f'Break after "...{clip(prefix[-40:])}" instead. A reader feels a break '
                f"at a comma or a full stop; one in the middle of a phrase just looks "
                f"like the line gave up.",
            ))


def check_shapes(p, box, findings):
    if not box:
        return
    x0, y0, w, h = box
    for tag, sx0, sy0, sx1, sy1, tracked in p.shapes:
        if not tracked:
            continue
        if sx0 < x0 - TOL or sy0 < y0 - TOL or sx1 > x0 + w + TOL or sy1 > y0 + h + TOL:
            findings.append(Finding(
                "shape-overflow",
                f"<{tag}> spans ({sx0:.0f},{sy0:.0f})-({sx1:.0f},{sy1:.0f}), "
                f"outside 0 0 {w:g} {h:g}.",
            ))


def check_forbidden(p, findings):
    for tag, _attrs in p.tags_seen:
        if tag in FORBIDDEN_TAGS:
            findings.append(Finding(
                "forbidden-element",
                f"<{tag}> is not allowed inside an infographic.",
                "The bl-site-package sanitizer strips it, and on biglobster a "
                "<style> block carrying its own prefers-color-scheme rule fights "
                "the site's data-theme switch. Inherit var(--token) and "
                "currentColor; draw arrowheads as <polygon>.",
            ))


def check_geometry_tokens(p, findings):
    for tag, attr, value in p.raw_attrs:
        if attr in GEOMETRY_ATTRS and "var(" in value:
            findings.append(Finding(
                "var-in-geometry",
                f'<{tag} {attr}="{value}"> - var() never resolves in a geometry attribute.',
                "It parses as an SVG data type, not a CSS property, so the browser "
                "silently uses the default (rx=0 -> square corners). Write the "
                "number: --radius-md is 8, --radius-sm is 4.",
            ))
        if attr in SINGLE_VALUE_ATTRS and value.strip() and len(re.findall(_NUM, value)) > 1:
            findings.append(Finding(
                "multivalue-geometry",
                f'<{tag} {attr}="{value}"> takes exactly one number.',
                "rx is not the CSS border-radius shorthand: a list is discarded "
                "whole and drawn as 0. A single rx rounds all four corners - to "
                "round only two, draw a <path> with arcs.",
            ))


def check_design_tokens(p, stack, findings, source=""):
    known = STACK_TOKENS[stack]
    seen = set()
    # Scan the raw source, not just attributes: a legacy infographic can declare
    # its palette inside an SVG <style> block, where an attribute walk never
    # sees it. lib/infographic-guard.mjs reads the markup and did catch those,
    # so an attribute-only scan here would put the two tools out of step and
    # leave a build failure missing from the baseline.
    haystack = [v for _t, _a, v in p.raw_attrs] + [source]
    for value in haystack:
        for token in re.findall(r"var\(\s*--([a-zA-Z0-9-]+)", value):
            if token not in known and token not in seen:
                seen.add(token)
                findings.append(Finding(
                    "unknown-token",
                    f"--{token} is not defined by the {stack} stylesheet.",
                    "It resolves to nothing and the attribute falls back to its "
                    "initial value (black fill, no stroke) with no error. Known: "
                    + ", ".join("--" + t for t in sorted(known)),
                ))


def is_emoji(ch):
    if ch in SYMBOL_ALLOWLIST:
        return False
    cp = ord(ch)
    if cp == 0xFE0F:  # variation selector-16 forces emoji presentation
        return True
    return (
        0x1F000 <= cp <= 0x1FAFF
        or 0x2600 <= cp <= 0x27BF
        or 0x231A <= cp <= 0x231B
        or 0x23E0 <= cp <= 0x23FF
        or 0x2B00 <= cp <= 0x2BFF
    )


def check_emoji(p, findings):
    for run in p.runs:
        bad = sorted({ch for ch in run.text if is_emoji(ch)})
        if bad:
            findings.append(Finding(
                "emoji-icon",
                f'"{clip(run.text)}" uses {" ".join(bad)} as an icon.',
                "Emoji render as tofu boxes in SVG text and do not follow the "
                "theme. Draw the icon as vector shapes, or drop it.",
            ))


WORD_RE = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ'-]{1,}")


def article_vocabulary(source):
    """Every word the ARTICLE already uses, lowercased.

    The validator checks text the agent just wrote, and that text is drawn from
    the article it illustrates. A word the article already uses is therefore the
    author's vocabulary, not a typo the agent introduced — so hunspell flagging
    it is a false positive, every time.

    This is not hypothetical. On the first successful run the check flagged
    `CRA`, `ENISA`, `pyme`, `dic` and `sep`, all of which appear in the article,
    and the agent rewrote its labels to get past them: it wrote "el Reglamento
    europeo" where a human would write "el CRA", and spelled out every date. It
    even filed the workaround in its skill. A validator that changes what the
    content SAYS has overstepped — it is there to catch a slip, not to enforce a
    dictionary on domain language.
    """
    return {w.lower() for w in WORD_RE.findall(source or "")}


def check_spelling(p, findings, lang="es_ES", vocabulary=None):
    """Spanish spellcheck via hunspell. Absent hunspell warns, it does not fail."""
    if not shutil.which("hunspell"):
        print("warning: hunspell not installed, skipping the spelling check",
              file=sys.stderr)
        return
    vocabulary = vocabulary or set()
    words = {}
    for run in p.runs:
        for word in re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ'-]{2,}", run.text):
            words.setdefault(word, run.text)
    if not words:
        return
    try:
        proc = subprocess.run(
            ["hunspell", "-d", lang, "-l"],
            input="\n".join(words) + "\n",
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"warning: hunspell failed ({exc}), skipping the spelling check", file=sys.stderr)
        return
    for word in dict.fromkeys(w for w in proc.stdout.split() if w):
        if word.lower() in vocabulary:
            continue  # the article already uses it; not a slip the agent made
        findings.append(Finding(
            "spelling",
            f'"{word}" is not a Spanish word.',
            f'In the label "{clip(words.get(word, ""))}". If it is a brand or a '
            f"technical term, that is fine - confirm it is spelled right.",
        ))


def clip(text, n=52):
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ----------------------------------------------------------------------- input

FIGURE_RE = re.compile(
    r"<figure[^>]*class=\"[^\"]*article-infographic[^\"]*\"[^>]*>(.*?)</figure>",
    re.S | re.I,
)
SVG_RE = re.compile(r"<svg\b.*?</svg>", re.S | re.I)


def extract_svgs(source):
    """Pull infographic SVGs out of raw SVG, a <figure>, or a whole article."""
    figures = FIGURE_RE.findall(source)
    haystack = "\n".join(figures) if figures else source
    return SVG_RE.findall(haystack)


def check_poster(source, box, findings):
    """A poster figure is TWO layers that must agree, or the labels drift.

    The <svg> is absolutely positioned over the <img> and takes its height from
    its own viewBox, so if the image's aspect ratio differs the two coordinate
    systems separate and every label lands away from the artwork it belongs to —
    a little at the top, badly at the bottom, and nothing looks broken.
    """
    if "article-infographic--poster" not in source:
        if re.search(r"<img\b", source, re.I):
            findings.append(Finding(
                "poster-class-missing",
                "The figure has an <img> but not the article-infographic--poster class.",
                "Without it neither stack overlays the two layers: they stack "
                "vertically instead, artwork above and a floating data layer below."))
        return
    img = re.search(r"<img\b[^>]*>", source, re.I)
    if not img:
        findings.append(Finding(
            "poster-image-missing",
            "article-infographic--poster is set but the figure has no <img>.",
            "The class makes the <svg> position:absolute over an image that is "
            "not there, so the graphic collapses to zero height."))
        return
    tag = img.group(0)
    if not re.search(r'\balt\s*=\s*"\s*"', tag):
        findings.append(Finding(
            "poster-alt-not-empty",
            "The poster's <img> needs alt=\"\".",
            "The artwork is decorative; the <svg>'s <title> and <desc> carry the "
            "accessible description. A populated alt reads it out twice."))
    w = re.search(r'\bwidth\s*=\s*"(\d+)"', tag)
    h = re.search(r'\bheight\s*=\s*"(\d+)"', tag)
    if not (w and h):
        findings.append(Finding(
            "poster-image-unsized",
            "The poster's <img> needs explicit width and height.",
            "They pin the intrinsic ratio so the layers line up before the image "
            "has loaded, and they are what this check compares against."))
        return
    if not box:
        return
    iw, ih = int(w.group(1)), int(h.group(1))
    _x, _y, vw, vh = box
    if (iw, ih) != (int(vw), int(vh)):
        findings.append(Finding(
            "poster-aspect-mismatch",
            f'<img> is {iw}x{ih} but the viewBox is {vw:g}x{vh:g}.',
            "The two layers must share one coordinate system. Set the image "
            f'attributes to width="{vw:g}" height="{vh:g}" and composite the '
            "artwork at that ratio."))


def validate(svg, stack, skip_spelling=False, vocabulary=None):
    parser = SvgParser()
    parser.css = parse_css_classes(
        " ".join(re.findall(r"<style[^>]*>(.*?)</style>", svg, re.S | re.I)))
    try:
        parser.feed(svg)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - malformed markup is a finding, not a crash
        return [Finding("parse-error", f"Could not parse the SVG: {exc}")]
    findings = []
    box = check_canvas(parser, findings)
    check_text(parser, box, findings)
    check_text_in_box(parser, findings)
    check_text_collisions(parser, findings)
    check_unneeded_breaks(parser, box, findings)
    check_shapes(parser, box, findings)
    check_poster(svg, box, findings)
    check_forbidden(parser, findings)
    check_geometry_tokens(parser, findings)
    check_design_tokens(parser, stack, findings, svg)
    check_emoji(parser, findings)
    if not skip_spelling:
        check_spelling(parser, findings, vocabulary=vocabulary)
    return findings


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Validate an infographic SVG before publishing it.")
    ap.add_argument("--stack", required=True, choices=sorted(STACK_TOKENS),
                    help="which site's design tokens and sanitizer apply")
    ap.add_argument("--file", help="read from this file instead of stdin "
                                   "(may be a whole article; figures are extracted)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--no-spelling", action="store_true",
                    help="skip the hunspell pass")
    args = ap.parse_args(argv)

    try:
        source = open(args.file, encoding="utf-8").read() if args.file else sys.stdin.read()
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    svgs = extract_svgs(source)
    if not svgs:
        print("error: no <svg> found in the input", file=sys.stderr)
        return 3

    # Built from the WHOLE input, not just the figure: when --file is an
    # article, its prose is the author's vocabulary and nothing drawn from
    # it is a new misspelling.
    vocab = article_vocabulary(source)
    results = [validate(svg, args.stack, args.no_spelling, vocab) for svg in svgs]
    total = sum(len(r) for r in results)

    if args.json:
        print(json.dumps({
            "ok": total == 0,
            "stack": args.stack,
            "svg_count": len(svgs),
            "findings": [[f.as_dict() for f in r] for r in results],
        }, ensure_ascii=False, indent=1))
        return 1 if total else 0

    where = args.file or "<stdin>"
    if total == 0:
        print(f"OK  {where}: {len(svgs)} infographic(s), no findings.")
        return 0
    print(f"FAIL  {where}: {total} finding(s) across {len(svgs)} infographic(s).\n")
    for i, found in enumerate(results):
        if not found:
            continue
        if len(results) > 1:
            print(f"  --- infographic {i + 1} ---")
        for finding in found:
            print(finding)
    print("\nFix every finding and run this again before publishing.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
