"""ASS subtitle generation for burned-in vertical captions.

Captions are the single biggest retention lever on Reels and TikTok — most
viewers watch muted — so they are burned into the pixels rather than shipped
as a sidecar track the platform may ignore.

Layout rules encoded here:

* Canvas is the render canvas, 1080x1920, so coordinates need no scaling.
* Text stays inside the 9:16 safe area. The top ~14% and bottom ~20% of a
  vertical video are covered by the platform's own UI (caption text, buttons,
  progress bar), so the caption block sits well above that floor.
* At most two lines on screen at once. Longer scene text is split into
  successive cues whose durations are proportional to their character count,
  which keeps the words roughly in step with the voiceover without needing
  word-level timestamps from the TTS engine.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Sequence, Tuple

# DejaVu Sans ships with the container image (fonts-dejavu-core, pulled in as
# an ffmpeg dependency). libass falls back if it is ever missing, but the
# layout constants below are tuned for it.
DEFAULT_FONT = "DejaVu Sans"
FONT_SIZE = 84
PLAY_RES_X = 1080
PLAY_RES_Y = 1920

# Horizontal margins, and the vertical lift that keeps the block clear of the
# platform UI at the bottom of the frame.
MARGIN_H = 80
MARGIN_V = 520

# ~20 characters is what fits across 920px of usable width at 84px bold.
MAX_CHARS_PER_LINE = 20
MAX_LINES_PER_CUE = 2

_ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {res_x}
PlayResY: {res_y}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,3,2,{margin_h},{margin_h},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def format_time(seconds: float) -> str:
    """Format seconds as the ASS ``H:MM:SS.cc`` timestamp form."""
    if seconds < 0:
        seconds = 0.0
    centis = int(round(seconds * 100))
    hours, rem = divmod(centis, 360000)
    minutes, rem = divmod(rem, 6000)
    secs, cs = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def escape_text(text: str) -> str:
    """Neutralise ASS markup so caption text renders literally."""
    # Braces open override blocks; a stray backslash starts an escape.
    return (
        text.replace("\\", "/")
        .replace("{", "(")
        .replace("}", ")")
        .strip()
    )


def wrap_lines(text: str, max_chars: int = MAX_CHARS_PER_LINE) -> List[str]:
    """Greedy word wrap. Words longer than ``max_chars`` get their own line."""
    words = [w for w in re.split(r"\s+", text.strip()) if w]
    lines: List[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def split_into_cues(text: str) -> List[str]:
    """Split caption text into cues of at most two wrapped lines each."""
    lines = wrap_lines(text)
    cues: List[str] = []
    for index in range(0, len(lines), MAX_LINES_PER_CUE):
        cues.append("\\N".join(lines[index : index + MAX_LINES_PER_CUE]))
    return cues


def _cue_weights(cues: Sequence[str]) -> List[float]:
    """Relative time share per cue, by visible character count."""
    lengths = [max(len(cue.replace("\\N", " ")), 1) for cue in cues]
    total = float(sum(lengths))
    return [length / total for length in lengths]


def build_ass(
    timed_captions: Iterable[Tuple[float, float, str]],
    *,
    font: str = DEFAULT_FONT,
) -> str:
    """Render an ASS subtitle file.

    ``timed_captions`` yields ``(start_seconds, duration_seconds, text)`` —
    one entry per scene. Each scene's text is split into at most-two-line
    cues that share the scene's duration proportionally.
    """
    body: List[str] = []
    for start, duration, text in timed_captions:
        clean = escape_text(text or "")
        if not clean or duration <= 0:
            continue
        cues = split_into_cues(clean)
        if not cues:
            continue
        cursor = start
        for cue, weight in zip(cues, _cue_weights(cues)):
            end = cursor + duration * weight
            body.append(
                f"Dialogue: 0,{format_time(cursor)},{format_time(end)},"
                f"Caption,,0,0,0,,{cue}"
            )
            cursor = end

    header = _ASS_HEADER.format(
        res_x=PLAY_RES_X,
        res_y=PLAY_RES_Y,
        font=font,
        size=FONT_SIZE,
        margin_h=MARGIN_H,
        margin_v=MARGIN_V,
    )
    return header + "\n".join(body) + "\n"
