"""The short *package*: the one document an agent writes per video.

A package is plain JSON. The agent decides the words and the look; everything
that can be checked mechanically is checked here, on both sides of the render
boundary (Hermes before submitting, the Actions renderer before spending CPU).

Shape::

    {
      "version": 1,
      "lang": "en" | "es",
      "voice": "en-US-ChristopherNeural",          # optional, default per lang
      "article": {"url": "...", "title": "...", "slug": "..."},
      "style": {"palette": "indigo-coral", "motif": "diamonds"},
      "beats": [
        {"kind": "hook",  "vo": "...", "onscreen": "...", "kicker": "...", "broll": "...",
         "scene": "...", "presenter": true},
        {"kind": "point", "vo": "...", "onscreen": "...", "broll": "..."},
        {"kind": "stat",  "vo": "...", "value": "73%", "label": "...", "broll": "..."},
        {"kind": "list",  "vo": "...", "onscreen": "...", "items": ["...", "..."]},
        {"kind": "quote", "vo": "...", "onscreen": "..."},
        {"kind": "avatar","vo": "...", "clip_url": "https://...", "avatar": "martin"},
        {"kind": "cta",   "vo": "...", "onscreen": "...", "url": "biglobster.top/...",
         "presenter": true}
      ],
      "covers": {"cover_title": "...", "thumb_title": "..."},
      "social": {"youtube": {"title": "...", "description": "...", "tags": []},
                 "instagram": "...", "facebook": "...", "x": "..."},
      "music": {"mood": "corporate", "avoid_ids": []},
      "avoid_broll_ids": []
    }

The first beat is always the hook and the last is always the CTA. Scene
length follows the speech (the renderer measures each line), so nothing here
carries a duration.

``scene`` (optional, at most ``MAX_SCENES`` per package) asks the renderer to
generate that beat's background with a paid video model instead of stock
footage (``genvideo.py``). It describes a picture only: the model garbles
letters and numbers, so text, figures and logos always come from Remotion on
top, and a scene that asks for any of them is rejected here. ``broll`` stays
the fallback when the budget, the key or the model says no.

``"format": "tip"`` is the presenter format (``TIP_*`` below): ONE tip in
~20 s, and the whole script said to camera in one take by the presenter
(Lucía in Spanish, Martín in English), so one voice runs from the first
word to the last. Each beat's ``show`` picks the picture while that line is
said: ``presenter`` (them, full-frame) or ``card`` (an animated card — steps,
a before/after, a figure, a checklist, a key line — with the presenter in a
bubble). The hook and the CTA are always ``presenter``; at least two beats
are cards; the CTA asks the viewer to save or share it, because a tip worth
keeping is the point. No ``scene`` and no per-beat ``presenter`` flag here:
the take is the picture. ``compare`` (tip only) is a before/after:
``before`` and ``after``, ≤8 words each.

``presenter: true`` (hook and CTA only) has the brand's presenter say that
line to camera — Lucía in Spanish, Martín in English — with a generated
talking clip (``presenter.py``). The line is short (``MAX_PRESENTER_WORDS``)
because the presenter costs by the second. If the clip is not made, the beat
falls back to the voice-over, over its ``scene`` or ``broll``.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

PACKAGE_VERSION = 1

DEFAULT_VOICES = {
    "en": "en-US-ChristopherNeural",
    "es": "es-ES-AlvaroNeural",
}

# Mirrored in shorts/studio/remotion/src/theme.ts — a test asserts they match.
PALETTES = (
    "indigo-coral",
    "teal-amber",
    "violet-lime",
    "cobalt-pink",
    "emerald-gold",
    "crimson-sky",
    "slate-cyan",
    "orange-navy",
)
MOTIFS = ("diamonds", "circles", "chevrons", "dots", "bars", "rings", "grid", "waves")

BEAT_KINDS = ("hook", "point", "stat", "list", "quote", "avatar", "cta")
MIDDLE_KINDS = ("point", "stat", "list", "quote", "avatar")

FORMATS = ("classic", "tip")

MIN_BEATS = 4          # hook + 2 middle + cta
MAX_BEATS = 10         # hook + 8 middle + cta

# ~150 spoken words a minute. 55–65s is the target; these are the hard walls
# either side of it, so a script cannot be accidentally a 20s or a 2-minute one.
MIN_TOTAL_WORDS = 70
MAX_TOTAL_WORDS = 200
MAX_VO_WORDS = 45           # one beat, one breath-sized idea or two
MAX_ONSCREEN_WORDS = 8
MAX_ONSCREEN_CHARS = 60
MAX_KICKER_CHARS = 28
MAX_STAT_VALUE_CHARS = 8
MAX_LIST_ITEMS = 4
MAX_LIST_ITEM_WORDS = 5
MAX_COVER_TITLE_WORDS = 7
MAX_HOOK_LEAD_WORDS = 8     # the hook's first sentence: ~3 s at the voices' pace
MAX_SCENES = 3              # generated backgrounds per short; the rest is stock footage
SCENE_KINDS = ("hook", "point", "stat", "list", "quote")
MIN_SCENE_WORDS = 6
PRESENTER_KINDS = ("hook", "cta")
MAX_PRESENTER_WORDS = 24    # ~11 s said to camera; the presenter is paid by the second
MAX_SCENE_WORDS = 60
MAX_SCENE_CHARS = 400

# The tip format: one take said to camera, paid by the second (~$0.05/s at
# ~3 words/s), so the script is short — one tip, ~15-20 s.
TIP_MIDDLE_KINDS = ("point", "stat", "list", "compare", "quote")
TIP_MAX_BEATS = 7
TIP_MIN_TOTAL_WORDS = 40
# Fits $1 a take at $0.05/s with each presenter's pace (presenter.WORDS_PER_SECOND):
# Martín ~3 words/s, Lucía ~2.6.
TIP_MAX_TOTAL_WORDS = {"en": 55, "es": 48}
TIP_MIN_CARDS = 2
SHOWS = ("presenter", "card")
MAX_COMPARE_WORDS = 8
# The CTA asks for the save or the share: a tip people keep is the goal.
_SAVE_OR_SHARE = re.compile(
    r"\b(save|share|bookmark|send (it|this)|guárdalo|guardalo|guárdate|guarda(lo|te)?|"
    r"compárte(lo)?|comparte(lo)?|compartir|mándaselo|envíaselo|pásaselo)\b",
    re.IGNORECASE,
)

MAX_X_CHARS = 280
MAX_YT_TITLE_CHARS = 100
MAX_YT_DESCRIPTION_CHARS = 4800
MAX_IG_CAPTION_CHARS = 2200
MAX_HASHTAGS = 30           # Instagram's own ceiling; the prompt asks for far fewer
MAX_YT_TAGS = 15

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_WORD = re.compile(r"\S+")
# The first three seconds decide whether anyone keeps watching, so the hook
# opens on the promise itself: never a greeting, a self-introduction or talk
# about the video.
_HOOK_FILLER = re.compile(
    r"^\W*(hi|hey|hello|welcome|today|in this (video|short)|this video|let'?s talk|"
    r"i'?m |my name|we'?re |at biglobster|"
    r"hola|buenas|bienvenid[oa]s?|hoy |en este (v[ií]deo|short)|este v[ií]deo|vamos a hablar|"
    r"soy |me llamo|somos |en biglobster)",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[.!?…]+")

# What a generated background must never be asked for: rendered lettering,
# figures or marks (the model garbles them, and they would bypass the
# grounding check), or someone else's brand.
_SCENE_FORBIDDEN = re.compile(
    r"\b(text|texts|caption|captions|subtitles?|letters?|lettering|typography|fonts?|words?|"
    r"headlines?|writing|written|logos?|watermarks?|brand(?:ed|s)?|trademarks?|signage|"
    r"numbers?|digits?|charts?|graphs?|infographics?|"
    r"texto|letras?|rótulos?|carteles?|logotipos?|marcas?|números?|cifras?|gráficos?)\b",
    re.IGNORECASE,
)


class PackageError(ValueError):
    """Raised with every problem found, one per line."""

    def __init__(self, problems: List[str]):
        self.problems = problems
        super().__init__("\n".join(f"- {p}" for p in problems))


def _words(text: str) -> int:
    return len(_WORD.findall(text or ""))


def _text(value: Any) -> str:
    """Coerce to a single-line-safe string with control characters removed."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value))
    return _CONTROL.sub(" ", text).strip()


def _check_text(problems: List[str], where: str, text: str, *,
                required: bool = True, max_words: int = 0, max_chars: int = 0) -> None:
    if not text:
        if required:
            problems.append(f"{where}: required")
        return
    if max_words and _words(text) > max_words:
        problems.append(f"{where}: {_words(text)} words, max {max_words}: {text!r}")
    if max_chars and len(text) > max_chars:
        problems.append(f"{where}: {len(text)} chars, max {max_chars}: {text!r}")


def _check_hook_lead(problems: List[str], where: str, vo: str) -> None:
    """The hook's first ~3 seconds: the promise or the pain, said at once."""
    if _HOOK_FILLER.match(vo):
        problems.append(
            f"{where}.vo: the hook must open on the hook itself — a pain, a figure or the "
            f"benefit for the viewer — never a greeting, an introduction or talk about the video: {vo!r}"
        )
    lead = _SENTENCE_END.split(vo.lstrip("¿¡ "), maxsplit=1)[0]
    if _words(lead) > MAX_HOOK_LEAD_WORDS:
        problems.append(
            f"{where}.vo: the hook's first sentence has {_words(lead)} words, max "
            f"{MAX_HOOK_LEAD_WORDS} (~3 s): land the promise first, explain after: {lead!r}"
        )


def _check_tip_beat(problems: List[str], where: str, b: Dict[str, Any], beat: Dict[str, Any],
                    kind: str, i: int, n: int) -> None:
    """Tip format: what is on screen while this line is said, and the before/after."""
    show = _text(b.get("show")) or ("presenter" if kind in ("hook", "cta") else "card")
    if show not in SHOWS:
        problems.append(f"{where}.show: one of {list(SHOWS)}, got {show!r}")
    elif kind in ("hook", "cta") and show != "presenter":
        problems.append(f"{where}.show: the hook and the CTA are said to camera ('presenter')")
    beat["show"] = show
    if kind == "compare":
        for side in ("before", "after"):
            beat[side] = _text(b.get(side))
            _check_text(problems, f"{where}.{side}", beat[side],
                        max_words=MAX_COMPARE_WORDS, max_chars=MAX_ONSCREEN_CHARS)
        beat["onscreen"] = _text(b.get("onscreen"))
        _check_text(problems, f"{where}.onscreen", beat["onscreen"], required=False,
                    max_words=MAX_ONSCREEN_WORDS, max_chars=MAX_ONSCREEN_CHARS)
        if beat["show"] != "card":
            problems.append(f"{where}.show: a compare beat is a card")


def _host(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def validate_package(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Return a normalised copy of ``raw`` or raise :class:`PackageError`.

    Normalisation fills defaults (voice, version) and strips control
    characters; it never rewrites the agent's words.
    """
    problems: List[str] = []
    if not isinstance(raw, dict):
        raise PackageError(["package must be a JSON object"])

    pkg: Dict[str, Any] = {"version": PACKAGE_VERSION}

    lang = _text(raw.get("lang")).lower()
    if lang not in DEFAULT_VOICES:
        problems.append(f"lang: must be one of {sorted(DEFAULT_VOICES)}, got {lang!r}")
    pkg["lang"] = lang
    voice = _text(raw.get("voice")) or DEFAULT_VOICES.get(lang, "")
    if voice and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}-[A-Za-z]+Neural", voice):
        problems.append(f"voice: not an Edge neural voice name: {voice!r}")
    if voice and lang and not voice.lower().startswith(lang + "-"):
        problems.append(f"voice: {voice!r} does not speak lang {lang!r}")
    pkg["voice"] = voice
    rate = _text(raw.get("rate")) or "+0%"
    if not re.fullmatch(r"[+-]\d{1,2}%", rate):
        problems.append(f"rate: expected like '+0%', got {rate!r}")
    pkg["rate"] = rate

    article = raw.get("article") if isinstance(raw.get("article"), dict) else {}
    art = {
        "url": _text(article.get("url")),
        "title": _text(article.get("title")),
        "slug": _text(article.get("slug")),
    }
    if not art["url"].startswith("https://"):
        problems.append("article.url: must be the https URL of the source post")
    if not art["title"]:
        problems.append("article.title: required")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,100}", art["slug"]):
        problems.append(f"article.slug: lowercase letters, digits and dashes only, got {art['slug']!r}")
    pkg["article"] = art
    article_host = _host(art["url"]) if art["url"] else ""

    style = raw.get("style") if isinstance(raw.get("style"), dict) else {}
    palette = _text(style.get("palette"))
    motif = _text(style.get("motif"))
    if palette not in PALETTES:
        problems.append(f"style.palette: one of {list(PALETTES)}, got {palette!r}")
    if motif not in MOTIFS:
        problems.append(f"style.motif: one of {list(MOTIFS)}, got {motif!r}")
    pkg["style"] = {"palette": palette, "motif": motif}

    fmt = _text(raw.get("format")) or "classic"
    if fmt not in FORMATS:
        problems.append(f"format: one of {list(FORMATS)}, got {fmt!r}")
    tip = fmt == "tip"
    pkg["format"] = fmt
    if tip:
        pkg["version"] = 2

    beats_raw = raw.get("beats")
    beats: List[Dict[str, Any]] = []
    if not isinstance(beats_raw, list):
        problems.append("beats: must be a list")
        beats_raw = []
    max_beats = TIP_MAX_BEATS if tip else MAX_BEATS
    middle_kinds = TIP_MIDDLE_KINDS if tip else MIDDLE_KINDS
    if beats_raw and not (MIN_BEATS <= len(beats_raw) <= max_beats):
        problems.append(f"beats: {len(beats_raw)} beats, need {MIN_BEATS}-{max_beats} (hook + middle + cta)")

    total_words = 0
    for i, b in enumerate(beats_raw):
        where = f"beats[{i}]"
        if not isinstance(b, dict):
            problems.append(f"{where}: must be an object")
            continue
        kind = _text(b.get("kind"))
        if i == 0 and kind != "hook":
            problems.append(f"{where}: the first beat must be kind 'hook', got {kind!r}")
        elif i == len(beats_raw) - 1 and kind != "cta":
            problems.append(f"{where}: the last beat must be kind 'cta', got {kind!r}")
        elif 0 < i < len(beats_raw) - 1 and kind not in middle_kinds:
            problems.append(f"{where}: middle beats are one of {list(middle_kinds)}, got {kind!r}")

        beat: Dict[str, Any] = {"kind": kind, "vo": _text(b.get("vo"))}
        _check_text(problems, f"{where}.vo", beat["vo"], max_words=MAX_VO_WORDS)
        if i == 0 and kind == "hook" and beat["vo"]:
            _check_hook_lead(problems, where, beat["vo"])
        total_words += _words(beat["vo"])

        broll = _text(b.get("broll"))
        if broll:
            _check_text(problems, f"{where}.broll", broll, max_words=6, max_chars=60)
            beat["broll"] = broll

        if tip:
            _check_tip_beat(problems, where, b, beat, kind, i, len(beats_raw))

        scene = _text(b.get("scene"))
        if scene and tip:
            problems.append(f"{where}.scene: the tip format has no generated scenes; the take is the picture")
        elif scene:
            if kind not in SCENE_KINDS:
                problems.append(f"{where}.scene: only {list(SCENE_KINDS)} beats get a generated scene")
            else:
                _check_text(problems, f"{where}.scene", scene,
                            max_words=MAX_SCENE_WORDS, max_chars=MAX_SCENE_CHARS)
                if _words(scene) < MIN_SCENE_WORDS:
                    problems.append(f"{where}.scene: describe the picture in at least "
                                    f"{MIN_SCENE_WORDS} words: {scene!r}")
                bad = sorted({m.group(0).lower() for m in _SCENE_FORBIDDEN.finditer(scene)})
                if bad:
                    problems.append(
                        f"{where}.scene: a generated scene is a picture only, never text, figures or "
                        f"brands (Remotion draws those on top); remove {bad}: {scene!r}"
                    )
                beat["scene"] = scene

        presenter = b.get("presenter")
        if presenter not in (None, False) and tip:
            problems.append(f"{where}.presenter: not in the tip format, where the whole script is "
                            "said to camera; use show")
        elif presenter not in (None, False):
            if presenter is not True:
                problems.append(f"{where}.presenter: true or absent, got {presenter!r}")
            elif kind not in PRESENTER_KINDS:
                problems.append(f"{where}.presenter: only the hook and the CTA are said to camera")
            elif _words(beat["vo"]) > MAX_PRESENTER_WORDS:
                problems.append(f"{where}.vo: a line said to camera is {MAX_PRESENTER_WORDS} words "
                                f"at most, got {_words(beat['vo'])}")
            else:
                beat["presenter"] = True

        if kind in ("hook", "point", "quote", "list", "cta"):
            beat["onscreen"] = _text(b.get("onscreen"))
            _check_text(problems, f"{where}.onscreen", beat["onscreen"],
                        max_words=MAX_ONSCREEN_WORDS, max_chars=MAX_ONSCREEN_CHARS)
        if kind == "hook":
            kicker = _text(b.get("kicker"))
            _check_text(problems, f"{where}.kicker", kicker, required=False,
                        max_chars=MAX_KICKER_CHARS)
            if kicker:
                beat["kicker"] = kicker
        if kind == "stat":
            beat["value"] = _text(b.get("value"))
            beat["label"] = _text(b.get("label"))
            _check_text(problems, f"{where}.value", beat["value"], max_chars=MAX_STAT_VALUE_CHARS)
            if beat["value"] and not re.search(r"\d", beat["value"]):
                problems.append(f"{where}.value: a stat needs a number, got {beat['value']!r}")
            _check_text(problems, f"{where}.label", beat["label"],
                        max_words=MAX_ONSCREEN_WORDS, max_chars=MAX_ONSCREEN_CHARS)
        if kind == "list":
            items = b.get("items") if isinstance(b.get("items"), list) else []
            beat["items"] = [_text(x) for x in items if _text(x)]
            if not 2 <= len(beat["items"]) <= MAX_LIST_ITEMS:
                problems.append(f"{where}.items: 2-{MAX_LIST_ITEMS} items, got {len(beat['items'])}")
            for j, item in enumerate(beat["items"]):
                _check_text(problems, f"{where}.items[{j}]", item,
                            max_words=MAX_LIST_ITEM_WORDS, max_chars=40)
        if kind == "avatar":
            beat["clip_url"] = _text(b.get("clip_url"))
            beat["avatar"] = _text(b.get("avatar")).lower()
            if not beat["clip_url"].startswith("https://"):
                problems.append(f"{where}.clip_url: an https URL of the avatar clip is required")
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", beat["avatar"]):
                problems.append(f"{where}.avatar: name the avatar, e.g. 'martin' or 'lucia'")
        if kind == "cta":
            beat["url"] = _text(b.get("url"))
            if not beat["url"]:
                problems.append(f"{where}.url: the CTA must point at the article")
            elif article_host and _host(beat["url"]) != article_host:
                problems.append(
                    f"{where}.url: must be on the article's own site ({article_host}), "
                    f"got {beat['url']!r}"
                )
        beats.append(beat)

    scenes = sum(1 for b in beats if b.get("scene"))
    if scenes > MAX_SCENES:
        problems.append(f"beats: {scenes} generated scenes, max {MAX_SCENES} per short "
                        "(the hook and the most visual beats; the rest uses broll)")

    lo, hi = ((TIP_MIN_TOTAL_WORDS, TIP_MAX_TOTAL_WORDS.get(lang, 48)) if tip
              else (MIN_TOTAL_WORDS, MAX_TOTAL_WORDS))
    if beats_raw and not (lo <= total_words <= hi):
        problems.append(
            f"beats: {total_words} spoken words in total, need {lo}-{hi} "
            + ("(one tip, ~20 s said to camera)" if tip else "(~150 words is a 60s short)")
        )
    if tip and beats:
        cards = sum(1 for b in beats if b.get("show") == "card")
        if cards < TIP_MIN_CARDS:
            problems.append(f"beats: {cards} card beats, need at least {TIP_MIN_CARDS}: show the tip, "
                            "do not only say it")
        cta = beats[-1]
        if cta.get("kind") == "cta" and cta.get("vo") and not _SAVE_OR_SHARE.search(cta["vo"]):
            problems.append(f"beats[{len(beats) - 1}].vo: the CTA asks the viewer to save the tip or "
                            f"share it with someone it helps: {cta['vo']!r}")
    pkg["beats"] = beats

    covers = raw.get("covers") if isinstance(raw.get("covers"), dict) else {}
    pkg["covers"] = {
        "cover_title": _text(covers.get("cover_title")),
        "thumb_title": _text(covers.get("thumb_title")),
    }
    _check_text(problems, "covers.cover_title", pkg["covers"]["cover_title"],
                max_words=MAX_COVER_TITLE_WORDS, max_chars=50)
    _check_text(problems, "covers.thumb_title", pkg["covers"]["thumb_title"],
                max_words=MAX_COVER_TITLE_WORDS, max_chars=50)

    pkg["social"] = _validate_social(raw.get("social"), problems)

    music = raw.get("music") if isinstance(raw.get("music"), dict) else {}
    mood = _text(music.get("mood")) or "corporate"
    if not re.fullmatch(r"[a-z][a-z-]{1,30}", mood):
        problems.append(f"music.mood: a single lowercase tag like 'corporate', got {mood!r}")
    pkg["music"] = {
        "mood": mood,
        "avoid_ids": [str(x) for x in (music.get("avoid_ids") or []) if str(x).strip()][:200],
    }
    pkg["avoid_broll_ids"] = [
        str(x) for x in (raw.get("avoid_broll_ids") or []) if str(x).strip()
    ][:500]

    request_id = _text(raw.get("request_id"))
    if request_id:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{5,90}", request_id):
            problems.append(f"request_id: invalid {request_id!r}")
        pkg["request_id"] = request_id

    if problems:
        raise PackageError(problems)
    return pkg


def _hashtags(text: str) -> List[str]:
    return re.findall(r"(?<!\w)#\w+", text or "")


def _validate_social(raw: Any, problems: List[str]) -> Dict[str, Any]:
    social = raw if isinstance(raw, dict) else {}
    yt = social.get("youtube") if isinstance(social.get("youtube"), dict) else {}
    out = {
        "youtube": {
            "title": _text(yt.get("title")),
            "description": str(yt.get("description") or "").strip(),
            "tags": [_text(t) for t in (yt.get("tags") or []) if _text(t)],
        },
        "instagram": str(social.get("instagram") or "").strip(),
        "facebook": str(social.get("facebook") or "").strip(),
        "x": str(social.get("x") or "").strip(),
    }
    _check_text(problems, "social.youtube.title", out["youtube"]["title"],
                max_chars=MAX_YT_TITLE_CHARS)
    _check_text(problems, "social.youtube.description", out["youtube"]["description"],
                max_chars=MAX_YT_DESCRIPTION_CHARS)
    if "<" in out["youtube"]["title"] or ">" in out["youtube"]["title"]:
        problems.append("social.youtube.title: YouTube rejects '<' and '>' in titles")
    if len(out["youtube"]["tags"]) > MAX_YT_TAGS:
        problems.append(f"social.youtube.tags: max {MAX_YT_TAGS}")
    _check_text(problems, "social.instagram", out["instagram"], max_chars=MAX_IG_CAPTION_CHARS)
    _check_text(problems, "social.facebook", out["facebook"], max_chars=MAX_IG_CAPTION_CHARS)
    _check_text(problems, "social.x", out["x"], max_chars=MAX_X_CHARS)
    for key in ("instagram", "facebook"):
        if len(_hashtags(out[key])) > MAX_HASHTAGS:
            problems.append(f"social.{key}: {len(_hashtags(out[key]))} hashtags, max {MAX_HASHTAGS}")
    return out


# ---------------------------------------------------------------------------
# Grounding — every figure in the short must be in the article
# ---------------------------------------------------------------------------

# A "figure" is a digit run with its decorations: 73%, 1,200, 3.5, €40, 2026.
_FIGURE = re.compile(r"[$€£]?\d[\d.,]*(?:\s?%|[kKMx](?![A-Za-z]))?")

# Counting words ("3 mistakes", "step 2") are not claims. Anything decorated
# (%, currency, multiplier) or larger than this is.
_BARE_COUNT_MAX = 10


def _normalise_figure(token: str) -> Tuple[str, bool]:
    """Return (digits-only core, is_claim)."""
    token = token.strip()
    decorated = bool(re.search(r"[%$€£kKMx]", token))
    core = re.sub(r"[^\d.,]", "", token).strip(".,")
    # 1,200 / 1.200 / 1200 all normalise to 1200; 3.5 / 3,5 to 3.5
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", core):
        core = re.sub(r"[.,]", "", core)
    else:
        core = core.replace(",", ".")
    try:
        is_claim = decorated or float(core) > _BARE_COUNT_MAX
    except ValueError:
        is_claim = decorated
    return core, is_claim


def figures(text: str) -> List[str]:
    """Figures in ``text`` that count as factual claims, normalised."""
    out: List[str] = []
    for match in _FIGURE.finditer(text or ""):
        core, is_claim = _normalise_figure(match.group(0))
        if core and is_claim:
            out.append(core)
    return out


def _article_figures(article_text: str) -> set:
    found = set()
    for match in _FIGURE.finditer(article_text or ""):
        core, _ = _normalise_figure(match.group(0))
        if core:
            found.add(core)
    return found


def check_grounding(pkg: Dict[str, Any], article_text: str) -> List[str]:
    """Return one problem per figure the short states and the article does not.

    Deliberately narrow: it cannot judge a paraphrase, but it can prove that
    "73%" in a video came from somewhere. An agent that would invent a number
    would also swear it was sourced, so the check reads the article itself.
    """
    known = _article_figures(article_text)
    problems: List[str] = []
    for i, beat in enumerate(pkg.get("beats") or []):
        fields = [("vo", beat.get("vo")), ("onscreen", beat.get("onscreen")),
                  ("value", beat.get("value")), ("label", beat.get("label")),
                  ("before", beat.get("before")), ("after", beat.get("after"))]
        fields += [(f"items[{j}]", item) for j, item in enumerate(beat.get("items") or [])]
        for name, text in fields:
            for fig in figures(text or ""):
                if fig not in known:
                    problems.append(
                        f"beats[{i}].{name}: the figure {fig!r} does not appear in the article"
                    )
    covers = pkg.get("covers") or {}
    for name in ("cover_title", "thumb_title"):
        for fig in figures(covers.get(name) or ""):
            if fig not in known:
                problems.append(f"covers.{name}: the figure {fig!r} does not appear in the article")
    return problems


def spoken_text(pkg: Dict[str, Any]) -> str:
    """The full voiceover, beat by beat — handy for reports and the SRT."""
    return "\n".join(b.get("vo", "") for b in pkg.get("beats") or [])
