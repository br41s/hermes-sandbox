"""Voiceover synthesis with word timings, and the caption data built from them.

Kokoro first, since 2026-10-14: the owner compared VideoDesk's shorts (Buzz,
Kokoro at its own pace, one voice) with the Studio's and chose that sound.
Kokoro-82M (Apache-2.0, ``kokoro-onnx``) runs on the render farm's CPU for
free, one fixed voice per presenter, no speaking rate and no tempo shift.
Its word timings come from faster-whisper read back over the audio and
aligned to the script, the way VideoDesk's ``subtitular --guion`` does it,
so the karaoke shows the script's words at the heard times.

Azure Speech (official REST) stays as the fallback engine, and Edge TTS
behind it. Edge streams a ``WordBoundary`` event per word alongside the
audio, so its karaoke is exact without an alignment model; the Azure REST
call returns no word timings, so its words are spread evenly. Microsoft
changed the Edge endpoint's anti-abuse token on 2026-10-12 and edge-tts
7.2.8 got a 403 on nearly every call, which is why Azure came first then.

``SHORTS_TTS_ENGINE`` picks the engine: ``kokoro`` (the default) or
``azure``. The push smoke render builds every sample with both, so the
owner can hear them side by side.

Beats with no TTS timing — avatar clips, Azure, or the offline ``fake``
voice used in tests — get their words spread evenly across the speech,
which is close enough for a line of a few seconds.
"""

from __future__ import annotations

import asyncio
import difflib
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

from plugins.shorts.studio import media

TICKS_PER_MS = 10_000  # Edge reports offsets in 100 ns ticks

Word = Dict[str, float | str]  # {"text", "start", "end"} in seconds, beat-relative


class VoiceError(RuntimeError):
    pass


ENGINES = ("kokoro", "azure")

# Kokoro voices. Martín, the narrator of every card, per language; the owner
# picks between the Spanish male voices (em_alex, em_santa) after hearing the
# smoke renders, with SHORTS_KOKORO_VOICE_ES / _EN. Lucía's Kokoro voice is
# only for a line whose Avatar IV take was not made: she normally speaks
# with her HeyGen voice inside the take.
KOKORO_VOICES = {"es": "em_alex", "en": "am_michael"}
KOKORO_HOST_VOICES = {"es": "ef_dora", "en": "af_heart"}
KOKORO_LANG = {"es": "es", "en": "en-us"}
KOKORO_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
KOKORO_MODEL, KOKORO_VOICE_PACK = "kokoro-v1.0.int8.onnx", "voices-v1.0.bin"
WHISPER_MODEL = "small"          # faster-whisper on CPU, int8: the size VideoDesk transcribes with
_KOKORO_ID = re.compile(r"^[a-z]{2}_[a-z]+$")
# The neural voice of the same kind, for a Kokoro id that has to fall back to Azure or Edge.
NEURAL_TWINS = {"ef": "es-ES-ElviraNeural", "em": "es-ES-AlvaroNeural",
                "af": "en-US-AvaMultilingualNeural", "am": "en-US-AndrewMultilingualNeural"}

AZURE_TTS = "https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
# Azure HD voices ("…:DragonHDLatestNeural") do not exist on Edge; when Azure
# fails, Edge reads the line with the standard voice of the same language.
EDGE_FALLBACK = {"es-ES": "es-ES-AlvaroNeural", "en-US": "en-US-AndrewMultilingualNeural"}
# An HD voice takes no prosody rate, so its line is sped up after synthesis:
# Martín read slowly to the owner at 1.0 (2026-10-13). Kokoro is not sped up.
HD_TEMPO = 1.12


def engine() -> str:
    name = (os.environ.get("SHORTS_TTS_ENGINE") or "kokoro").strip().lower()
    return name if name in ENGINES else "kokoro"


def model_dir() -> Path:
    """Where the free engine's models live; the workflow caches it between runs."""
    return Path(os.environ.get("SHORTS_MODEL_DIR") or Path.home() / ".cache" / "shorts-models")


def lang2(voice: str) -> str:
    """'es' or 'en' from a neural voice name ("es-ES-…") or a Kokoro id ("em_alex")."""
    if _KOKORO_ID.match(voice):
        return "es" if voice[0] == "e" else "en"
    return (voice.split("-")[0][:2] or "en").lower()


def kokoro_voice(voice: str) -> str:
    """The Kokoro voice for a package voice: an id passes through; a neural name maps by language."""
    if _KOKORO_ID.match(voice):
        return voice
    lang = lang2(voice)
    override = (os.environ.get(f"SHORTS_KOKORO_VOICE_{lang.upper()}") or "").strip()
    return override if _KOKORO_ID.match(override) else KOKORO_VOICES.get(lang, KOKORO_VOICES["en"])


def host_voice(lang: str, neural: Dict[str, str]) -> str:
    """Lucía's free voice for a line without a take: Kokoro's, or the neural one under Azure."""
    if engine() == "kokoro":
        return KOKORO_HOST_VOICES.get(lang, KOKORO_HOST_VOICES["en"])
    return neural.get(lang, neural["en"])


def _lang(voice: str) -> str:
    return "-".join(voice.split("-")[:2])


# ---------------------------------------------------------------------------
# Kokoro (free, on the runner) + faster-whisper timings aligned to the script
# ---------------------------------------------------------------------------

def _download(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    print(f"[studio] voice: downloading {dest.name} (once; cached)", flush=True)
    with httpx.stream("GET", url, follow_redirects=True, timeout=600.0) as resp:
        resp.raise_for_status()
        with open(part, "wb") as handle:
            for chunk in resp.iter_bytes(1 << 20):
                handle.write(chunk)
    part.replace(dest)
    return dest


_KOKORO = None


def _kokoro_model():
    global _KOKORO
    if _KOKORO is None:
        from kokoro_onnx import Kokoro

        d = model_dir() / "kokoro"
        _KOKORO = Kokoro(str(_download(KOKORO_URL + KOKORO_MODEL, d / KOKORO_MODEL)),
                         str(_download(KOKORO_URL + KOKORO_VOICE_PACK, d / KOKORO_VOICE_PACK)))
    return _KOKORO


def _kokoro(text: str, voice: str, lang: str, wav: Path) -> float:
    """Speak ``text`` with Kokoro into ``wav`` (mono, 16-bit); return its seconds."""
    import wave

    import numpy as np

    samples, rate = _kokoro_model().create(text, voice=voice, speed=1.0, lang=KOKORO_LANG.get(lang, "en-us"))
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(rate))
        handle.writeframes(pcm.tobytes())
    return len(pcm) / float(rate)


def heard_words(wav: Path, lang: str) -> List[Word]:
    """What faster-whisper hears in ``wav``, word by word, with times."""
    from faster_whisper import WhisperModel

    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8",
                         download_root=str(model_dir() / "whisper"))
    segments, _info = model.transcribe(str(wav), language=lang, word_timestamps=True,
                                       beam_size=5, vad_filter=False)
    words: List[Word] = []
    for segment in segments:
        for w in segment.words or []:
            if w.word.strip():
                words.append({"text": w.word.strip(), "start": float(w.start), "end": float(w.end)})
    return words


def _norm(token: str) -> str:
    return re.sub(r"[\W_]+", "", token.lower())


def _even(start: float, end: float, tokens: List[str]) -> List[Tuple[float, float]]:
    weights = [max(2, len(_norm(t))) for t in tokens]
    total = float(sum(weights)) or 1.0
    span = max(0.1, end - start)
    out: List[Tuple[float, float]] = []
    cursor = start
    for weight in weights:
        length = span * weight / total
        out.append((cursor, cursor + length * 0.92))
        cursor += length
    return out


def align_words(text: str, heard: List[Word], seconds: float, *, lead: float = 0.05,
                tail: float = 0.15) -> List[Word]:
    """The script's tokens at the times the recogniser heard them.

    Matched words take the heard time; a stretch heard differently (a figure
    read out, a brand split in two) shares its heard span; a token nobody
    heard sits between its neighbours. The karaoke therefore shows what the
    script says, never what the recogniser thought it heard.
    """
    tokens = [t for t in re.split(r"\s+", text.strip()) if t]
    if not tokens:
        return []
    if not heard:
        return spread_words(text, seconds, lead=lead, tail=tail)
    a = [_norm(t) for t in tokens]
    b = [_norm(str(w["text"])) for w in heard]
    spans: List[Optional[Tuple[float, float]]] = [None] * len(tokens)
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op == "equal" or (op == "replace" and i2 - i1 == j2 - j1):
            for k in range(i2 - i1):
                spans[i1 + k] = (float(heard[j1 + k]["start"]), float(heard[j1 + k]["end"]))
        elif op == "replace":
            start, end = float(heard[j1]["start"]), float(heard[j2 - 1]["end"])
            for k, span in enumerate(_even(start, end, tokens[i1:i2])):
                spans[i1 + k] = span
        # delete: tokens nobody heard, filled below; insert: heard words with no token, ignored
    end_limit = max(seconds - tail, lead + 0.1)
    i = 0
    while i < len(tokens):
        if spans[i] is not None:
            i += 1
            continue
        j = i
        while j < len(tokens) and spans[j] is None:
            j += 1
        start = spans[i - 1][1] if i else lead
        end = spans[j][0] if j < len(tokens) else end_limit
        if end <= start:
            end = start + 0.2 * (j - i)
        for k, span in enumerate(_even(start, end, tokens[i:j])):
            spans[i + k] = span
        i = j
    out: List[Word] = []
    floor = 0.0
    for token, span in zip(tokens, spans):
        start = max(float(span[0]), floor)
        end = max(float(span[1]), start + 0.05)
        floor = start
        out.append({"text": token, "start": round(start, 3), "end": round(end, 3)})
    return out


def _kokoro_synthesize(text: str, voice: str, mp3: Path) -> List[Word]:
    lang = lang2(voice)
    wav = mp3.with_suffix(".wav")
    seconds = _kokoro(text, kokoro_voice(voice), lang, wav)
    media.ffmpeg("-i", str(wav), "-c:a", "libmp3lame", "-q:a", "2", str(mp3), what="kokoro to mp3")
    try:
        heard = heard_words(wav, lang)
    except Exception as exc:  # timings are a nicety; the line is already spoken
        print(f"[studio] voice: whisper timings failed ({exc}); words spread evenly", flush=True)
        heard = []
    return align_words(text, heard, seconds)


# ---------------------------------------------------------------------------
# Azure Speech, then Edge
# ---------------------------------------------------------------------------

def _azure_config():
    key = (os.environ.get("SHORTS_AZURE_SPEECH_KEY") or "").strip()
    region = (os.environ.get("SHORTS_AZURE_SPEECH_REGION") or "").strip()
    return (key, region) if key and re.fullmatch(r"[a-z0-9]{3,30}", region) else None


def _azure(text: str, voice: str, rate: str, mp3: Path, key: str, region: str) -> None:
    """One request to Azure Speech; the key goes to its own host only."""
    import httpx

    lang = _lang(voice)
    body = escape(text)
    # HD voices keep their own pace; only a standard voice gets a prosody rate.
    if rate and rate != "+0%" and ":" not in voice:
        body = f"<prosody rate='{escape(rate)}'>{body}</prosody>"
    ssml = (f"<speak version='1.0' xml:lang='{escape(lang)}'><voice name='{escape(voice)}'>"
            f"{body}</voice></speak>")
    resp = httpx.post(AZURE_TTS.format(region=region), content=ssml.encode("utf-8"), timeout=60.0, headers={
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "audio-24khz-96kbitrate-mono-mp3",
        "User-Agent": "biglobster-shorts-studio",
    })
    if resp.status_code != 200 or not resp.content:
        raise VoiceError(f"Azure Speech: HTTP {resp.status_code} {resp.text[:200]}")
    mp3.write_bytes(resp.content)


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


# Edge answers 403 in bursts (2026-10-12: three in a row within 12 s, a run
# that had already paid for its presenter takes), so back off for over a minute.
BACKOFF_S = (3, 6, 12, 20, 30)


def synthesize(text: str, voice: str, rate: str, mp3: Path, *, attempts: int = len(BACKOFF_S) + 1) -> List[Word]:
    """Speak ``text`` into ``mp3``; return its words with beat-relative times.

    ``voice`` is the package's neural voice name (Martín's, forced by the
    package) or a Kokoro id (Lucía's free voice under Kokoro). Kokoro maps a
    neural name to its own voice for the language; Azure and Edge map a Kokoro
    id to the neural voice of the same kind.
    """
    if engine() == "kokoro":
        try:
            return _kokoro_synthesize(text, voice, mp3)
        except Exception as exc:  # the model files, the runtime, or the audio
            print(f"[studio] voice: Kokoro failed ({exc}); trying Azure", flush=True)
    if _KOKORO_ID.match(voice):
        voice = NEURAL_TWINS.get(voice[:2], EDGE_FALLBACK["en-US"])
    azure = _azure_config()
    if azure:
        try:
            _azure(text, voice, rate, mp3, *azure)
            if ":" in voice:
                fast = mp3.with_name(mp3.stem + "_fast.mp3")
                media.tempo(mp3, fast, HD_TEMPO)
                fast.replace(mp3)
            return spread_words(text, media.duration(mp3))
        except Exception as exc:  # Edge may still answer
            print(f"[studio] voice: Azure failed ({exc}); trying Edge", flush=True)
    if ":" in voice:
        voice = EDGE_FALLBACK.get(_lang(voice), voice)
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            words = asyncio.run(_edge_stream(text, voice, rate, mp3))
            if mp3.exists() and mp3.stat().st_size > 0:
                return words or spread_words(text, media.duration(mp3))
            last = VoiceError("Edge TTS returned no audio")
        except Exception as exc:  # network hiccups are the common case
            last = exc
        if attempt < attempts:
            time.sleep(BACKOFF_S[min(attempt, len(BACKOFF_S)) - 1])
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
