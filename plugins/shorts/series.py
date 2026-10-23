"""Episodes of the "Agentes BigLobster" series: long horizontal explainers, from Buzz to YouTube.

Who does what (shorts/STUDIO.md, "Who publishes where"): bespoke videos are made
in Buzz by Guionista and VideoDesk; YouTube is Hermes's. Each episode explains
one agent BigLobster rents, 2-3 minutes, 16:9, one video per language.

When an episode is finished in both languages, VideoDesk posts ONE message in
the series channel: its first line is ``#publicar-episodio`` and a JSON block
follows::

    {"episodio": "01", "agente": "content-gap-hunter",
     "es": {"video": "<relay /media/ URL>", "srt": "<relay URL or null>",
            "titulo": "...", "descripcion": "...", "etiquetas": ["..."]},
     "en": {...}}

The shorts Publisher calls ``shorts_studio`` action ``series`` on every run.
This module then reads those messages (only from the senders allowed in
``SERIES_BUZZ_FROM``), downloads both masters from the relay with the ``buzz``
CLI, uploads each as a regular YouTube video (not a Short) and replies in the
message's thread with the links, mentioning the sender. A ledger keyed by the
message id makes every step idempotent: a video is never uploaded twice, and a
run that stops halfway is finished by the next one.

Optimising a published episode: SEODesk posts ``#actualizar-video`` with a JSON block, one
entry per language it rewrites (either or both)::

    {"episodio": "01", "agente": "content-gap-hunter",
     "es": {"id": "<YouTube id>", "titulo": "...", "descripcion": "... (chapters, links, hashtags)",
            "etiquetas": ["..."], "portada": "<relay /media/ URL of a JPEG, or null>"},
     "en": {...}}

The same run applies it with ``videos.update`` (title, description, tags) and
``thumbnails.set``, and replies in the thread. Only from the allowed senders, and
only for videos this module uploaded (ids in its ledger). About 100 units each.

The blog Shorts are optimised the same way: instead of "episodio" and "agente" the
block carries ``"short": "<request_id of the Short>"`` and the one language it is in::

    {"short": "es-limite-decision-ia-pymes-2026-20261017040713",
     "es": {"id": "<YouTube id>", "titulo": "...", "descripcion": "...", "etiquetas": [...], "portada": null}}

Only for ids the shorts ledger recorded as uploaded by Hermes ("short" is the label
used in the reply; the request_id is the natural one), and for the Flow shorts below
(their "short" is the Flow folder name).

Flow shorts (from 2026-10-22 no agent renders videos: Brais shoots shorts in Google Flow
and VideoDesk finishes them) are requested with ``#publicar-short`` and ONE flat block::

    {"short": "2026-10-21_es-ia-empleados-junior", "idioma": "es",
     "articulo": "https://biglobster.top/...", "video": "<relay /media/ URL of the mp4>",
     "srt": "<relay URL or null>", "portada": "<relay URL of a JPEG/PNG <= 2 MB, or null>",
     "titulo": "...", "descripcion": "...", "etiquetas": ["..."],
     "redes": {"instagram": "...", "facebook": "...", "x": "..."}}      (redes is optional)

The same run uploads it as a YouTube Short (vertical, audio, 5-180 s, marked as
synthetic media: the scenes are AI-generated) with its captions and cover, replies in
the thread, and hands it to SocialBot in the shorts channel exactly as the blog Shorts
(``buzz_share.post``: video, cover, YouTube link, the ``redes`` texts or else the
description). They share the ledger, marked ``"kind": "short"``.

Config (service env of the profile that runs the shorts jobs):

    SERIES_BUZZ_CHANNEL     UUID of the channel episodes are requested in
    SERIES_BUZZ_FROM        hex pubkeys allowed to request (comma-separated; VideoDesk, SEODesk)
    SERIES_YOUTUBE_PRIVACY  public | unlisted | private   (default public)
    SERIES_MAX_PER_DAY      episodes started per UTC day (default 1)
    SERIES_SHORTS_MAX_PER_DAY  Flow shorts started per UTC day, apart from the episodes (default 2)
    BUZZ_RELAY_URL, BUZZ_PRIVATE_KEY, BUZZ_AUTH_TAG   as for the shorts hand-off

Quota: an episode is two uploads and two caption tracks, about 4,000 of the
10,000 daily units; the blog shorts use about 4,100 and a Flow short about 2,050
(upload 1,600, captions 400, cover 50). One episode a day fits.
Shadow mode (``SHORTS_PUBLISH_MODE`` not ``live``) publishes nothing.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from plugins.shorts import buzz_share
from plugins.shorts._env import env

MARK = "#publicar-episodio"
MARK_UPDATE = "#actualizar-video"
MARK_SHORT = "#publicar-short"
LANGS = ("es", "en")
MAX_ATTEMPTS = 3            # per episode; after that the sender is told and it is left alone
MIN_SECONDS, MAX_SECONDS = 30.0, 900.0
SHORT_MIN_SECONDS, SHORT_MAX_SECONDS = 5.0, 180.0
TIMEOUT = 600
MAX_THUMB_BYTES = 2_000_000     # YouTube's limit for a custom thumbnail


class SeriesError(RuntimeError):
    pass


def channel() -> str:
    return env("SERIES_BUZZ_CHANNEL")


def allowed() -> list[str]:
    return [p.strip().lower() for p in env("SERIES_BUZZ_FROM").split(",") if re.fullmatch(r"[0-9a-fA-F]{64}", p.strip())]


def configured() -> bool:
    return bool(channel() and allowed() and all(env(n) for n in buzz_share._CREDENTIALS))


def _cap(name: str, default: int) -> int:
    try:
        return max(0, int(env(name, str(default)) or str(default)))
    except ValueError:
        return default


def _max_per_day() -> int:
    return _cap("SERIES_MAX_PER_DAY", 1)


def _max_shorts_per_day() -> int:
    return _cap("SERIES_SHORTS_MAX_PER_DAY", 2)


def _root() -> Path:
    from plugins.shorts.ledger import shorts_root

    return shorts_root() / "series"


def _ledger_path() -> Path:
    return _root() / "ledger.json"


def _updates_path() -> Path:
    return _root() / "updates.json"


def _load(path: Optional[Path] = None) -> dict[str, Any]:
    path = path or _ledger_path()
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}


def _save(data: dict[str, Any], path: Optional[Path] = None) -> None:
    path = path or _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# The request message
# ---------------------------------------------------------------------------

def _relay_origin() -> str:
    url = env("BUZZ_RELAY_URL")
    url = re.sub(r"^wss://", "https://", re.sub(r"^ws://", "http://", url))
    m = re.match(r"^(https?://[^/]+)", url)
    return m.group(1) if m else ""


def _media_url(value: Any, what: str) -> str:
    url = str(value or "").strip()
    origin = _relay_origin()
    if not origin or not url.startswith(origin + "/media/"):
        raise SeriesError(f"{what}: not a media URL of this relay ({origin}/media/...)")
    return url


def _json_block(content: str, mark: str) -> dict[str, Any]:
    text = (content or "").strip()
    if not text.startswith(mark):
        raise SeriesError(f"the message does not start with {mark}")
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    raw = fenced.group(1) if fenced else text[len(mark):].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SeriesError(f"the JSON block does not parse: {exc}") from exc
    if not isinstance(data, dict):
        raise SeriesError("the JSON block is not an object")
    return data


def _metadata(part: dict[str, Any], lang: str, problems: list[str]) -> dict[str, Any]:
    """Title, description and tags of one language (``lang`` empty for a flat block), as YouTube accepts them."""
    at = f"{lang}." if lang else ""
    titulo = " ".join(str(part.get("titulo") or "").split())
    if not 10 <= len(titulo) <= 100:
        problems.append(f"{at}titulo: 10 to 100 characters")
    descripcion = str(part.get("descripcion") or "").strip()
    if not 20 <= len(descripcion) <= 4900:
        problems.append(f"{at}descripcion: 20 to 4900 characters")
    if any(c in titulo + descripcion for c in "<>"):
        problems.append(f"{lang + ': ' if lang else ''}YouTube rejects < and > in the title and description")
    etiquetas = part.get("etiquetas") or []
    if not isinstance(etiquetas, list) or not all(isinstance(t, str) and t.strip() for t in etiquetas):
        problems.append(f"{at}etiquetas: a list of words")
        etiquetas = []
    return {"titulo": titulo, "descripcion": descripcion, "etiquetas": [t.strip() for t in etiquetas][:15]}


SHORT_ID = r"[a-z0-9][a-z0-9_-]{4,120}"


def _short_ids() -> dict[str, str]:
    """request_id -> YouTube id of every blog Short already on YouTube (shorts ledger)."""
    from plugins.shorts import ledger

    return {e["request_id"]: e["published"]["youtube"]["id"] for e in ledger.entries()
            if (((e.get("published") or {}).get("youtube") or {}).get("id")) and e.get("request_id")}


def _episode(data: dict[str, Any], problems: list[str]) -> dict[str, Any]:
    agente = str(data.get("agente") or "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,60}", agente):
        problems.append("agente: the agent's page slug, e.g. content-gap-hunter")
    episodio = str(data.get("episodio") or "")
    if not re.fullmatch(r"\d{1,3}", episodio):
        problems.append("episodio: its number, e.g. 01")
    return {"agente": agente, "episodio": episodio}


def parse(content: str) -> dict[str, Any]:
    """The episode request in a message, validated. Raises SeriesError with every problem."""
    data = _json_block(content, MARK)
    problems: list[str] = []
    out = _episode(data, problems)
    for lang in LANGS:
        part = data.get(lang) or {}
        try:
            video = _media_url(part.get("video"), f"{lang}.video")
        except SeriesError as exc:
            problems.append(str(exc))
            video = ""
        srt = None
        if part.get("srt"):
            try:
                srt = _media_url(part.get("srt"), f"{lang}.srt")
            except SeriesError as exc:
                problems.append(str(exc))
        out[lang] = {"video": video, "srt": srt, **_metadata(part, lang, problems)}
    if problems:
        raise SeriesError("; ".join(problems))
    return out


def _article_url(value: Any, problems: list[str]) -> str:
    url = str(value or "").strip()
    try:
        parts = urlparse(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        parts, host = urlparse(""), ""
    if parts.scheme != "https" or parts.username or not (host == "biglobster.top" or host.endswith(".biglobster.top")):
        problems.append("articulo: an https URL on biglobster.top")
    return url


def parse_short(content: str) -> dict[str, Any]:
    """A Flow short request, validated. Raises SeriesError with every problem."""
    data = _json_block(content, MARK_SHORT)
    problems: list[str] = []
    short = str(data.get("short") or "")
    if not re.fullmatch(SHORT_ID, short):
        problems.append("short: the Flow folder name, e.g. 2026-10-21_es-ia-empleados-junior")
    idioma = str(data.get("idioma") or "")
    if idioma not in LANGS:
        problems.append("idioma: es or en")
    out: dict[str, Any] = {"short": short, "idioma": idioma, "articulo": _article_url(data.get("articulo"), problems)}
    for field in ("video", "srt", "portada"):
        out[field] = None
        if field == "video" or data.get(field):
            try:
                out[field] = _media_url(data.get(field), field)
            except SeriesError as exc:
                problems.append(str(exc))
    redes = data.get("redes") or {}
    if not isinstance(redes, dict) or not all(isinstance(redes.get(n) or "", str) for n in ("instagram", "facebook", "x")):
        problems.append("redes: instagram, facebook and x, each a text")
        redes = {}
    out["redes"] = {n: str(redes.get(n) or "").strip() for n in ("instagram", "facebook", "x")}
    out.update(_metadata(data, "", problems))
    if problems:
        raise SeriesError("; ".join(problems))
    return out


def parse_update(content: str) -> dict[str, Any]:
    """An optimisation request (new metadata and thumbnail for published videos), validated."""
    data = _json_block(content, MARK_UPDATE)
    problems: list[str] = []
    if data.get("short"):
        out: dict[str, Any] = {"short": str(data["short"]), "agente": "", "episodio": ""}
        if not re.fullmatch(SHORT_ID, out["short"]):
            problems.append("short: the Short's id (a blog request_id, e.g. es-limite-decision-ia-pymes-2026-20261017040713, "
                            "or a Flow folder name, e.g. 2026-10-21_es-ia-empleados-junior)")
    else:
        out = _episode(data, problems)
    out["langs"] = [lang for lang in LANGS if data.get(lang)]
    if not out["langs"]:
        problems.append("at least one language (es, en)")
    for lang in out["langs"]:
        part = data[lang] if isinstance(data[lang], dict) else {}
        video_id = str(part.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            problems.append(f"{lang}.id: the YouTube video id (11 characters)")
        portada = None
        if part.get("portada"):
            try:
                portada = _media_url(part.get("portada"), f"{lang}.portada")
            except SeriesError as exc:
                problems.append(str(exc))
        out[lang] = {"id": video_id, "portada": portada, **_metadata(part, lang, problems)}
    if problems:
        raise SeriesError("; ".join(problems))
    return out


# ---------------------------------------------------------------------------
# Buzz
# ---------------------------------------------------------------------------

def _buzz(args: list[str], content: Optional[str] = None) -> str:
    binary = buzz_share.cli()
    if not binary:
        raise SeriesError("the buzz CLI is not installed in this image")
    try:
        done = subprocess.run([binary, *args], input=content, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=TIMEOUT, env=buzz_share._child_env(), check=False)
    except subprocess.TimeoutExpired as exc:
        raise SeriesError(f"buzz {args[0]} {args[1]} timed out after {TIMEOUT}s") from exc
    if done.returncode != 0:
        tail = " / ".join((done.stderr or done.stdout or "").strip().splitlines()[-4:])
        raise SeriesError(f"buzz {args[0]} {args[1]} failed ({done.returncode}): {tail[:400]}")
    return done.stdout or ""


def requests(limit: int = 200, mark: str = MARK) -> list[dict[str, Any]]:
    """Requests with ``mark`` in the channel from allowed senders, oldest first (invalid ones carry ``error``)."""
    parser = {MARK_UPDATE: parse_update, MARK_SHORT: parse_short}.get(mark, parse)
    raw = _buzz(["messages", "get", "--channel", channel(), "--limit", str(limit)])
    try:
        events = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise SeriesError(f"buzz messages get did not return JSON: {exc}") from exc
    found = []
    senders = set(allowed())
    for ev in sorted(events if isinstance(events, list) else [], key=lambda e: e.get("created_at") or 0):
        content = str(ev.get("content") or "")
        if str(ev.get("pubkey") or "").lower() not in senders or not content.strip().startswith(mark):
            continue
        item = {"event": ev.get("id"), "from": str(ev.get("pubkey")).lower(), "created_at": ev.get("created_at")}
        try:
            item.update(parser(content))
        except SeriesError as exc:
            item["error"] = str(exc)
        found.append(item)
    return found


def _download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _buzz(["media", "get", url, "-o", str(dest)])
    if not dest.is_file() or dest.stat().st_size == 0:
        raise SeriesError(f"downloaded nothing from {url}")
    return dest


def _probe(path: Path, what: str) -> tuple:
    """(width, height, seconds) of a video that has audio."""
    from plugins.shorts.studio import media

    info = media.probe(path)
    video = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    if not video:
        raise SeriesError("the file has no video")
    if not any(s.get("codec_type") == "audio" for s in info.get("streams", [])):
        raise SeriesError(f"the {what} has no audio")
    return (int(video[0].get("width") or 0), int(video[0].get("height") or 0),
            float(info.get("format", {}).get("duration") or 0))


def _check_video(path: Path) -> float:
    width, height, seconds = _probe(path, "episode")
    if width <= height:
        raise SeriesError("an episode is horizontal (16:9); this video is not")
    if not MIN_SECONDS <= seconds <= MAX_SECONDS:
        raise SeriesError(f"an episode lasts {MIN_SECONDS:.0f}-{MAX_SECONDS:.0f} s; this one {seconds:.1f} s")
    return seconds


def _check_short(path: Path) -> float:
    width, height, seconds = _probe(path, "short")
    if height <= width:
        raise SeriesError("a short is vertical (9:16); this video is not")
    if not SHORT_MIN_SECONDS <= seconds <= SHORT_MAX_SECONDS:
        raise SeriesError(f"a short lasts {SHORT_MIN_SECONDS:.0f}-{SHORT_MAX_SECONDS:.0f} s; this one {seconds:.1f} s")
    return seconds


def _check_thumb(path: Path) -> None:
    head = path.read_bytes()[:8]
    if not (head.startswith(b"\xff\xd8") or head.startswith(b"\x89PNG")):
        raise SeriesError("the cover is not a JPEG or PNG")
    if path.stat().st_size > MAX_THUMB_BYTES:
        raise SeriesError(f"the cover weighs {path.stat().st_size // 1000} KB; YouTube takes up to 2 MB")


def _reply(item: dict[str, Any], text: str) -> None:
    _buzz(["messages", "send", "--channel", channel(), "--reply-to", item["event"],
           "--mention", item["from"], "--content", "-"], content=text)


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def _publish_language(item: dict[str, Any], lang: str, work: Path) -> dict[str, Any]:
    from plugins.shorts.social import youtube

    part = item[lang]
    video = _download(part["video"], work / f"{lang}.mp4")
    seconds = _check_video(video)
    srt = _download(part["srt"], work / f"{lang}.srt") if part.get("srt") else None
    social = {"youtube": {"title": part["titulo"], "description": part["descripcion"], "tags": part["etiquetas"]}}
    result = youtube.upload(video, None, srt, social, lang, synthetic=False, short=False,
                            privacy=env("SERIES_YOUTUBE_PRIVACY", "public") or "public")
    return {**result, "seconds": round(seconds, 1), "at": datetime.now(UTC).isoformat(timespec="seconds")}


def _summary(item: dict[str, Any], entry: dict[str, Any]) -> str:
    lines = [f"Episodio {item['episodio']} ({item['agente']}) publicado en YouTube por Hermes:"]
    for lang in LANGS:
        done = entry.get(lang) or {}
        lines.append(f"{lang.upper()}: {done.get('url')} (id {done.get('id')}, {done.get('privacy') or '?'})")
    warnings = [w for lang in LANGS for w in (entry.get(lang) or {}).get("warnings") or []]
    if warnings:
        lines.append("Avisos: " + " · ".join(warnings))
    lines.append("Para la web: estos ids van en site/_data/videosAgentes.json de biglobster "
                 f"(\"{item['agente']}\": es / en).")
    return "\n".join(lines)


def _update_language(item: dict[str, Any], lang: str, work: Path) -> dict[str, Any]:
    from plugins.shorts.social import youtube

    part = item[lang]
    thumb = _download(part["portada"], work / f"{lang}-portada") if part.get("portada") else None
    if thumb:
        _check_thumb(thumb)
    social = {"youtube": {"title": part["titulo"], "description": part["descripcion"], "tags": part["etiquetas"]}}
    result = youtube.update(part["id"], social, lang, thumb, short=bool(item.get("short")))
    return {**result, "at": datetime.now(UTC).isoformat(timespec="seconds")}


def _save_entry(ledger: dict[str, Any], key: str, entry: dict[str, Any]) -> None:
    ledger[key] = entry
    _save(ledger)


def _hand_off(item: dict[str, Any], video: Path, cover: Optional[Path], youtube_url: str) -> list[str]:
    """Post the Short to SocialBot in the shorts channel, as the blog Shorts are. Returns the attachments."""
    files = {"master": str(video)}
    if cover and cover.read_bytes()[:2] == b"\xff\xd8":      # the relay refuses a PNG
        files["cover"] = str(cover)
    entry = {"lang": item["idioma"], "title": item["titulo"], "request_id": item["short"],
             "article_url": item["articulo"], "synthetic": True}
    social = {net: text or item["descripcion"] for net, text in item["redes"].items()}
    try:
        return buzz_share.post(entry, files, social, youtube_url)["attached"]
    except buzz_share.BuzzError as exc:
        raise SeriesError(f"could not hand the short to SocialBot: {exc}") from exc


def _publish_short(item: dict[str, Any], entry: dict[str, Any], work: Path, ledger: dict[str, Any], key: str) -> None:
    """Upload the Short and hand it to SocialBot; each step is recorded, so a retry does only what is left."""
    from plugins.shorts.social import youtube

    video = _download(item["video"], work / "master.mp4")
    cover = _download(item["portada"], work / "cover.jpg") if item.get("portada") else None
    if not entry.get("youtube"):
        seconds = _check_short(video)
        if cover:
            _check_thumb(cover)
        srt = _download(item["srt"], work / "short.srt") if item.get("srt") else None
        social = {"youtube": {"title": item["titulo"], "description": item["descripcion"], "tags": item["etiquetas"]}}
        article = {"url": item["articulo"], "slug": item["short"]}
        result = youtube.upload(video, cover, srt, social, item["idioma"], synthetic=True, short=True, article=article,
                                privacy=env("SERIES_YOUTUBE_PRIVACY", "public") or "public")
        entry["youtube"] = {**result, "seconds": round(seconds, 1),
                            "at": datetime.now(UTC).isoformat(timespec="seconds")}
        _save_entry(ledger, key, entry)
    if not entry.get("handoff_at") and buzz_share.configured():
        entry["handoff"] = _hand_off(item, video, cover, entry["youtube"]["url"])
        entry["handoff_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        _save_entry(ledger, key, entry)


def _short_summary(item: dict[str, Any], entry: dict[str, Any]) -> str:
    done = entry["youtube"]
    lines = [f"Short {item['short']} publicado en YouTube por Hermes:",
             f"{done.get('url')} (id {done.get('id')}, {done.get('privacy') or '?'})"]
    if done.get("warnings"):
        lines.append("Avisos: " + " · ".join(done["warnings"]))
    if entry.get("handoff_at"):
        lines.append(f"Entregado a SocialBot en el canal de shorts ({', '.join(entry['handoff'])}) "
                     "para Facebook, Instagram y X.")
        if item.get("portada") and "cover.jpg" not in entry["handoff"]:
            lines.append("La portada no va adjunta: el relé de Buzz sólo acepta JPEG.")
    else:
        lines.append("No se ha entregado a SocialBot: falta SHORTS_BUZZ_CHANNEL en Hermes.")
    return "\n".join(lines)


def _publish_shorts(ledger: dict[str, Any], report: dict[str, Any]) -> None:
    """Publish the pending Flow shorts this run may take, under their own daily cap."""
    shorts = {k: e for k, e in ledger.items() if e.get("kind") == "short"}
    started_today = sum(1 for e in shorts.values() if e.get("started") == _today())
    for item in requests(mark=MARK_SHORT):
        key = item["event"]
        entry = ledger.get(key) or {}
        if entry.get("replied"):
            continue
        problem = item.get("error")
        if not problem and not entry.get("started"):
            twin = next((e for k, e in shorts.items() if k != key and e.get("short") == item["short"]
                         and (e.get("youtube") or {}).get("id")), None)
            if twin:
                problem = f"{item['short']} is already on YouTube: {twin['youtube']['url']}"
        if problem:
            _reply(item, f"No puedo publicar este short: {problem}. Corrige el mensaje y vuelve a enviarlo.")
            _save_entry(ledger, key, {"kind": "short", "rejected": True, "replied": True, "error": problem})
            report["invalid"].append({"event": key, "error": problem})
            continue
        if not entry.get("started"):
            if started_today >= _max_shorts_per_day():
                report["waiting"].append({"short": item["short"],
                                          "why": f"{_max_shorts_per_day()} short(s) a day (YouTube quota)"})
                continue
            entry = {"kind": "short", "short": item["short"], "idioma": item["idioma"], "started": _today(),
                     "attempts": 0}
            started_today += 1
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        _save_entry(ledger, key, entry)
        shorts[key] = entry
        work = _root() / key[:16]
        try:
            _publish_short(item, entry, work, ledger, key)
        except Exception as exc:
            entry["error"] = str(exc)[:400]
            if entry["attempts"] >= MAX_ATTEMPTS:
                where = (f"El short {item['short']} está en YouTube ({entry['youtube']['url']}) pero no he podido "
                         "entregarlo a SocialBot" if entry.get("youtube") else
                         f"No he podido publicar el short {item['short']}")
                _reply(item, f"{where} tras {MAX_ATTEMPTS} intentos: {entry['error']}. Necesita que alguien lo mire.")
                entry["replied"] = True
            _save_entry(ledger, key, entry)
            report["failed"].append({"short": item["short"], "error": entry["error"], "attempt": entry["attempts"]})
            continue
        finally:
            shutil.rmtree(work, ignore_errors=True)
        _reply(item, _short_summary(item, entry))
        entry["replied"] = True
        entry.pop("error", None)
        _save_entry(ledger, key, entry)
        report["published"].append({"short": item["short"], "handoff": bool(entry.get("handoff_at")),
                                    **{k: entry["youtube"].get(k) for k in ("id", "url", "privacy")}})


def _what(item: dict[str, Any]) -> str:
    return f"el short {item['short']}" if item.get("short") else f"el episodio {item['episodio']}"


def _apply_updates(ledger: dict[str, Any], report: dict[str, Any]) -> None:
    """Apply the optimisation requests, each once; only to videos this module published."""
    ours = {(e.get(lang) or {}).get("id") for e in ledger.values() for lang in LANGS} - {None}
    shorts = set(_short_ids().values()) | {e["youtube"]["id"] for e in ledger.values()
                                           if e.get("kind") == "short" and (e.get("youtube") or {}).get("id")}
    updates = _load(_updates_path())
    for item in requests(mark=MARK_UPDATE):
        key = item["event"]
        entry = updates.get(key) or {}
        if entry.get("replied"):
            continue
        if item.get("short"):
            problem = item.get("error") or next(
                (f"{lang}.id {item[lang]['id']} is not a Short uploaded by Hermes"
                 for lang in item["langs"] if item[lang]["id"] not in shorts), None)
        else:
            problem = item.get("error") or next(
                (f"{lang}.id {item[lang]['id']} is not a video of the series" for lang in item["langs"]
                 if item[lang]["id"] not in ours), None)
        if problem:
            _reply(item, f"No puedo actualizar este vídeo: {problem}. Corrige el mensaje y vuelve a enviarlo.")
            updates[key] = {"rejected": True, "replied": True, "error": problem}
            _save(updates, _updates_path())
            report["invalid"].append({"event": key, "error": problem})
            continue
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        updates[key] = entry
        _save(updates, _updates_path())
        work = _root() / f"u-{key[:16]}"
        try:
            for lang in item["langs"]:
                if not (entry.get(lang) or {}).get("id"):
                    entry[lang] = _update_language(item, lang, work)
                    _save(updates, _updates_path())
        except Exception as exc:
            entry["error"] = str(exc)[:400]
            if entry["attempts"] >= MAX_ATTEMPTS:
                _reply(item, f"No he podido actualizar {_what(item)} tras {MAX_ATTEMPTS} intentos: "
                             f"{entry['error']}. Necesita que alguien lo mire.")
                entry["replied"] = True
            _save(updates, _updates_path())
            report["failed"].append({"episodio": item["episodio"], "agente": item["agente"], "short": item.get("short"),
                                     "update": True,
                                     "error": entry["error"], "attempt": entry["attempts"]})
            continue
        finally:
            shutil.rmtree(work, ignore_errors=True)
        lines = [f"Short {item['short']} actualizado en YouTube por Hermes:" if item.get("short") else
                 f"Episodio {item['episodio']} ({item['agente']}) actualizado en YouTube por Hermes:"]
        for lang in item["langs"]:
            done = entry[lang]
            lines.append(f"{lang.upper()}: {done['url']} · título, descripción y etiquetas"
                         + (" · portada" if done.get("thumbnail") else ""))
        warnings = [w for lang in item["langs"] for w in entry[lang].get("warnings") or []]
        if warnings:
            lines.append("Avisos: " + " · ".join(warnings))
        _reply(item, "\n".join(lines))
        entry["replied"] = True
        entry.pop("error", None)
        _save(updates, _updates_path())
        report["updated"].append({"short": item["short"], "langs": item["langs"]} if item.get("short") else
                                 {"episodio": item["episodio"], "agente": item["agente"], "langs": item["langs"]})


def run() -> dict[str, Any]:
    """Publish the pending episodes this run may take. Returns what happened, for the Publisher's report."""
    if not configured():
        return {"skipped": "series not configured (SERIES_BUZZ_CHANNEL, SERIES_BUZZ_FROM, Buzz credentials)"}
    from plugins.shorts.social import youtube
    from plugins.shorts.studio_tool import publish_mode

    if publish_mode() != "live":
        return {"skipped": "shadow mode: nothing is published"}
    if not youtube.configured():
        return {"skipped": "YouTube is not configured"}
    ledger = _load()
    started_today = sum(1 for e in ledger.values() if e.get("kind") != "short" and e.get("started") == _today())
    report: dict[str, Any] = {"published": [], "updated": [], "failed": [], "invalid": [], "waiting": []}
    for item in requests():
        key = item["event"]
        entry = ledger.get(key) or {}
        if entry.get("replied"):
            continue
        if item.get("error"):
            if not entry.get("rejected"):
                _reply(item, f"No puedo publicar este episodio: {item['error']}. Corrige el mensaje y vuelve a enviarlo.")
                ledger[key] = {"rejected": True, "replied": True, "error": item["error"]}
                _save(ledger)
            report["invalid"].append({"event": key, "error": item["error"]})
            continue
        if not entry.get("started"):
            if started_today >= _max_per_day():
                report["waiting"].append({"episodio": item["episodio"], "agente": item["agente"],
                                          "why": f"{_max_per_day()} episode(s) a day (YouTube quota)"})
                continue
            entry = {"agente": item["agente"], "episodio": item["episodio"], "started": _today(), "attempts": 0}
            started_today += 1
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        ledger[key] = entry
        _save(ledger)
        work = _root() / key[:16]
        try:
            for lang in LANGS:
                if not (entry.get(lang) or {}).get("id"):
                    entry[lang] = _publish_language(item, lang, work)
                    ledger[key] = entry
                    _save(ledger)
        except Exception as exc:
            entry["error"] = str(exc)[:400]
            if entry["attempts"] >= MAX_ATTEMPTS:
                _reply(item, f"No he podido publicar el episodio {item['episodio']} tras {MAX_ATTEMPTS} intentos: "
                             f"{entry['error']}. Necesita que alguien lo mire.")
                entry["replied"] = True
            ledger[key] = entry
            _save(ledger)
            report["failed"].append({"episodio": item["episodio"], "agente": item["agente"], "error": entry["error"],
                                     "attempt": entry["attempts"]})
            continue
        finally:
            shutil.rmtree(work, ignore_errors=True)
        _reply(item, _summary(item, entry))
        entry["replied"] = True
        entry.pop("error", None)
        ledger[key] = entry
        _save(ledger)
        report["published"].append({"episodio": item["episodio"], "agente": item["agente"],
                                    **{lang: {k: entry[lang].get(k) for k in ("id", "url", "privacy")} for lang in LANGS}})
    _publish_shorts(ledger, report)
    _apply_updates(ledger, report)
    return {k: v for k, v in report.items() if v} or {"nothing": "no episode to publish"}
