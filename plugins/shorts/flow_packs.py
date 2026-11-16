"""Script packs for the owner's Google Flow shorts: write one, validate it, learn from his edits.

The cron ``short`` agent writes ONE pack per run, in Markdown, for a Short the owner
films himself in Google Flow. The pack is a contract, not prose::

    # <título del short>
    Artículo: <https URL on biglobster.top>
    Idioma: es|en

    ## Historia
    <one paragraph>

    ## Clips
    ### Clip 0 — <name> (<N> s)
    ```
    <English prompt for Google Flow>
    ```
    ...

    ## Rótulos
    | Clip | Posición | Retardo (s) | Texto |
    |---|---|---|---|
    | 0 | arriba | 0 | ¿Tu tienda vacía? |

    ## Portada
    Título: <text>
    Rótulo: <text>

    ## Antes de rodar
    - <bullets>

Everything the agent could get subtly wrong mechanically is decided here, not in its
prompt: the language has to match the Drive folder, the article has to be a real
``biglobster.top`` post in that language, the clips have to fit the owner's 45 s Short,
the two BigLobster characters have to keep their voices, on-screen captions have to be
readable, and nothing drifts into Galician or Portuguese (on 2026-11-11 the retired Buzz
writer did exactly that through its own memory file, and nobody caught it: see
``FOREIGN_MARKERS``).

The owner reads the pack in Drive ``Flow/<carpeta>/prompts.md``, edits it while filming,
and the edited file comes back through the inbox with his footage. The inbox keeps the
difference as a lesson (``lessons/<carpeta>.diff``), and the writer gets the newest five
in ``next_pack`` before writing the following pack.

The first ``FLOW_PACK_REVIEW_FIRST`` packs (default 3) wait in ``review`` until a human
releases them: ``write`` is the agent's step, ``release`` is the owner's.

Config (service env of the profile that runs the shorts jobs):

    FLOW_PACK_LANGS          languages the writer may pick      (default es,en)
    FLOW_PACK_REVIEW_FIRST   packs held for review first        (default 3)
    FLOW_PACKS_WAITING_MAX   packs waiting for him before the writer stops (default 3)

Storage lives under ``shorts_root()/flow/packs``::

    index.json             {carpeta: {lang, article_url, title, state, written_at, released_at?}}
    <carpeta>.md           the pack exactly as the agent wrote it (the "original")
    lessons/<carpeta>.diff the owner's edits, as a unified diff
"""

from __future__ import annotations

import calendar
import difflib
import logging
import re
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from plugins.shorts import flow_inbox, series, studio_tool
from plugins.shorts._env import env

logger = logging.getLogger(__name__)

# The two BigLobster characters are already loaded in Flow: every take keeps their
# voices, so only the action changes from clip to clip.
VOICE_LINE = {
    "es": "Lucía and Martín are the BigLobster characters already loaded in Flow, with the same "
          "voices as in every previous take; they speak Spain Spanish with a light Galician accent.",
    "en": "Lucía and Martín are the BigLobster characters already loaded in Flow, with the same "
          "voices as in every previous take; they speak English with a light Galician accent.",
}
PROMPT_END = "No music, no subtitles, no text on screen."
CLIPS_MIN, CLIPS_MAX = 4, 6
CLIP_S = (3, 10)            # seconds per clip
TOTAL_MAX_S = 45
CARPETA = flow_inbox.CARPETA

# Words that only Galician or Portuguese use, never Spanish or English. On 2026-11-11
# the retired Buzz writer drifted into Galician through its own memory file and nobody
# caught it until the owner filmed; a pack now fails at three of these, anywhere.
FOREIGN_MARKERS = {
    "hoxe", "unha", "tamén", "xa", "moito", "moita", "cando", "agora", "desta", "deste",
    "dun", "dunha", "polo", "pola", "nun", "nunha", "coa", "co", "aínda", "máis", "non",
    "nao", "não", "você", "muito", "também", "então", "está-se", "isso", "isto", "fazer", "ou",
}

# Small stopword sets: a Spanish line is not English when more than half its words are
# here; an English line is not Spanish when more than a third are. A prompt is English
# when at least five of its words are.
ENGLISH_WORDS = frozenset("the and you your is are to of in it for with on that this we our".split())
SPANISH_WORDS = frozenset("el la los las de que y en un una es tu su por con para no se".split())
PROMPT_ENGLISH_MIN = 5

SECTIONS = ("Historia", "Clips", "Rótulos", "Portada", "Antes de rodar")

CLIP_HEADING = re.compile(r"^###\s+Clip\s+(\d+)\s*[—-]\s*(.+?)\s*\((\d+(?:[.,]\d+)?)\s*s\)\s*$", re.MULTILINE)
FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
OUTSIDE = re.compile(r"```.*?```", re.DOTALL)
SECTION = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
TITLE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
QUOTE = re.compile(r'"([^"]*)"')
WORD = re.compile(r"[^\W_]+(?:-[^\W_]+)*", re.UNICODE)
NAME = re.compile(r"\blucia\b|\bmartin\b")


# ---------------------------------------------------------------------------
# Parsing — never raises: a half-written pack parses into what is there
# ---------------------------------------------------------------------------

def _body(markdown: str, name: str) -> str:
    """The text under ``## <name>``, up to the next heading."""
    match = re.search(rf"^##\s+{re.escape(name)}\s*$", markdown, re.MULTILINE)
    if match is None:
        return ""
    rest = markdown[match.end():]
    following = HEADING.search(rest)
    return (rest[:following.start()] if following else rest).strip()


def _header(markdown: str) -> str:
    """Everything before the first ``## ``, where the title, article and language live."""
    first = re.search(r"^##\s+", markdown, re.MULTILINE)
    return markdown[:first.start()] if first else markdown


def _field(text: str, name: str) -> Optional[str]:
    match = re.search(rf"^{re.escape(name)}:\s*(.*?)\s*$", text, re.MULTILINE)
    return match.group(1) if match else None


def _fenced(markdown: str, start: int) -> Optional[str]:
    match = FENCE.search(markdown, start)
    return match.group(1).strip() if match else None


def _int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> Optional[float]:
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def _position(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    if text in ("arriba", "top"):
        return "top"
    if text in ("abajo", "bottom"):
        return "bottom"
    return text or None


def _rows(markdown: str) -> list[list[str]]:
    """The Rótulos table's data rows: header and ``|---|`` rows left out."""
    rows: list[list[str]] = []
    for line in _body(markdown, "Rótulos").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        if all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells):
            continue
        if cells[0].lower() == "clip":
            continue
        rows.append(cells[:4])
    return rows


def _overlay(row: list[str]) -> dict[str, Any]:
    return {"clip": _int(row[0]), "position": _position(row[1]),
            "delay_s": _float(row[2]), "text": row[3]}


def parse(markdown: str) -> dict[str, Any]:
    """What the pack says, normalised; missing parts are empty or ``None``. Never raises."""
    text = str(markdown or "")
    header = _header(text)
    title = TITLE.search(header)
    clips = [{"index": int(match.group(1)), "name": match.group(2).strip(),
              "seconds": float(match.group(3).replace(",", ".")),
              "prompt": _fenced(text, match.end())}
             for match in CLIP_HEADING.finditer(text)]
    cover_body = _body(text, "Portada")
    return {
        "title": title.group(1).strip() if title else None,
        "article_url": _field(header, "Artículo"),
        "lang": _field(header, "Idioma"),
        "story": _body(text, "Historia") or None,
        "clips": clips,
        "overlays": [_overlay(row) for row in _rows(text)],
        "cover": {"title": _field(cover_body, "Título"), "label": _field(cover_body, "Rótulo")},
        "before": _body(text, "Antes de rodar"),
        "sections": [match.group(1).strip() for match in SECTION.finditer(text)],
    }


# ---------------------------------------------------------------------------
# Language checks
# ---------------------------------------------------------------------------

def _words(text: Any) -> list[str]:
    return WORD.findall(str(text or "").lower())


def _markers(text: Any) -> list[str]:
    """Foreign-word occurrences in ``text``; whole lowercase words only."""
    return [word for word in _words(text) if word in FOREIGN_MARKERS]


def _named(markers: list[str]) -> str:
    return ", ".join(sorted(set(markers)))


def _ratio(text: Any, vocabulary: frozenset) -> float:
    words = _words(text)
    if not words:
        return 0.0
    return sum(1 for word in words if word in vocabulary) / len(words)


def _plain(text: Any) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _name_count(text: Any) -> int:
    return len(NAME.findall(_plain(text)))


# ---------------------------------------------------------------------------
# Validation — every problem, one string each, so the agent can fix them all
# ---------------------------------------------------------------------------

def _check_carpeta(carpeta: str, lang: Optional[str], problems: list[str]) -> None:
    match = CARPETA.match(str(carpeta or ""))
    if match is None:
        problems.append(f"carpeta: {carpeta!r} must look like 2026-11-14_es-slug (flow_inbox.CARPETA)")
    elif lang and lang != match.group(1):
        problems.append(f"carpeta: {carpeta!r} is {match.group(1)} but the pack says Idioma: {lang}")


def _check_article(url: Optional[str], lang: Optional[str], problems: list[str]) -> None:
    value = str(url or "").strip()
    parts = urlparse(value)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (host == "biglobster.top" or host.endswith(".biglobster.top")):
        problems.append(f"Artículo: {value!r} must be an https URL on biglobster.top")
        return
    if lang == "es" and not parts.path.startswith("/es/"):
        problems.append(f"Artículo: an es pack needs a /es/ path on biglobster.top, not {value!r}")
    if lang == "en" and parts.path.startswith("/es/"):
        problems.append(f"Artículo: an en pack needs the English path, not {value!r}")


def _check_sections(sections: list[str], problems: list[str]) -> None:
    for name in SECTIONS:
        if name not in sections:
            problems.append(f"section: the pack has no ## {name}")


def _check_clips(pack: dict[str, Any], problems: list[str]) -> None:
    clips = pack["clips"]
    if not CLIPS_MIN <= len(clips) <= CLIPS_MAX:
        problems.append(f"clips: {len(clips)} clips; a pack has {CLIPS_MIN} to {CLIPS_MAX}")
    indexes = [clip["index"] for clip in clips]
    if indexes != list(range(len(clips))):
        found = ", ".join(str(index) for index in indexes) or "none"
        problems.append(f"clips: number them 0 to {len(clips) - 1} in order (found {found})")
    for clip in clips:
        where = f"clips[{clip['index']}]"
        if not CLIP_S[0] <= clip["seconds"] <= CLIP_S[1]:
            problems.append(f"{where}: {clip['seconds']:g} s; each clip is {CLIP_S[0]} to {CLIP_S[1]} s")
        if clip["prompt"] is None:
            problems.append(f"{where}: no prompt (a fenced block under the heading)")
    total = sum(clip["seconds"] for clip in clips)
    if total > TOTAL_MAX_S:
        problems.append(f"clips: {total:g} s in total; a pack is {TOTAL_MAX_S} s at most")


def _check_span(span: str, where: str, lang: Optional[str], problems: list[str]) -> None:
    markers = _markers(span)
    if len(markers) >= 3:
        problems.append(f"{where}: the quoted line has foreign words ({_named(markers)})")
    if lang == "es" and _ratio(span, ENGLISH_WORDS) > 0.5:
        problems.append(f"{where}: the quoted line is English; this is an es pack")
    if lang == "en" and _ratio(span, SPANISH_WORDS) > 1 / 3:
        problems.append(f"{where}: the quoted line is Spanish; this is an en pack")
    words = _words(span)
    # 3, not 6: the owner's own lines are that short ("Primer error: perfil a medias").
    if not 3 <= len(words) <= 30:
        problems.append(f"{where}: a quoted line is 3 to 30 words, this one has {len(words)}")


def _check_prompt(clip: dict[str, Any], lang: Optional[str], problems: list[str]) -> None:
    prompt = clip["prompt"]
    where = f"clips[{clip['index']}].prompt"
    if not prompt.rstrip().endswith(PROMPT_END):
        problems.append(f"{where}: must end with {PROMPT_END!r}")
    if sum(1 for word in _words(prompt) if word in ENGLISH_WORDS) < PROMPT_ENGLISH_MIN:
        problems.append(f"{where}: does not read as English; the prompts are in English")
    spans = QUOTE.findall(prompt)
    if clip["index"] >= 1:
        voice = VOICE_LINE.get(lang or "")
        if voice and voice not in prompt:
            problems.append(f"{where}: add the fixed voice line verbatim")
        if _name_count(prompt) <= _name_count(voice or ""):
            problems.append(f"{where}: name Lucía or Martín in the action, outside the voice line")
        if not spans:
            problems.append(f"{where}: no spoken words; quote the line the characters say")
    for span in spans:
        _check_span(span, where, lang, problems)


def _check_overlays(clip_indexes: set[int], markdown: str, problems: list[str]) -> None:
    rows = _rows(markdown)
    if not 1 <= len(rows) <= 12:
        problems.append(f"rótulos: {len(rows)} rows; a pack has 1 to 12")
    for index, (clip_raw, position, delay_raw, text) in enumerate(rows):
        where = f"rótulos[{index}]"
        clip = _int(clip_raw)
        if clip is None or clip not in clip_indexes:
            problems.append(f"{where}: clip {clip_raw!r} is not one of the pack's clips")
        if _position(position) not in ("top", "bottom"):
            problems.append(f"{where}: position arriba or abajo (top or bottom), not {position!r}")
        delay = _float(delay_raw)
        if delay is None or not 0 <= delay <= 5:
            problems.append(f"{where}: delay 0 to 5 seconds, not {delay_raw!r}")
        if not 1 <= len(text) <= 40:
            problems.append(f"{where}: text 1 to 40 characters, this one {len(text)}")
        markers = _markers(text)
        if len(markers) >= 3:
            problems.append(f"{where}: the text has foreign words ({_named(markers)})")


def _check_cover(cover: dict[str, Any], problems: list[str]) -> None:
    title = cover.get("title") or ""
    if not 3 <= len(title) <= 60:
        problems.append(f"portada: Título 3 to 60 characters, this one {len(title)}")
    label = cover.get("label") or ""
    if len(label) > 40:
        problems.append(f"portada: Rótulo up to 40 characters, this one {len(label)}")


def validate(markdown: str, carpeta: str) -> list[str]:
    """Every problem with the pack, as plain English, one string each. Empty when it is good."""
    text = str(markdown or "")
    pack = parse(text)
    lang = pack["lang"]
    problems: list[str] = []
    if lang not in VOICE_LINE:
        problems.append("Idioma: es or en")
    _check_carpeta(carpeta, lang, problems)
    _check_article(pack["article_url"], lang, problems)
    _check_sections(pack["sections"], problems)
    _check_clips(pack, problems)
    for clip in pack["clips"]:
        if clip["prompt"] is not None:
            _check_prompt(clip, lang, problems)
    if "Rótulos" in pack["sections"]:
        _check_overlays({clip["index"] for clip in pack["clips"]}, text, problems)
    if "Portada" in pack["sections"]:
        _check_cover(pack["cover"], problems)
    markers = _markers(OUTSIDE.sub(" ", text))
    if len(markers) >= 3:
        problems.append(f"language: the prose outside the clip prompts has foreign words ({_named(markers)})")
    return problems


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _root() -> Path:
    from plugins.shorts.ledger import shorts_root

    return shorts_root() / "flow" / "packs"


def _index_path() -> Path:
    return _root() / "index.json"


def _lessons_dir() -> Path:
    return _root() / "lessons"


def _load_index() -> dict[str, Any]:
    return series._load(_index_path())


def _save_index(index: dict[str, Any]) -> None:
    series._save(index, _index_path())


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _cap(name: str, default: int) -> int:
    try:
        return max(0, int(env(name, str(default)) or str(default)))
    except ValueError:
        return default


def _langs() -> list[str]:
    raw = env("FLOW_PACK_LANGS", "es,en")
    langs = [part.strip() for part in raw.split(",") if part.strip() in VOICE_LINE]
    return langs or ["es", "en"]


def _norm_url(url: Any) -> str:
    """Lowercase, no query, no fragment, no trailing slash: two posts' one identity."""
    text = str(url or "").strip().lower()
    return text.split("#", 1)[0].split("?", 1)[0].rstrip("/")


def _age_days(stamp: Any) -> float:
    try:
        written = calendar.timegm(time.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return float("inf")
    return (time.time() - written) / 86400


def _waiting(index: dict[str, Any]) -> list[str]:
    """Packs the owner has not filmed yet: in review, or written and still without footage."""
    waiting = [carpeta for carpeta, entry in index.items() if entry.get("state") == "review"]
    for carpeta, entry in index.items():
        if entry.get("state") != "written" or _age_days(entry.get("written_at")) > 3:
            continue
        files = flow_inbox._list_files(carpeta)
        if not any(str(f.get("Name") or "").lower().endswith(".mp4") for f in files):
            waiting.append(carpeta)
    return waiting


def _next_lang(index: dict[str, Any]) -> str:
    """The language whose last pack is oldest; never used counts as the oldest."""
    last: dict[str, str] = {}
    for entry in index.values():
        lang = str(entry.get("lang") or "")
        stamp = str(entry.get("written_at") or "")
        if lang in VOICE_LINE and stamp > last.get(lang, ""):
            last[lang] = stamp
    return min(_langs(), key=lambda lang: last.get(lang, ""))


def _used_urls(index: dict[str, Any]) -> set[str]:
    used = {_norm_url(entry.get("article_url")) for entry in index.values()}
    used |= {_norm_url(entry.get("article_url")) for entry in flow_inbox._load().values()}
    used |= {_norm_url(entry.get("articulo")) for entry in series._load().values()}
    used.discard("")
    return used


def _carpeta(lang: str, slug: str) -> str:
    clean = re.sub(r"[^a-z0-9-]", "-", str(slug or "").lower())
    clean = re.sub(r"-{2,}", "-", clean).strip("-")[:60].strip("-") or "short"
    return f"{datetime.now(UTC).strftime('%Y-%m-%d')}_{lang}-{clean}"


def _lessons(limit: int = 5) -> list[dict[str, str]]:
    folder = _lessons_dir()
    if not folder.is_dir():
        return []
    paths = [path for path in folder.glob("*.diff") if path.is_file()]
    paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [{"carpeta": path.stem, "diff": path.read_text(encoding="utf-8-sig").strip()[:4000]}
            for path in paths[:limit]]


# ---------------------------------------------------------------------------
# The agent's two steps
# ---------------------------------------------------------------------------

def next_pack() -> dict[str, Any]:
    """The next article to script, with its text, the fixed lines and the owner's last edits."""
    index = _load_index()
    waiting = _waiting(index)
    limit = _cap("FLOW_PACKS_WAITING_MAX", 3)
    if len(waiting) >= limit:
        return {"blocked": f"{len(waiting)} packs wait for the owner to film them: {', '.join(waiting)}"}
    lang = _next_lang(index)
    used = _used_urls(index)
    items = studio_tool.parse_feed(studio_tool._http_get(studio_tool._feeds()[lang]))
    item = next((row for row in items if _norm_url(row["url"]) not in used), None)
    if item is None:
        return {"blocked": f"every {lang} article already has a pack or a short"}
    from plugins.shorts.studio.article import fetch_html, html_to_text

    _, text = html_to_text(fetch_html(item["url"]))
    return {"lang": lang, "carpeta": _carpeta(lang, item["slug"]),
            "article": {"url": item["url"], "title": item["title"]},
            "text": text[:studio_tool.ARTICLE_CHARS],
            "voice_line": VOICE_LINE[lang], "prompt_end": PROMPT_END, "lessons": _lessons()}


def write(carpeta: str, markdown: str) -> dict[str, Any]:
    """Validate a pack and either hold it for review or write it to the owner's Drive."""
    index = _load_index()
    problems = validate(markdown, carpeta)
    if carpeta in index:
        problems.append(f"carpeta: {carpeta!r} already has a pack")
    if carpeta in flow_inbox._list_dirs():
        problems.append(f"carpeta: Flow/{carpeta} is already in Drive")
    if problems:
        raise flow_inbox.FlowError("; ".join(problems))
    path = _root() / f"{carpeta}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    pack = parse(markdown)
    released = sum(1 for entry in index.values()
                   if entry.get("released_at") or entry.get("state") == "written")
    entry = {"lang": pack["lang"], "article_url": pack["article_url"], "title": pack["title"],
             "state": "review", "written_at": _now()}
    if released < _cap("FLOW_PACK_REVIEW_FIRST", 3):
        index[carpeta] = entry
        _save_index(index)
        return {"carpeta": carpeta, "state": "review",
                "message": f"Pack {carpeta} escrito; espera revisión antes de ir a Drive."}
    flow_inbox._write_text(carpeta, "prompts.md", markdown)
    entry["state"] = "written"
    index[carpeta] = entry
    _save_index(index)
    return {"carpeta": carpeta, "state": "written",
            "message": f"📝 Pack nuevo en Drive: Flow/{carpeta}/prompts.md — "
                       f"{pack['title']} ({pack['article_url']})"}


def release(carpeta: str) -> dict[str, Any]:
    """The owner has reviewed a pack: validate the stored file again and send it to Drive."""
    index = _load_index()
    entry = index.get(carpeta)
    if entry is None:
        raise flow_inbox.FlowError(f"{carpeta} is not a pack waiting for review")
    if entry.get("state") != "review":
        raise flow_inbox.FlowError(f"{carpeta} is {entry.get('state')!r}; release needs a pack in review")
    path = _root() / f"{carpeta}.md"
    if not path.is_file():
        raise flow_inbox.FlowError(f"{carpeta}: the stored pack is missing")
    markdown = path.read_text(encoding="utf-8-sig")
    problems = validate(markdown, carpeta)
    if problems:
        raise flow_inbox.FlowError("; ".join(problems))
    flow_inbox._write_text(carpeta, "prompts.md", markdown)
    stamp = _now()
    entry.update(state="written", written_at=stamp, released_at=stamp)
    index[carpeta] = entry
    _save_index(index)
    return {"carpeta": carpeta, "state": "written"}


def record_lesson(carpeta: str, filmed: str) -> Optional[Path]:
    """Keep the owner's edits between the pack as written and the one he filmed from."""
    original_path = _root() / f"{carpeta}.md"
    if not original_path.is_file():
        return None
    original = [line.rstrip() for line in original_path.read_text(encoding="utf-8-sig").splitlines()]
    shot = [line.rstrip() for line in str(filmed or "").splitlines()]
    if original == shot:
        return None
    diff = "".join(difflib.unified_diff([line + "\n" for line in original],
                                        [line + "\n" for line in shot], "escrito", "rodado", n=1))
    path = _lessons_dir() / f"{carpeta}.diff"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(diff[:8000], encoding="utf-8")
    return path
