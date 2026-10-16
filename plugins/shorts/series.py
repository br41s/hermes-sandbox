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

Config (service env of the profile that runs the shorts jobs):

    SERIES_BUZZ_CHANNEL     UUID of the channel episodes are requested in
    SERIES_BUZZ_FROM        hex pubkeys allowed to request (comma-separated; VideoDesk)
    SERIES_YOUTUBE_PRIVACY  public | unlisted | private   (default public)
    SERIES_MAX_PER_DAY      episodes started per UTC day (default 1)
    BUZZ_RELAY_URL, BUZZ_PRIVATE_KEY, BUZZ_AUTH_TAG   as for the shorts hand-off

Quota: an episode is two uploads and two caption tracks, about 4,000 of the
10,000 daily units; the blog shorts use about 4,100. One episode a day fits.
Shadow mode (``SHORTS_PUBLISH_MODE`` not ``live``) publishes nothing.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from plugins.shorts import buzz_share
from plugins.shorts._env import env

MARK = "#publicar-episodio"
LANGS = ("es", "en")
MAX_ATTEMPTS = 3            # per episode; after that the sender is told and it is left alone
MIN_SECONDS, MAX_SECONDS = 30.0, 900.0
TIMEOUT = 600


class SeriesError(RuntimeError):
    pass


def channel() -> str:
    return env("SERIES_BUZZ_CHANNEL")


def allowed() -> List[str]:
    return [p.strip().lower() for p in env("SERIES_BUZZ_FROM").split(",") if re.fullmatch(r"[0-9a-fA-F]{64}", p.strip())]


def configured() -> bool:
    return bool(channel() and allowed() and all(env(n) for n in buzz_share._CREDENTIALS))


def _max_per_day() -> int:
    try:
        return max(0, int(env("SERIES_MAX_PER_DAY", "1") or "1"))
    except ValueError:
        return 1


def _root() -> Path:
    from plugins.shorts.ledger import shorts_root

    return shorts_root() / "series"


def _ledger_path() -> Path:
    return _root() / "ledger.json"


def _load() -> Dict[str, Any]:
    path = _ledger_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _save(data: Dict[str, Any]) -> None:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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


def parse(content: str) -> Dict[str, Any]:
    """The episode request in a message, validated. Raises SeriesError with every problem."""
    text = (content or "").strip()
    if not text.startswith(MARK):
        raise SeriesError(f"the message does not start with {MARK}")
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = fenced.group(1) if fenced else text[len(MARK):].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SeriesError(f"the JSON block does not parse: {exc}") from exc
    problems: List[str] = []
    agente = str(data.get("agente") or "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,60}", agente):
        problems.append("agente: the agent's page slug, e.g. content-gap-hunter")
    episodio = str(data.get("episodio") or "")
    if not re.fullmatch(r"\d{1,3}", episodio):
        problems.append("episodio: its number, e.g. 01")
    out = {"agente": agente, "episodio": episodio}
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
        titulo = " ".join(str(part.get("titulo") or "").split())
        if not 10 <= len(titulo) <= 100:
            problems.append(f"{lang}.titulo: 10 to 100 characters")
        descripcion = str(part.get("descripcion") or "").strip()
        if not 20 <= len(descripcion) <= 4900:
            problems.append(f"{lang}.descripcion: 20 to 4900 characters")
        etiquetas = part.get("etiquetas") or []
        if not isinstance(etiquetas, list) or not all(isinstance(t, str) and t.strip() for t in etiquetas):
            problems.append(f"{lang}.etiquetas: a list of words")
            etiquetas = []
        out[lang] = {"video": video, "srt": srt, "titulo": titulo, "descripcion": descripcion,
                     "etiquetas": [t.strip() for t in etiquetas][:15]}
    if problems:
        raise SeriesError("; ".join(problems))
    return out


# ---------------------------------------------------------------------------
# Buzz
# ---------------------------------------------------------------------------

def _buzz(args: List[str], content: Optional[str] = None) -> str:
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


def requests(limit: int = 50) -> List[Dict[str, Any]]:
    """Episode requests in the channel from allowed senders, oldest first (invalid ones carry ``error``)."""
    raw = _buzz(["messages", "get", "--channel", channel(), "--limit", str(limit)])
    try:
        events = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise SeriesError(f"buzz messages get did not return JSON: {exc}") from exc
    found = []
    senders = set(allowed())
    for ev in sorted(events if isinstance(events, list) else [], key=lambda e: e.get("created_at") or 0):
        content = str(ev.get("content") or "")
        if str(ev.get("pubkey") or "").lower() not in senders or not content.strip().startswith(MARK):
            continue
        item = {"event": ev.get("id"), "from": str(ev.get("pubkey")).lower(), "created_at": ev.get("created_at")}
        try:
            item.update(parse(content))
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


def _check_video(path: Path) -> float:
    from plugins.shorts.studio import media

    info = media.probe(path)
    video = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    if not video:
        raise SeriesError("the file has no video")
    if not any(s.get("codec_type") == "audio" for s in info.get("streams", [])):
        raise SeriesError("the episode has no audio")
    if int(video[0].get("width") or 0) <= int(video[0].get("height") or 0):
        raise SeriesError("an episode is horizontal (16:9); this video is not")
    seconds = float(info.get("format", {}).get("duration") or 0)
    if not MIN_SECONDS <= seconds <= MAX_SECONDS:
        raise SeriesError(f"an episode lasts {MIN_SECONDS:.0f}-{MAX_SECONDS:.0f} s; this one {seconds:.1f} s")
    return seconds


def _reply(item: Dict[str, Any], text: str) -> None:
    _buzz(["messages", "send", "--channel", channel(), "--reply-to", item["event"],
           "--mention", item["from"], "--content", "-"], content=text)


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def _publish_language(item: Dict[str, Any], lang: str, work: Path) -> Dict[str, Any]:
    from plugins.shorts.social import youtube

    part = item[lang]
    video = _download(part["video"], work / f"{lang}.mp4")
    seconds = _check_video(video)
    srt = _download(part["srt"], work / f"{lang}.srt") if part.get("srt") else None
    social = {"youtube": {"title": part["titulo"], "description": part["descripcion"], "tags": part["etiquetas"]}}
    result = youtube.upload(video, None, srt, social, lang, synthetic=False, short=False,
                            privacy=env("SERIES_YOUTUBE_PRIVACY", "public") or "public")
    return {**result, "seconds": round(seconds, 1), "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _summary(item: Dict[str, Any], entry: Dict[str, Any]) -> str:
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


def run() -> Dict[str, Any]:
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
    started_today = sum(1 for e in ledger.values() if e.get("started") == _today())
    report: Dict[str, Any] = {"published": [], "failed": [], "invalid": [], "waiting": []}
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
        except Exception as exc:  # noqa: BLE001 - reported, and retried on the next run
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
    return {k: v for k, v in report.items() if v} or {"nothing": "no episode to publish"}
