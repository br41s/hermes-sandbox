"""The Flow inbox: the owner's Google Drive folders, from footage to published Short.

The owner drops ``Flow/<carpeta>/`` in his Drive with the Google Flow export
(one ``.mp4``) and the script pack (``prompts.md``). Hermes downloads it, sends
it ENCRYPTED to the GitHub Actions renderer (format ``flow``), writes the
finished Short back to ``Flow/<carpeta>/final/`` for review, and publishes it
only after the owner writes ``publica <carpeta>`` in Telegram.

States::

    new ──> rendering ──> rendered ──> ready ──> publishing ──> published
     ^          │                       │           │
     └──────────┘ (attempt failed)      │           └──> failed (terminal)
                                        └── failed (terminal)

Each step is its own run: no run waits on a render (the job runs hourly), and a
run that stops halfway is finished by the next one. Nothing publishes on the
agent's word: the ``publica`` message is read from the gateway's own message
store by :mod:`plugins.shorts.approval`.

Config (service env of the profile that runs the Flow job):

    FLOW_DRIVE_REMOTE     rclone remote + folder            (default hermesdrive:Flow)
    FLOW_RCLONE_CONFIG    rclone.conf                       (default $HERMES_HOME/.config/rclone/rclone.conf)
    FLOW_ASSET_KEY        Fernet key shared with the renderer (required to render)
    FLOW_MAX_PER_DAY      Shorts published per UTC day       (default 2; YouTube quota)
    TELEGRAM_HOME_CHANNEL the chat the ``publica`` must come from
"""

from __future__ import annotations

import calendar
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Optional

from cryptography.fernet import Fernet

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl; the pod is Linux
    fcntl = None

from plugins.shorts import approval, github_studio, series
from plugins.shorts._env import env

logger = logging.getLogger(__name__)

# Flow/<carpeta>: the date, the language and the post's slug. The language is
# the one the folder's Short is in.
CARPETA = re.compile(r"^\d{4}-\d{2}-\d{2}_(es|en)-[a-z0-9][a-z0-9-]{2,100}$")
REQUEST_ID = re.compile(r"[a-z0-9][a-z0-9-]{4,90}")
MIN_FOOTAGE_S = 8.0                 # studio/flow.py FOOTAGE_MIN_S
MAX_FOOTAGE_S = 60.0
MAX_ATTEMPTS = 3
MAX_PUBLISH_ATTEMPTS = 3
RENDER_TIMEOUT_S = 3600
NETWORKS = ("youtube", "facebook", "instagram")
REDES = ("instagram", "facebook", "x")


class FlowError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def drive_remote() -> str:
    return env("FLOW_DRIVE_REMOTE", "hermesdrive:Flow")


def rclone_config() -> str:
    default = str(Path(os.environ.get("HERMES_HOME", "/opt/data")) / ".config" / "rclone" / "rclone.conf")
    return env("FLOW_RCLONE_CONFIG", default)


def asset_key() -> str:
    return env("FLOW_ASSET_KEY")


def max_per_day() -> int:
    try:
        return max(0, int(env("FLOW_MAX_PER_DAY", "2") or "2"))
    except ValueError:
        return 2


def telegram_home_channel() -> str:
    return env("TELEGRAM_HOME_CHANNEL")


def configured() -> bool:
    return bool(asset_key()) and github_studio.configured() and bool(shutil.which("rclone"))


def _root() -> Path:
    from plugins.shorts.ledger import shorts_root

    return shorts_root() / "flow"


def _ledger_path() -> Path:
    return _root() / "ledger.json"


def _load() -> dict[str, Any]:
    return series._load(_ledger_path())


def _save(ledger: dict[str, Any]) -> None:
    series._save(ledger, _ledger_path())


@contextlib.contextmanager
def _single_runner():
    """One Flow step at a time, across processes.

    The hourly job and the chat agent both call the tool: on 2026-11-16 the owner's
    ``publica`` woke the chat agent, both runs read ``ready`` and the Short went to
    Facebook twice. The loser is told to come back, never left waiting.
    """
    if fcntl is None:
        yield
        return
    path = _root() / ".lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FlowError("another Flow run is working on the folders; try again in a few minutes") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Drive (rclone)
# ---------------------------------------------------------------------------

def _rclone(args: list[str]) -> str:
    cmd = ["rclone", "--config", rclone_config(), *args]
    try:
        done = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FlowError(f"rclone {args[0]} failed: {exc}") from exc
    if done.returncode != 0:
        tail = (done.stderr or "").strip()[-300:]
        raise FlowError(f"rclone {args[0]} failed ({done.returncode}): {tail}")
    return done.stdout or ""


def _json(out: str, what: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(out or "[]")
    except json.JSONDecodeError as exc:
        raise FlowError(f"rclone {what} did not return JSON: {exc}") from exc
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def _list_dirs() -> list[str]:
    rows = _json(_rclone(["lsjson", "--dirs-only", drive_remote()]), "lsjson")
    return [str(row.get("Name") or "") for row in rows if row.get("Name")]


def _list_files(carpeta: str) -> list[dict[str, Any]]:
    rows = _json(_rclone(["lsjson", "--max-depth", "1", f"{drive_remote()}/{carpeta}"]), "lsjson")
    return [{"Name": str(row.get("Name") or ""), "Size": row.get("Size") or 0}
            for row in rows if row.get("Name") and not row.get("IsDir")]


def _download(carpeta: str, name: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _rclone(["copyto", f"{drive_remote()}/{carpeta}/{name}", str(dest)])


def _read_text(carpeta: str, path: str) -> Optional[str]:
    """One small file's text; None when it is not there (a folder may still be uploading)."""
    cmd = ["rclone", "--config", rclone_config(), "cat", f"{drive_remote()}/{carpeta}/{path}"]
    try:
        done = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("rclone cat %s/%s: %s", carpeta, path, exc)
        return None
    return done.stdout if done.returncode == 0 else None


def _upload(src: Path, carpeta: str, path: str) -> None:
    _rclone(["copyto", str(src), f"{drive_remote()}/{carpeta}/{path}"])


def _write_text(carpeta: str, path: str, text: str) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as handle:
        handle.write(text)
        tmp = Path(handle.name)
    try:
        _upload(tmp, carpeta, path)
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# advance: move every folder one step forward
# ---------------------------------------------------------------------------

def _discover(ledger: dict[str, Any], report: dict[str, Any]) -> None:
    """Take in Drive folders the ledger has never seen."""
    try:
        names = _list_dirs()
    except FlowError as exc:
        report["errors"].append(f"Drive: {exc}")
        return
    for name in names:
        match = CARPETA.match(name)
        if match is None or name in ledger:
            continue
        try:
            files = _list_files(name)
        except FlowError as exc:
            report["errors"].append(f"{name}: {exc}")
            continue
        mp4s = [f for f in files if str(f.get("Name") or "").lower().endswith(".mp4")]
        if not mp4s:
            continue                                # still uploading: try again next run
        if len(mp4s) > 1:
            report["errors"].append(f"{name}: more than one .mp4, leave only the one to publish")
            continue                                # not in the ledger: re-checked next run
        entry = {"carpeta": name, "lang": match.group(1), "state": "new", "attempts": 0,
                 "created_at": _now()}
        dest = _root() / name / "footage.mp4"
        try:
            _download(name, str(mp4s[0]["Name"]), dest)
        except FlowError as exc:
            report["errors"].append(f"{name}: {exc}")
            continue                                # a failed download is retried, not failed forever
        try:
            seconds = series._check_short(dest)
            if seconds < MIN_FOOTAGE_S:     # the renderer's floor: footage + outro must clear QA's 12 s
                raise FlowError(f"footage lasts {seconds:.1f} s; a Flow short is {MIN_FOOTAGE_S:.0f} s at least")
            if seconds > MAX_FOOTAGE_S:
                raise FlowError(f"footage lasts {seconds:.1f} s; a Flow short is {MAX_FOOTAGE_S:.0f} s at most")
        except (FlowError, series.SeriesError) as exc:
            entry.update(state="failed", error=str(exc)[:400])
            ledger[name] = entry
            _save(ledger)
            shutil.rmtree(_root() / name, ignore_errors=True)
            report["failed"].append({"carpeta": name, "error": entry["error"]})
            continue
        pack_text = _read_text(name, "prompts.md") or ""
        entry.update(seconds=round(float(seconds), 1), pack=pack_text)
        ledger[name] = entry
        _save(ledger)
        try:
            # The owner edits the pack in Drive while he films: what he changed is the lesson.
            from plugins.shorts import flow_packs

            flow_packs.record_lesson(name, pack_text)
        except Exception:
            logger.exception("flow: could not record the owner's edits for %s", name)


def _drop_asset(entry: dict[str, Any]) -> None:
    """The renderer has the run's input: the encrypted asset is no longer needed."""
    asset_id = entry.pop("asset_id", None)
    if asset_id is None:
        return
    try:
        github_studio.delete_flow_input(int(asset_id))
    except github_studio.StudioError as exc:      # a 404 is fine inside delete_flow_input
        logger.warning("flow: could not delete release asset %s: %s", asset_id, exc)


def _render_failed(carpeta: str, entry: dict[str, Any], reason: str,
                   ledger: dict[str, Any], report: dict[str, Any]) -> None:
    entry["attempts"] = int(entry.get("attempts") or 0) + 1
    entry["error"] = str(reason)[:400]
    for field in ("request_id", "run_id", "asset_id", "dispatched_at"):
        entry.pop(field, None)
    if entry["attempts"] >= MAX_ATTEMPTS:
        entry["state"] = "failed"
        shutil.rmtree(_root() / carpeta, ignore_errors=True)
        report["failed"].append({"carpeta": carpeta, "error": entry["error"]})
    else:
        entry["state"] = "new"                      # the agent sees the reason in needs_cover
    ledger[carpeta] = entry
    _save(ledger)


def _collect(carpeta: str, entry: dict[str, Any], ledger: dict[str, Any], report: dict[str, Any]) -> None:
    """Follow the render run (as studio_tool._collect does) and bring the files back to Drive."""
    if entry.get("run_id"):
        run = github_studio.get_run(int(entry["run_id"]))
    else:
        run = github_studio.find_run(str(entry.get("request_id") or ""))
    if run is None:
        stamp = str(entry.get("dispatched_at") or entry.get("created_at") or "")
        age = time.time() - calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
        if age > RENDER_TIMEOUT_S:
            _drop_asset(entry)                      # no run will ever read it: never leave the blob behind
            _render_failed(carpeta, entry, "no render run found an hour after dispatch", ledger, report)
        return
    entry["run_id"] = run["id"]
    if run.get("status") != "completed":
        ledger[carpeta] = entry
        _save(ledger)
        return
    out = _root() / carpeta / "out"
    if out.exists():
        shutil.rmtree(out)
    got = github_studio.download_artifact(run["id"], f"short-{entry['request_id']}", out)
    _drop_asset(entry)                              # the input is no longer needed, whatever came back
    manifest_path = out / "manifest.json"
    if not got or not manifest_path.exists():
        _render_failed(carpeta, entry, f"render {run.get('conclusion')}: no deliverables; "
                                       f"see {run.get('html_url')}", ledger, report)
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    qa_report = manifest.get("qa") or {}
    if not qa_report.get("passed"):
        errors = "; ".join(str(e) for e in qa_report.get("errors") or [])
        _render_failed(carpeta, entry, "QA: " + (errors or f"see {run.get('html_url')}"), ledger, report)
        return
    wanted = {"master.mp4": "final/short.mp4", "cover.jpg": "final/portada.jpg",
              "captions.srt": "final/captions.srt", "transcript.txt": "final/transcripcion.txt"}
    missing = [name for name in wanted if not (out / name).is_file()]
    if missing:
        _render_failed(carpeta, entry, f"render {run.get('conclusion')}: missing {', '.join(missing)}; "
                                       f"see {run.get('html_url')}", ledger, report)
        return
    for name, dest in wanted.items():
        _upload(out / name, carpeta, dest)
    transcript = (out / "transcript.txt").read_text(encoding="utf-8-sig", errors="replace")
    entry.update(state="rendered", manifest_flags=manifest.get("flags") or {},
                 footage_s=manifest.get("footage_s"),
                 # findings in his own footage pass QA as warnings (studio/flow.py): he must still see them
                 footage_warnings=[w for w in (qa_report.get("warnings") or []) if str(w).startswith("footage: ")],
                 transcript=transcript[:6000], duration_s=manifest.get("duration_s"))
    entry.pop("error", None)
    ledger[carpeta] = entry
    _save(ledger)


def _validate_texts(texts: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The YouTube metadata plus one line per network, validated; raises nothing, lists every problem."""
    problems: list[str] = []
    data = texts if isinstance(texts, dict) else {}
    meta = series._metadata(data, "", problems)
    raw = data.get("redes")
    if not isinstance(raw, dict) or not all(isinstance(raw.get(net), str) and raw.get(net).strip() for net in REDES):
        problems.append("redes: instagram, facebook and x, each a text")
        raw = {}
    redes = {net: str((raw or {}).get(net) or "").strip() for net in REDES}
    for net, value in redes.items():
        if "\n" in value or "\r" in value:
            # VideoDesk learned this the hard way: a caption with a line break
            # is cut at the break by both Meta and X.
            problems.append(f"redes.{net}: one line, no line breaks")
    return {**meta, "redes": redes}, problems


def _norm(text: str) -> str:
    """Lowercase, no accents, every non-alphanumeric run as one space: compare what is said."""
    decomposed = unicodedata.normalize("NFKD", str(text or "").lower())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", plain).strip()


def _unheard(phrase: str, transcript: str) -> bool:
    """The phrase is not in the transcript (accents and case ignored); empty says nothing."""
    normalized = _norm(phrase)
    return bool(normalized) and normalized not in _norm(transcript)


def _preview(carpeta: str, entry: dict[str, Any]) -> str:
    """The review message the agent pastes as-is: what to listen for, the texts, and how to approve."""
    flags = entry.get("manifest_flags") or {}
    texts = entry.get("texts") or {}
    redes = texts.get("redes") or {}
    lines = [
        f"🎬 Short de Flow listo para revisar: {carpeta} ({entry.get('duration_s')} s)",
        f"Drive: Flow/{carpeta}/final/ — vídeo, portada, subtítulos y textos.json (puedes corregir los textos ahí antes de aprobar).",
    ]
    if flags.get("late_start"):
        lines.append(f"⚠️ La primera palabra se oye en {_seconds(flags.get('first_word_s'))} s (objetivo: antes de 0,5 s).")
    if flags.get("over_30s"):
        lines.append(f"⚠️ El vídeo dura {_seconds(entry.get('footage_s'))} s (más de 30).")
    if flags.get("no_speech"):
        lines.append("⚠️ No se ha oído ninguna palabra: el vídeo sale sin subtítulos.")
    doubtful = flags.get("doubtful") or []
    if doubtful:
        words = ", ".join(f"«{d.get('word')}» a los {_seconds(d.get('at_s'))} s" for d in doubtful[:3])
        lines.append("⚠️ Palabras dudosas (revisa los subtítulos): " + words + (", …" if len(doubtful) > 3 else ""))
    for warning in entry.get("footage_warnings") or []:
        lines.append(f"⚠️ En tu vídeo: {warning[len('footage: '):]}")
    cover = entry.get("cover") or {}
    for text in (cover.get("title"), cover.get("kicker")):
        if text and _unheard(str(text), entry.get("transcript") or ""):
            lines.append(f"⚠️ La portada dice «{text}» y no lo oigo tal cual en el vídeo: "
                         "si no vale, cambia final/portada.jpg en Drive antes de aprobar.")
    out = _root() / carpeta / "out"
    lines += [
        "", f"YouTube — {texts.get('titulo', '')}", texts.get("descripcion", ""),
        "", "Instagram:", redes.get("instagram", ""),
        "", "Facebook:", redes.get("facebook", ""),
        "", "X (lo publicas tú):", redes.get("x", ""), "",
        f"Para publicarlo, escribe en este hilo: publica {carpeta}",
        f"MEDIA:{out / 'master.mp4'}",
        f"MEDIA:{out / 'cover.jpg'}",
    ]
    return "\n".join(lines)


def _seconds(value: Any) -> str:
    try:
        text = f"{float(value):.1f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "?"
    return text.replace(".", ",")


def _approve(carpeta: str, entry: dict[str, Any], ledger: dict[str, Any], report: dict[str, Any]) -> None:
    """Re-read the owner's texts, then look for his ``publica``; fall through to publishing when it is there."""
    raw = _read_text(carpeta, "final/textos.json")
    problems: list[str] = []
    texts: dict[str, Any] = {}
    if raw is None:
        problems.append("final/textos.json is missing")
    else:
        try:
            data = json.loads(raw)
        except ValueError as exc:
            problems.append(f"final/textos.json does not parse: {exc}")
        else:
            texts, problems = _validate_texts(data)
    if problems:
        # Never the stored texts: the owner may have edited the file since.
        entry["error"] = "textos.json: " + "; ".join(problems)
        report["errors"].append(f"{carpeta}: {entry['error']}")
        ledger[carpeta] = entry
        _save(ledger)
        return
    entry["texts"] = texts
    hit = approval.find(approval.phrase("publica", carpeta), chat_id=telegram_home_channel(),
                        after=float(entry.get("ready_at") or 0))
    if hit is None:
        report["waiting_approval"].append({"carpeta": carpeta, "since": entry.get("ready_at")})
        return
    entry["approval"] = hit
    entry["state"] = "publishing"
    entry.pop("error", None)
    ledger[carpeta] = entry
    _save(ledger)
    _publish(carpeta, entry, ledger, report, texts=texts)


def _publish(carpeta: str, entry: dict[str, Any], ledger: dict[str, Any], report: dict[str, Any],
             texts: Optional[dict[str, Any]] = None) -> None:
    """Hand the collected files to series._publish_files, under the publish mode and the Flow daily cap."""
    from plugins.shorts.social import youtube
    from plugins.shorts.studio_tool import publish_mode

    if publish_mode() != "live":
        report["waiting"].append({"carpeta": carpeta, "why": "shadow mode: nothing is published"})
        return
    if not youtube.configured():
        report["waiting"].append({"carpeta": carpeta, "why": "YouTube is not configured"})
        return
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    published_today = sum(1 for e in ledger.values()
                          if str((e.get("youtube") or {}).get("at") or "").startswith(today))
    if published_today >= max_per_day() and not entry.get("youtube"):
        report["waiting"].append({"carpeta": carpeta,
                                  "why": f"{max_per_day()} Flow short(s) a day (YouTube quota)"})
        return
    texts = texts or entry.get("texts") or {}
    if not texts:
        entry["error"] = "the texts are missing; run flow again to revalidate final/textos.json"
        report["errors"].append(f"{carpeta}: {entry['error']}")
        ledger[carpeta] = entry
        _save(ledger)
        return
    item = {"short": carpeta, "idioma": entry.get("lang"), "articulo": entry.get("article_url"), **texts}
    publish = _root() / carpeta / "publish"
    publish.mkdir(parents=True, exist_ok=True)
    video = publish / "short.mp4"
    cover_path = publish / "portada.jpg"
    srt_path = publish / "captions.srt"
    try:
        # The owner reviews Flow/<carpeta>/final/ and may replace the cover (or even
        # the video) there before writing publica: publish what Drive holds now.
        _download(carpeta, "final/short.mp4", video)
        cover: Optional[Path] = cover_path
        srt: Optional[Path] = srt_path
        try:
            _download(carpeta, "final/portada.jpg", cover_path)
        except FlowError as exc:
            logger.warning("flow: %s has no final/portada.jpg (%s); publishing without it", carpeta, exc)
            cover = None
        try:
            _download(carpeta, "final/captions.srt", srt_path)
        except FlowError as exc:
            logger.warning("flow: %s has no final/captions.srt (%s); publishing without it", carpeta, exc)
            srt = None
        series._publish_files(item, entry, video=video, cover=cover,
                              srt=srt, save=lambda: _save(ledger),
                              report={"meta_shadow": []})
    except series.SeriesPending:
        report["waiting"].append({"carpeta": carpeta, "why": "Instagram still processing"})
        _save(ledger)
        return
    except Exception as exc:                        # any publishing failure is recorded, never raised
        logger.exception("flow: publishing %s failed", carpeta)
        entry["error"] = str(exc)[:400]
        entry["publish_attempts"] = int(entry.get("publish_attempts") or 0) + 1
        if entry["publish_attempts"] >= MAX_PUBLISH_ATTEMPTS:
            entry["state"] = "failed"
            shutil.rmtree(_root() / carpeta, ignore_errors=True)
            report["failed"].append({"carpeta": carpeta, "error": entry["error"]})
        ledger[carpeta] = entry
        _save(ledger)
        return
    entry["state"] = "published"
    entry.pop("error", None)
    ledger[carpeta] = entry
    _save(ledger)
    lines = [f"publicado: {datetime.now(UTC).isoformat(timespec='seconds')}"]
    lines += [f"{net}: {entry[net]['url']}" for net in NETWORKS if (entry.get(net) or {}).get("url")]
    try:
        _write_text(carpeta, "PUBLICADO", "\n".join(lines) + "\n")
    except FlowError as exc:                        # the Short is out; a failed note is not a failed publish
        logger.warning("flow: could not write PUBLICADO for %s: %s", carpeta, exc)
    shutil.rmtree(_root() / carpeta, ignore_errors=True)
    report["published"].append({"carpeta": carpeta, "summary": series._short_summary(item, entry)})


def _needs_cover(carpeta: str, entry: dict[str, Any], ledger: dict[str, Any],
                 report: dict[str, Any]) -> None:
    item = {"carpeta": carpeta, "lang": entry.get("lang"), "seconds": entry.get("seconds"),
            "pack": str(entry.get("pack") or "")[:8000]}
    if entry.get("error"):
        item["error"] = entry["error"]
    report["needs_cover"].append(item)


def _needs_texts(carpeta: str, entry: dict[str, Any], ledger: dict[str, Any],
                 report: dict[str, Any]) -> None:
    report["needs_texts"].append({
        "carpeta": carpeta, "lang": entry.get("lang"), "article_url": entry.get("article_url"),
        "transcript": entry.get("transcript"), "flags": entry.get("manifest_flags") or {},
        "duration_s": entry.get("duration_s"),
    })


_STATE_ACTIONS = {
    "new": _needs_cover,
    "rendering": _collect,
    "rendered": _needs_texts,
    "ready": _approve,
    "publishing": _publish,
}


def _advance_entry(carpeta: str, entry: dict[str, Any], ledger: dict[str, Any],
                   report: dict[str, Any]) -> None:
    handler = _STATE_ACTIONS.get(str(entry.get("state")))
    if handler is not None:
        handler(carpeta, entry, ledger, report)


@_single_runner()
def advance() -> dict[str, Any]:
    """Move every Flow folder one step forward.

    Returns what needs the agent's words, what waits for the owner, and what
    happened. One folder's problem is recorded in that folder and reported,
    never raised: the next folder still advances.
    """
    report: dict[str, Any] = {name: [] for name in
                              ("needs_cover", "needs_texts", "waiting_approval", "published", "failed",
                               "waiting", "errors")}
    ledger = _load()
    _discover(ledger, report)
    for carpeta in list(ledger):
        entry = ledger[carpeta]
        try:
            _advance_entry(carpeta, entry, ledger, report)
        except Exception as exc:                    # one folder must never stop the rest
            logger.exception("flow: %s failed", carpeta)
            entry["error"] = str(exc)[:400]
            report["errors"].append(f"{carpeta}: {entry['error']}")
            ledger[carpeta] = entry
            _save(ledger)
    return report


# ---------------------------------------------------------------------------
# The agent's two steps: the cover, then the texts
# ---------------------------------------------------------------------------

@_single_runner()
def render(carpeta: str, article_url: str, cover_title: str, cover_kicker: str = "",
           cover_frame_s: float = 2.5) -> dict[str, Any]:
    """Send a ``new`` folder's footage, encrypted, to the renderer with the cover the agent chose."""
    ledger = _load()
    entry = ledger.get(carpeta)
    if entry is None:
        raise FlowError(f"{carpeta} is not in the Flow ledger; run flow first")
    if entry.get("state") != "new":
        raise FlowError(f"{carpeta} is {entry.get('state')!r}; flow_render needs a folder in 'new'")
    problems: list[str] = []
    url = series._article_url(article_url, problems)
    title = str(cover_title or "").strip()
    if not 3 <= len(title) <= 60:
        problems.append(f"cover_title: 3 to 60 characters, this one {len(title)}")
    kicker = str(cover_kicker or "").strip()
    if len(kicker) > 40:
        problems.append(f"cover_kicker: up to 40 characters, this one {len(kicker)}")
    try:
        frame_s = float(2.5 if cover_frame_s is None else cover_frame_s)
    except (TypeError, ValueError):
        problems.append("cover_frame_s: a number of seconds")
        frame_s = 2.5
    if not 0.0 <= frame_s <= 60.0:
        problems.append(f"cover_frame_s: 0 to 60 seconds, this one {frame_s}")
    if problems:
        raise FlowError("; ".join(problems))
    request_id = f"flow-{carpeta.replace('_', '-')}-{int(entry.get('attempts') or 0) + 1}"[:90]
    assert REQUEST_ID.fullmatch(request_id), request_id

    footage = _root() / carpeta / "footage.mp4"
    if not footage.is_file():
        raise FlowError(f"{carpeta}: footage.mp4 is missing; run flow again")
    try:
        data = Fernet(asset_key()).encrypt(footage.read_bytes())
    except (TypeError, ValueError) as exc:
        raise FlowError(f"FLOW_ASSET_KEY is not a usable Fernet key: {exc}") from exc
    asset = github_studio.upload_flow_input(data, f"{request_id}.bin")
    from plugins.shorts import flow_packs

    pack = flow_packs.parse(str(entry.get("pack") or ""))
    pkg = {"format": "flow", "lang": entry.get("lang"), "article_url": url,
           "cover": {"title": title, "kicker": kicker, "frame_s": frame_s},
           "footage": {"url": asset["url"], "encrypted": True},
           # The pack's on-screen captions and how many clips he must have filmed; the
           # renderer uses them to check the takes and to place the rótulos.
           "overlays": pack["overlays"], "expected_clips": len(pack["clips"]) or None}
    try:
        details = github_studio.dispatch(request_id, pkg)
    except Exception as exc:  # health: allow BLE001 -- any dispatch failure must still delete the asset
        try:
            github_studio.delete_flow_input(asset["id"])
        except Exception:                           # the dispatch failure is the one to report
            logger.exception("flow: could not delete release asset %s after a failed dispatch", asset["id"])
        raise FlowError(f"could not start the render: {exc}") from exc
    entry.update(state="rendering", request_id=request_id, asset_id=asset["id"],
                 run_id=details.get("run_id"), dispatched_at=_now(), article_url=url,
                 cover={"title": title, "kicker": kicker, "frame_s": frame_s})
    entry.pop("error", None)
    ledger[carpeta] = entry
    _save(ledger)
    return {"carpeta": carpeta, "request_id": request_id, "state": "rendering"}


@_single_runner()
def texts(carpeta: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """Validate the agent's texts, write them to Drive for the owner to review, and wait for ``publica``."""
    ledger = _load()
    entry = ledger.get(carpeta)
    if entry is None:
        raise FlowError(f"{carpeta} is not in the Flow ledger; run flow first")
    if entry.get("state") != "rendered":
        raise FlowError(f"{carpeta} is {entry.get('state')!r}; flow_texts needs a folder in 'rendered'")
    clean, problems = _validate_texts(metadata)
    if problems:
        raise FlowError("; ".join(problems))
    _write_text(carpeta, "final/textos.json", json.dumps(clean, ensure_ascii=False, indent=2))
    entry.update(texts=clean, state="ready", ready_at=time.time())
    entry.pop("error", None)
    ledger[carpeta] = entry
    _save(ledger)
    return {"carpeta": carpeta, "state": "ready", "message": _preview(carpeta, entry)}
