"""Voiceover synthesis with word timings, and the caption data built from them.

Edge TTS streams a ``WordBoundary`` event per word alongside the audio, so
karaoke highlighting is exact without a separate alignment model. (The
checklist read word timings back out of ``--write-subtitles`` .vtt files;
edge-tts 7.x writes sentence-level cues there by default, so that path
silently degraded to one highlight per sentence.)

Beats with no TTS timing — avatar clips, or the offline ``fake`` voice used
in tests — get their words spread evenly across the speech, which is close
enough for a line of a few seconds.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Dict, List

from plugins.shorts.studio import media

TICKS_PER_MS = 10_000  # Edge reports offsets in 100 ns ticks

Word = Dict[str, float | str]  # {"text", "start", "end"} in seconds, beat-relative


class VoiceError(RuntimeError):
    pass


async def _edge_stream(text: str, voice: str, rate: str, mp3: Path) -> List[Word]:
    import edge_tts

    words: List[Word] = []
    communicate = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    with open(mp3, "wb") as handle:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                handle.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / TICKS_PER_MS / 1000
                words.append({
                    "text": chunk["text"],
                    "start": start,
                    "end": start + chunk["duration"] / TICKS_PER_MS / 1000,
                })
    return words


def synthesize(text: str, voice: str, rate: str, mp3: Path, *, attempts: int = 3) -> List[Word]:
    """Speak ``text`` into ``mp3``; return its words with beat-relative times."""
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            words = asyncio.run(_edge_stream(text, voice, rate, mp3))
            if mp3.exists() and mp3.stat().st_size > 0:
                return words or spread_words(text, media.duration(mp3))
            last = VoiceError("Edge TTS returned no audio")
        except Exception as exc:  # network hiccups are the common case
            last = exc
        time.sleep(2 * attempt)
    raise VoiceError(f"Edge TTS failed after {attempts} attempts for {text[:40]!r}: {last}")


def fake_synthesize(text: str, mp3: Path) -> List[Word]:
    """Offline stand-in: a quiet tone lasting ~0.36s a word. Tests and dry runs only."""
    seconds = max(1.0, 0.36 * len(text.split()))
    media.ffmpeg("-f", "lavfi", "-i", f"sine=frequency=220:sample_rate=48000:duration={seconds:.2f}",
                 "-af", "volume=0.2", str(mp3), what="fake voice")
    return spread_words(text, seconds)


def spread_words(text: str, seconds: float, *, lead: float = 0.05, tail: float = 0.15) -> List[Word]:
    """Even word timings across ``seconds`` (weighted by word length)."""
    tokens = [t for t in re.split(r"\s+", text.strip()) if t]
    if not tokens:
        return []
    span = max(0.1, seconds - lead - tail)
    weights = [max(2, len(t)) for t in tokens]
    total = float(sum(weights))
    words: List[Word] = []
    cursor = lead
    for token, weight in zip(tokens, weights):
        length = span * weight / total
        words.append({"text": token, "start": cursor, "end": cursor + length * 0.92})
        cursor += length
    return words


_STRIP = re.compile(r"^[\"'“”‘’(¿¡]+|[\"'“”‘’),.;:!?…]+$")


def clean_word(text: str) -> str:
    """Words shown in karaoke lose edge punctuation; inner hyphens stay."""
    return _STRIP.sub("", text.strip())


def attach_raw(words: List[Word], vo_text: str) -> List[Word]:
    """Give each timed word its script token (with punctuation) as ``raw``.

    Edge reports bare words; the SRT should read like the script. Matching is
    sequential with a small look-ahead, so a word the engine split or merged
    ("seventy-three") just keeps its own text.
    """
    tokens = [t for t in re.split(r"\s+", vo_text.strip()) if t]
    j = 0
    for w in words:
        target = clean_word(str(w["text"])).lower()
        w["raw"] = str(w["text"])
        for k in range(j, min(j + 3, len(tokens))):
            if clean_word(tokens[k]).lower() == target:
                w["raw"] = tokens[k]
                j = k + 1
                break
    return words


def paginate(words: List[Dict], *, max_words: int = 4, max_chars: int = 20,
             pause_ms: int = 380) -> List[Dict]:
    """Assign a karaoke ``page`` to each word (absolute-ms words, in order).

    A page breaks on a beat change, a pause, or when it is full, so a caption
    never straddles two scenes or runs ahead of the voice.
    """
    page = -1
    count = chars = 0
    prev = None
    for w in words:
        new_page = (
            prev is None
            or w["beat"] != prev["beat"]
            or w["startMs"] - prev["endMs"] > pause_ms
            or count >= max_words
            or chars + len(w["text"]) + 1 > max_chars
        )
        if new_page:
            page += 1
            count = chars = 0
        w["page"] = page
        count += 1
        chars += len(w["text"]) + 1
        prev = w
    return words


def _srt_time(ms: float) -> str:
    ms = max(0, int(round(ms)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(words: List[Dict], *, max_words: int = 7, max_chars: int = 42) -> str:
    """Sidecar captions for YouTube: short lines, broken at sentences, pauses and beats."""
    lines: List[List[Dict]] = []
    for w in words:
        cur = lines[-1] if lines else None
        if (
            cur is None
            or w["beat"] != cur[-1]["beat"]
            or str(cur[-1].get("raw", "")).endswith((".", "?", "!", "…"))
            or w["startMs"] - cur[-1]["endMs"] > 500
            or len(cur) >= max_words
            or sum(len(x.get("raw", x["text"])) + 1 for x in cur) + len(w.get("raw", w["text"])) > max_chars
        ):
            lines.append([w])
        else:
            cur.append(w)
    out = []
    for i, line in enumerate(lines, 1):
        text = " ".join(str(x.get("raw") or x["text"]) for x in line)
        end = line[-1]["endMs"] + 250
        if i < len(lines):
            end = min(end, lines[i][0]["startMs"] - 20)
        out.append(f"{i}\n{_srt_time(line[0]['startMs'])} --> {_srt_time(max(end, line[-1]['endMs']))}\n{text}\n")
    return "\n".join(out)
