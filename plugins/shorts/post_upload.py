"""Post-upload YouTube writes that the Buzz bots cannot make, requested with ``#actualizar-video``.

The bots have no YouTube write access (Studio is read-only for them), so SEODesk asks Hermes. The
format and the rules (allowed senders, ledger, quota) are documented in ``plugins/shorts/series.py``;
this module holds what is specific to the new keys: validating them, the quota budget, and one step
function per key. A step is recorded in the request's ledger entry the moment it succeeds, so a retry
does only what is left, and the videos and comments Hermes already touched are remembered across
requests in ``series/posts.json``.

Not possible through the API, so never attempted: pinning a comment (``comentario`` is the text to
pin; the sender pins it by hand), a Short's "related video" field, end screens.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta, UTC, datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from plugins.shorts import series
from plugins.shorts._env import env
from plugins.shorts.social import youtube

logger = logging.getLogger(__name__)

# YouTube Data API units (a read is 1; videos.update, playlists/playlistItems/comments inserts 50;
# captions.list 50; captions.insert 400).
READ, WRITE, CAPTION_LIST, CAPTION_INSERT = 1, 50, 50, 400
UPLOAD_UNITS = 2_050                # one video: upload 1,600 + captions 400 + cover 50
DAILY_UNITS = 10_000
RESERVE_UNITS = 2_100               # always left for one more Flow short upload
META_KEYS = ("titulo", "descripcion", "etiquetas")
EXTRA_KEYS = ("localizaciones", "subtitulos", "lista", "comentario")
MAX_LOCALES, MAX_TRACKS = 20, 6
COMMENT_MAX, REPLY_MAX = 1000, 500
MAX_SRT_BYTES = 1_000_000
LOCALE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
COMMENT_ID = re.compile(r"^[A-Za-z0-9_-]{10,100}$")
BARE_DOMAIN = re.compile(r"(?i)^[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:[/:?#].*)?$")
SCHEME = re.compile(r"(?i)^(?:[a-z][a-z0-9+.-]*://|www\.|(?:mailto|javascript|data|tel|ftp|file):)")
TRIM = ".,;:!?¡¿()[]{}\"'«»“”‘’*"


def _cap() -> int:
    return series._cap("SERIES_REPLIES_MAX_PER_RUN", 10)


# ---------------------------------------------------------------------------
# Validation: the text of a request is data from another bot
# ---------------------------------------------------------------------------

def _link_problem(text: str) -> str:
    """Why ``text`` may not be posted: it links anywhere but biglobster.top, or carries an e-mail address."""
    for raw in text.split():
        token = raw.strip(TRIM)
        if re.search(r"(?i)@[^@\s]+\.[a-z]{2,}", token):
            return f"{token}: no e-mail addresses"
        if not (SCHEME.match(token) or BARE_DOMAIN.match(token)):
            continue
        try:
            parts = urlparse(token if "://" in token else f"https://{token}")
            host = (parts.hostname or "").lower()
            port = parts.port
        except ValueError:
            return f"{token}: only https links on biglobster.top"
        if (parts.scheme != "https" or parts.username or port or
                not (host == "biglobster.top" or host.endswith(".biglobster.top"))):
            return f"{token}: only https links on biglobster.top"
    return ""


def _text(value: Any, what: str, limit: int, problems: list[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{what}: a text")
        return ""
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").strip().split("\n")))
    if len(text) > limit:
        problems.append(f"{what}: up to {limit} characters")
    elif any(ord(c) < 32 and c != "\n" for c in text):
        problems.append(f"{what}: control characters")
    elif problem := _link_problem(text):
        problems.append(f"{what}: {problem}")
    return text


def _locales(value: Any, at: str, problems: list[str]) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict) or not 1 <= len(value) <= MAX_LOCALES:
        problems.append(f'{at}localizaciones: 1 to {MAX_LOCALES} languages, {{"id": {{"titulo": "...", "descripcion": "..."}}}}')
        return {}
    out = {}
    for code, loc in value.items():
        loc = loc if isinstance(loc, dict) else {}
        titulo = " ".join(str(loc.get("titulo") or "").split())
        descripcion = str(loc.get("descripcion") or "").strip()
        where = f"{at}localizaciones.{code}"
        if not LOCALE.match(str(code)):
            problems.append(f"{where}: not a language code")
        if not 1 <= len(titulo) <= 100:
            problems.append(f"{where}.titulo: 1 to 100 characters")
        if not 1 <= len(descripcion) <= 4900:
            problems.append(f"{where}.descripcion: 1 to 4900 characters")
        if any(c in titulo + descripcion for c in "<>"):
            problems.append(f"{where}: YouTube rejects < and > in the title and description")
        out[str(code)] = {"title": titulo, "description": descripcion}
    return out


def _tracks(value: Any, at: str, problems: list[str]) -> dict[str, str]:
    if not isinstance(value, dict) or not 1 <= len(value) <= MAX_TRACKS:
        problems.append(f'{at}subtitulos: 1 to {MAX_TRACKS} languages, {{"id": "<relay /media/ URL of an .srt>"}}')
        return {}
    out = {}
    for code, url in value.items():
        if not LOCALE.match(str(code)):
            problems.append(f"{at}subtitulos.{code}: not a language code")
        try:
            out[str(code)] = series._media_url(url, f"{at}subtitulos.{code}")
        except series.SeriesError as exc:
            problems.append(str(exc))
            continue
        if not out[str(code)].lower().endswith(".srt"):
            problems.append(f"{at}subtitulos.{code}: an .srt file")
    return out


def _replies(value: Any, at: str, problems: list[str]) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= _cap():
        problems.append(f'{at}respuestas: a list of 1 to {_cap()} {{"comentario_id", "texto"}}')
        return []
    out = []
    for n, reply in enumerate(value, 1):
        reply = reply if isinstance(reply, dict) else {}
        cid = str(reply.get("comentario_id") or "")
        if not COMMENT_ID.match(cid):
            problems.append(f"{at}respuestas[{n}].comentario_id: the id of a top-level comment")
        out.append({"comentario_id": cid, "texto": _text(reply.get("texto"), f"{at}respuestas[{n}].texto", REPLY_MAX, problems)})
    return out


def _read(part: dict[str, Any], at: str, problems: list[str]) -> dict[str, Any]:
    """The post-upload keys present in ``part`` (a language entry, or the request), validated."""
    out: dict[str, Any] = {}
    if part.get("localizaciones"):
        out["localizaciones"] = _locales(part["localizaciones"], at, problems)
    if part.get("subtitulos"):
        out["subtitulos"] = _tracks(part["subtitulos"], at, problems)
    if part.get("lista"):
        title = " ".join(str(part["lista"]).split())
        if not 3 <= len(title) <= 100 or any(c in title for c in "<>"):
            problems.append(f"{at}lista: a playlist title, 3 to 100 characters, no < or >")
        out["lista"] = title
    if part.get("comentario"):
        out["comentario"] = _text(part["comentario"], f"{at}comentario", COMMENT_MAX, problems)
    if part.get("respuestas"):
        out["respuestas"] = _replies(part["respuestas"], at, problems)
    return out


def parse(data: dict[str, Any], langs: list[str], problems: list[str]) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """(keys per language, replies of the request). A language's own key wins over the request's."""
    top = _read(data, "", problems)
    replies = list(top.pop("respuestas", []))
    extras = {}
    for lang in langs:
        own = _read(data[lang], f"{lang}.", problems) if isinstance(data[lang], dict) else {}
        replies += own.pop("respuestas", [])
        extras[lang] = {**top, **own}
    ids = [r["comentario_id"] for r in replies]
    if len(set(ids)) != len(ids):
        problems.append("respuestas: a comment is answered once per request")
    if len(replies) > _cap():
        problems.append(f"respuestas: at most {_cap()} per request")
    return extras, replies


def has_extras(item: dict[str, Any]) -> bool:
    return bool(item.get("respuestas")) or any(item[lang].get(k) for lang in item["langs"] for k in EXTRA_KEYS)


# ---------------------------------------------------------------------------
# Quota: a day's budget, refusing what would starve the uploads
# ---------------------------------------------------------------------------

def _quota_day() -> str:
    """YouTube resets at midnight Pacific; UTC-8 never runs ahead of it, so the count errs high."""
    return (datetime.now(UTC) - timedelta(hours=8)).strftime("%Y-%m-%d")


def _quota_path() -> Path:
    return series._root() / "quota.json"


def _posts_path() -> Path:
    return series._root() / "posts.json"


def spent_today() -> int:
    """Units this module spent today (updates and everything here); uploads are estimated from the ledgers."""
    data = series._load(_quota_path())
    return int(data.get("spent") or 0) if data.get("day") == _quota_day() else 0


def spend(units: int) -> None:
    series._save({"day": _quota_day(), "spent": spent_today() + units}, _quota_path())


def uploads_today(ledger: dict[str, Any]) -> int:
    """Units today's uploads cost, worst case (every video with captions and a cover)."""
    from plugins.shorts import ledger as shorts_ledger

    days = {series._today(), _quota_day()}
    units = sum(UPLOAD_UNITS * (1 if e.get("kind") == "short" else len(series.LANGS))
                for e in ledger.values() if e.get("started") in days)
    blog = sum(1 for e in shorts_ledger.entries()
               if (((e.get("published") or {}).get("youtube") or {}).get("at") or "")[:10] in days)
    return units + blog * UPLOAD_UNITS


def pending_replies(item: dict[str, Any], entry: dict[str, Any]) -> int:
    answered = (entry.get("extras") or {}).get("respuestas") or {}
    return sum(1 for r in item.get("respuestas") or [] if r["comentario_id"] not in answered)


def cost(item: dict[str, Any], entry: dict[str, Any]) -> int:
    """Worst-case units the rest of this request needs. Steps already recorded cost nothing."""
    extras = entry.get("extras") or {}
    total = 0
    for lang in item["langs"]:
        part, mine = item[lang], extras.get(lang) or {}
        loc = part.get("localizaciones")
        if not (entry.get(lang) or {}).get("id"):
            if "titulo" in part:
                total += WRITE + (WRITE if part.get("portada") else 0) + (READ if loc else 0)
            elif loc:
                total += READ + WRITE
        todo = [c for c in part.get("subtitulos") or {} if c not in (mine.get("subtitulos") or {})]
        if todo:
            total += CAPTION_LIST + CAPTION_INSERT * len(todo)
        if part.get("lista") and not mine.get("lista"):
            total += 2 * READ + 2 * WRITE
        if part.get("comentario") and not mine.get("comentario"):
            total += 2 * READ + WRITE
    replies = pending_replies(item, entry)
    return total + (replies * (2 * READ + WRITE) + READ if replies else 0)


class Run:
    """What one pass over the requests shares: the quota, the reply cap and the cross-request memory."""

    def __init__(self, ledger: dict[str, Any], videos: set[str]):
        self.ledger, self.videos = ledger, videos
        self.replies_cap = _cap()
        self.replies_left = self.replies_cap
        self.quota_out = False
        self.posts = series._load(_posts_path())
        self.channel: Optional[str] = None

    def remember(self, kind: str, key: str, data: dict[str, Any]) -> None:
        self.posts.setdefault(kind, {})[key] = {**data, "at": datetime.now(UTC).isoformat(timespec="seconds")}
        series._save(self.posts, _posts_path())

    def finish(self, report: dict[str, Any]) -> None:
        used = self.replies_cap - self.replies_left
        if used:
            logger.info("post-upload: %d of %d replies posted this run", used, self.replies_cap)
            report["replies"] = used


def gate(item: dict[str, Any], entry: dict[str, Any], run: Run) -> Optional[tuple[str, str]]:
    """None to go ahead, ("reject", why) when it can never fit, ("wait", why) when it does not fit today."""
    if not has_extras(item):
        return None
    needs, room = cost(item, entry), DAILY_UNITS - RESERVE_UNITS
    if needs > room:
        return "reject", f"costaría ~{needs} unidades de YouTube y una petición no puede pasar de {room}"
    if run.quota_out:
        return "wait", "YouTube ya ha rechazado una llamada hoy por cuota"
    used = uploads_today(run.ledger) + spent_today()
    if used + needs > room:
        return "wait", (f"cuesta ~{needs} unidades y hoy ya van ~{used} de {DAILY_UNITS} "
                        f"(se reservan {RESERVE_UNITS} para subidas)")
    replies = pending_replies(item, entry)
    if replies > run.replies_left:
        return "wait", f"{replies} respuestas y esta ejecución sólo admite {run.replies_left} más (máximo {run.replies_cap})"
    return None


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------

class Skip(Exception):
    """A step that is not done on purpose, and must not be retried."""


def _call(api, units: int, method: str, resource: str, params: Optional[dict] = None, body: Optional[dict] = None) -> dict:
    out = api.call(method, resource, params, body)
    spend(units)
    return out


def _norm(title: str) -> str:
    return " ".join(title.split()).casefold()


def _author(snippet: dict[str, Any]) -> str:
    return str((snippet.get("authorChannelId") or {}).get("value") or "")


def _me(api, run: Run) -> str:
    if run.channel is None:
        items = _call(api, READ, "GET", "channels", {"part": "id", "mine": "true"}).get("items") or []
        if not items:
            raise youtube.YouTubeError("channels.list: the token has no channel")
        run.channel = str(items[0]["id"])
    return run.channel


def _check_srt(path: Path) -> None:
    raw = path.read_bytes()
    if len(raw) > MAX_SRT_BYTES:
        raise series.SeriesError(f"the subtitles weigh {len(raw) // 1000} KB; the limit is {MAX_SRT_BYTES // 1000} KB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise series.SeriesError("the subtitles are not UTF-8") from exc
    if "-->" not in text or "\x00" in text:
        raise series.SeriesError("the file is not an .srt")


def _captions(api, run: Run, part: dict[str, Any], mine: dict[str, Any], work: Path, save: Callable[[], None]) -> None:
    tracks = mine.setdefault("subtitulos", {})
    todo = {c: u for c, u in part["subtitulos"].items() if c not in tracks}
    if not todo:
        return
    listed = _call(api, CAPTION_LIST, "GET", "captions", {"part": "snippet", "videoId": part["id"]})
    have = {str((c.get("snippet") or {}).get("language") or "").lower() for c in listed.get("items") or []
            if (c.get("snippet") or {}).get("trackKind") != "ASR"}
    for code, url in todo.items():
        if code.lower() in have:
            tracks[code] = {"skipped": "ya tiene pista"}
        else:
            srt = series._download(url, work / f"{part['id']}-{code}.srt")
            _check_srt(srt)
            api.insert_caption(part["id"], code, srt.read_bytes())
            spend(CAPTION_INSERT)
            tracks[code] = {"applied": True}
        save()


def _find_playlist(api, title: str) -> Optional[str]:
    token = None
    for _ in range(10):
        page = _call(api, READ, "GET", "playlists",
                     {"part": "snippet", "mine": "true", "maxResults": 50, **({"pageToken": token} if token else {})})
        for playlist in page.get("items") or []:
            if _norm(playlist["snippet"]["title"]) == _norm(title):
                return str(playlist["id"])
        token = page.get("nextPageToken")
        if not token:
            break
    return None


def _playlist(api, run: Run, part: dict[str, Any], mine: dict[str, Any], work: Path, save: Callable[[], None]) -> None:
    if mine.get("lista"):
        return
    title, vid = part["lista"], part["id"]
    remembered = (run.posts.get("listas") or {}).get(_norm(title)) or {}
    pid = mine.get("lista_id") or _find_playlist(api, title) or remembered.get("id")
    if not pid:
        pid = str(_call(api, WRITE, "POST", "playlists", {"part": "snippet,status"},
                        {"snippet": {"title": title},
                         "status": {"privacyStatus": env("SERIES_YOUTUBE_PRIVACY", "public") or "public"}})["id"])
        mine["lista_created"] = True
        run.remember("listas", _norm(title), {"id": pid})
    mine["lista_id"] = pid
    save()                                  # a retry must not create the playlist again
    inside = _call(api, READ, "GET", "playlistItems",
                   {"part": "id", "playlistId": pid, "videoId": vid, "maxResults": 1}).get("items")
    if inside:
        mine["lista"] = {"id": pid, "skipped": "ya estaba"}
    else:
        _call(api, WRITE, "POST", "playlistItems", {"part": "snippet"},
              {"snippet": {"playlistId": pid, "resourceId": {"kind": "youtube#video", "videoId": vid}}})
        mine["lista"] = {"id": pid, "added": True, "created": bool(mine.get("lista_created"))}
    save()


def _comment(api, run: Run, part: dict[str, Any], mine: dict[str, Any], work: Path, save: Callable[[], None]) -> None:
    if mine.get("comentario"):
        return
    vid = part["id"]
    if vid in (run.posts.get("comentarios") or {}):
        mine["comentario"] = {"skipped": "Hermes ya comentó este vídeo"}
        return save()
    me = _me(api, run)
    threads = _call(api, READ, "GET", "commentThreads",
                    {"part": "snippet", "videoId": vid, "maxResults": 100, "order": "time", "textFormat": "plainText"})
    if any(_author(((t.get("snippet") or {}).get("topLevelComment") or {}).get("snippet") or {}) == me
           for t in threads.get("items") or []):
        run.remember("comentarios", vid, {"found": True})
        mine["comentario"] = {"skipped": "el canal ya tiene un comentario en este vídeo"}
        return save()
    posted = _call(api, WRITE, "POST", "commentThreads", {"part": "snippet"},
                   {"snippet": {"videoId": vid, "topLevelComment": {"snippet": {"textOriginal": part["comentario"]}}}})
    run.remember("comentarios", vid, {"thread": posted.get("id")})
    mine["comentario"] = {"thread": posted.get("id")}
    save()


def _reply(api, run: Run, comment_id: str, text: str) -> dict[str, Any]:
    if comment_id in (run.posts.get("respuestas") or {}):
        raise Skip("ya respondido")
    me = _me(api, run)
    got = _call(api, READ, "GET", "comments", {"part": "snippet", "id": comment_id, "textFormat": "plainText"}).get("items") or []
    if not got:
        raise Skip("no encuentro ese comentario")
    snippet = got[0].get("snippet") or {}
    if snippet.get("parentId"):
        raise Skip("es una respuesta, no un comentario principal")
    if snippet.get("videoId") not in run.videos:
        raise Skip("no es de un vídeo subido por Hermes")
    if _author(snippet) == me:
        raise Skip("es del propio canal")
    answers = _call(api, READ, "GET", "comments",
                    {"part": "snippet", "parentId": comment_id, "maxResults": 100, "textFormat": "plainText"}).get("items") or []
    if any(_author(a.get("snippet") or {}) == me for a in answers):
        run.remember("respuestas", comment_id, {"found": True})
        raise Skip("el canal ya respondió")
    posted = _call(api, WRITE, "POST", "comments", {"part": "snippet"},
                   {"snippet": {"parentId": comment_id, "textOriginal": text}})
    run.remember("respuestas", comment_id, {"reply": posted.get("id")})
    run.replies_left -= 1
    logger.info("post-upload: reply %d of %d posted on comment %s", run.replies_cap - run.replies_left, run.replies_cap, comment_id)
    return {"reply": posted.get("id")}


def note_failure(run: Run, exc: Exception) -> None:
    """A quota refusal ends this run's YouTube writes: later requests wait instead of burning attempts."""
    if "quotaExceeded" in str(exc):
        run.quota_out = True


def apply(item: dict[str, Any], entry: dict[str, Any], run: Run, work: Path, save: Callable[[], None]) -> list[str]:
    """Run the captions, playlist, comment and replies still to do. Returns the errors, one per failed step."""
    extras = entry.setdefault("extras", {})
    steps = (("subtitulos", _captions), ("lista", _playlist), ("comentario", _comment))
    errors: list[str] = []
    todo = [(lang, name, fn) for lang in item["langs"] for name, fn in steps
            if item[lang].get(name) and not (extras.get(lang) or {}).get(name)]
    if not todo and not pending_replies(item, entry):
        return errors
    with youtube.api() as api:
        for lang, name, fn in todo:
            if run.quota_out:
                break
            try:
                fn(api, run, item[lang], extras.setdefault(lang, {}), work, save)
            except Exception as exc:
                errors.append(f"{lang}.{name}: {str(exc)[:200]}")
                note_failure(run, exc)
        answered = extras.setdefault("respuestas", {})
        for reply in item.get("respuestas") or []:
            cid = reply["comentario_id"]
            if cid in answered or run.quota_out:
                continue
            if run.replies_left <= 0:
                errors.append(f"respuestas: the cap of {run.replies_cap} per run is spent; {cid} waits")
                continue
            try:
                answered[cid] = _reply(api, run, cid, reply["texto"])
            except Skip as skip:
                answered[cid] = {"skipped": str(skip)}
            except Exception as exc:
                errors.append(f"respuestas {cid}: {str(exc)[:200]}")
                note_failure(run, exc)
                continue
            save()
    return errors


# ---------------------------------------------------------------------------
# What the reply says
# ---------------------------------------------------------------------------

def summary(item: dict[str, Any], entry: dict[str, Any], lang: str) -> list[str]:
    """What was applied to ``lang``'s video apart from its title, description, tags and cover."""
    part = item[lang]
    mine = (entry.get("extras") or {}).get(lang) or {}
    bits = []
    done = entry.get(lang) or {}
    if done.get("localized"):
        bits.append("localizaciones (" + ", ".join(done["localized"]) + ")")
    tracks = mine.get("subtitulos") or {}
    new = [c for c, r in tracks.items() if r.get("applied")]
    had = [c for c, r in tracks.items() if r.get("skipped")]
    if new:
        bits.append("subtítulos (" + ", ".join(new) + ")")
    if had:
        bits.append("subtítulos que ya existían, sin tocar (" + ", ".join(had) + ")")
    lista = mine.get("lista")
    if lista:
        bits.append(f"lista «{part['lista']}»: " + ("ya estaba" if lista.get("skipped") else
                                                   "creada y vídeo añadido" if lista.get("created") else "vídeo añadido"))
    comentario = mine.get("comentario")
    if comentario:
        bits.append(f"comentario no publicado: {comentario['skipped']}" if comentario.get("skipped") else
                    "comentario publicado; fíjalo tú en Studio (la API no puede fijar)")
    return bits


def replies_summary(item: dict[str, Any], entry: dict[str, Any]) -> list[str]:
    answered = (entry.get("extras") or {}).get("respuestas") or {}
    if not answered:
        return []
    posted = sum(1 for r in answered.values() if r.get("reply"))
    skipped = [f"{cid} ({r['skipped']})" for cid, r in answered.items() if r.get("skipped")]
    return [f"Respuestas: {posted} publicadas" + (f"; omitidas: {', '.join(skipped)}" if skipped else "")]
