"""Publishing helpers for the Flow shorts: probe a vertical master, upload it to YouTube as a Short,
post it as a Facebook and Instagram Reel when `META_PUBLISH_MODE=live`, and summarise the result.
The shared ledger lives in `series/ledger.json` (Flow packs read it to skip articles that already
have a short). The Buzz request path (`#publicar-episodio`, `#publicar-short`, `#actualizar-video`)
was removed on 2026-11-22 with the Buzz fleet.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from plugins.shorts._env import env

SHORT_MIN_SECONDS, SHORT_MAX_SECONDS = 5.0, 180.0
MAX_THUMB_BYTES = 2_000_000     # YouTube's limit for a custom thumbnail


class SeriesError(RuntimeError):
    pass


class SeriesPending(SeriesError):
    """Instagram is still processing the reel: the next run resumes it, no attempt used."""


def _cap(name: str, default: int) -> int:
    try:
        return max(0, int(env(name, str(default)) or str(default)))
    except ValueError:
        return default


def _root() -> Path:
    from plugins.shorts.ledger import shorts_root

    return shorts_root() / "series"


def _ledger_path() -> Path:
    return _root() / "ledger.json"


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
# The request's fields
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Probing and publishing
# ---------------------------------------------------------------------------

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


def _publish_files(item: dict[str, Any], entry: dict[str, Any], video: Path, cover: Optional[Path],
                   srt: Optional[Path], save: Callable[[], None], report: dict[str, Any]) -> None:
    """Upload the Short to YouTube, then Facebook and Instagram; each step is recorded, so a retry does only what is left."""
    from plugins.shorts.social import meta, youtube

    if not entry.get("youtube"):
        seconds = _check_short(video)
        if cover:
            _check_thumb(cover)
        social = {"youtube": {"title": item["titulo"], "description": item["descripcion"], "tags": item["etiquetas"]}}
        result = youtube.upload(video, cover, srt, social, item["idioma"], synthetic=True, short=True,
                                privacy=env("SERIES_YOUTUBE_PRIVACY", "public") or "public")
        entry["youtube"] = {**result, "seconds": round(seconds, 1),
                            "at": datetime.now(UTC).isoformat(timespec="seconds")}
        save()
    meta_live = meta.publish_mode() == "live"
    text_fb = item["redes"].get("facebook") or item["redes"].get("instagram") or item["descripcion"]
    text_ig = item["redes"].get("instagram") or item["descripcion"]
    if meta_live and meta.configured() and not (entry.get("facebook") or {}).get("id"):
        if entry["youtube"]["seconds"] <= 90:
            entry["facebook"] = {**meta.facebook_reel(video, text_fb, ai_generated=True),
                                 "at": datetime.now(UTC).isoformat(timespec="seconds")}
            save()
        else:
            warnings = entry["youtube"].setdefault("warnings", [])
            warning = "Facebook: dura más de 90 s, Reels no lo admite; súbelo a mano"
            if warning not in warnings:
                warnings.append(warning)
                save()
    if meta_live and meta.configured(instagram=True) and not (
            (entry.get("instagram") or {}).get("id") or (entry.get("instagram") or {}).get("failed")):
        try:
            result = meta.instagram(video, kind="reel", caption=text_ig, cover=cover,
                                    pending_container=(entry.get("instagram") or {}).get("pending"),
                                    ai_generated=True)
        except meta.MetaError as exc:
            entry["instagram"] = {"failed": str(exc)[:300], "at": datetime.now(UTC).isoformat(timespec="seconds")}
            warnings = entry["youtube"].setdefault("warnings", [])
            warning = "Instagram: Meta rechazó la subida del vídeo; súbelo a mano"
            if warning not in warnings:
                warnings.append(warning)
            save()
        else:
            if result.get("pending"):
                entry["instagram"] = {"pending": result["pending"]}
                save()
                raise SeriesPending("Instagram is still processing the reel")
            entry["instagram"] = {**result, "at": datetime.now(UTC).isoformat(timespec="seconds")}
            save()
    if meta.configured() and not meta_live:
        report["meta_shadow"].append({"short": item["short"], "facebook": text_fb[:120],
                                      "instagram": text_ig[:120]})


def _short_summary(item: dict[str, Any], entry: dict[str, Any]) -> str:
    done = entry["youtube"]
    lines = [f"Short {item['short']} publicado en YouTube por Hermes:",
             f"{done.get('url')} (id {done.get('id')}, {done.get('privacy') or '?'})"]
    for net, label in (("facebook", "Facebook"), ("instagram", "Instagram")):
        if (entry.get(net) or {}).get("url"):
            lines.append(f"{label}: {entry[net]['url']}")
    if done.get("warnings"):
        lines.append("Avisos: " + " · ".join(done["warnings"]))
    return "\n".join(lines)
