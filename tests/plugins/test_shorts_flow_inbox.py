"""Tests for the Flow inbox: Drive folders, the encrypted render, the owner's
``publica`` approval, and publishing through the series code.

rclone, GitHub Actions and YouTube are faked; nothing leaves the machine.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from plugins.shorts import flow_inbox, flow_packs, github_studio, series

CHAT = "42"
CARPETA = "2026-11-14_es-seo-local"
CARPETA_B = "2026-11-15_es-google-maps"
ARTICLE = "https://biglobster.top/ia-para-pymes/seo-local"
PACK = "Artículo: https://biglobster.top/ia-para-pymes/seo-local\n\nClip 1: «Tu negocio sale en Maps»\n"

# A pack in the format plugins/shorts/flow_packs.py validates: the folder's language, four
# clips of 3 s, the two characters in clips 1-3, and two on-screen captions.
PACK_ARTICLE = "https://biglobster.top/es/ia-para-pymes/seo-local"
_CLIP_0 = (
    "Wide shot of the shop at dawn, with the street empty and the shutter down.\n"
    f"{flow_packs.PROMPT_END}"
)
_CLIP_LINE = (
    "Medium shot of Lucía and Martín at the counter while the shop opens.\n"
    f"{flow_packs.VOICE_LINE['es']}\n"
    'Lucía says: "He buscado tu tienda en Google Maps y no aparece por ninguna parte."\n'
    'Martín answers: "Eso me dice un cliente cada semana y la competencia sale antes."\n'
    f"{flow_packs.PROMPT_END}"
)


def _flow_clip(number: int, prompt: str) -> str:
    return f"### Clip {number} — plano (3 s)\n```\n{prompt}\n```\n"


FLOW_PACK = (
    "# Tu tienda no aparece en Google Maps\n\n"
    f"Artículo: {PACK_ARTICLE}\n"
    "Idioma: es\n\n"
    "## Historia\nLa tienda que no sale en el mapa pierde clientes cada día.\n\n"
    "## Clips\n"
    f"{_flow_clip(0, _CLIP_0)}\n"
    f"{_flow_clip(1, _CLIP_LINE)}\n"
    f"{_flow_clip(2, _CLIP_LINE)}\n"
    f"{_flow_clip(3, _CLIP_LINE)}\n"
    "## Rótulos\n"
    "| Clip | Posición | Retardo (s) | Texto |\n"
    "|---|---|---|---|\n"
    "| 0 | arriba | 0 | ¿Tu tienda vacía? |\n"
    "| 1 | abajo | 1 | Atrae más clientes hoy |\n\n"
    "## Portada\n"
    "Título: Tu tienda no aparece en Google\n"
    "Rótulo: SEO local\n\n"
    "## Antes de rodar\n"
    "- Graba en vertical.\n"
)

TEXTOS = {
    "titulo": "Tu negocio sale en Google Maps o no existe",
    "descripcion": "Salir en Google Maps separa a un negocio de su barrio de uno invisible.\n\n"
                   "#IAparaPymes #SmallBusinessAI #BigLobster #SEOLocal #GoogleMaps #Pymes",
    "etiquetas": ["seo local", "google maps", "ia para pymes", "biglobster", "negocios locales"],
    "redes": {
        "instagram": "Tu negocio sale en Google Maps o no existe. Enlace en la bio. #SEOLocal #Pymes",
        "facebook": "Tu negocio sale en Google Maps o no existe. https://biglobster.top/?utm_source=facebook",
        "x": "Tu negocio sale en Google Maps o no existe. https://biglobster.top/?utm_source=x",
    },
}


def _bytes(value) -> bytes:
    return value if isinstance(value, bytes) else str(value).encode()


def _put(files: dict, carpeta: str, path: str, value) -> None:
    files.setdefault(carpeta, {})[path] = value


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for key, value in {
        "FLOW_ASSET_KEY": Fernet.generate_key().decode(),
        "FLOW_MAX_PER_DAY": "2",
        "TELEGRAM_HOME_CHANNEL": CHAT,
        "SHORTS_PUBLISH_MODE": "live",
        "YOUTUBE_CLIENT_ID": "c", "YOUTUBE_CLIENT_SECRET": "s", "YOUTUBE_REFRESH_TOKEN": "r",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(series, "_check_short", lambda path: 42.0)
    return tmp_path


@pytest.fixture
def drive(monkeypatch):
    """An in-memory Drive: ``{carpeta: {path: bytes|str}}`` behind the six rclone helpers."""
    files: dict[str, dict] = {}

    def download(carpeta, name, dest):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_bytes(files[carpeta][name]))

    def read_text(carpeta, path):
        value = files.get(carpeta, {}).get(path)
        return None if value is None else _bytes(value).decode()

    def upload(src, carpeta, path):
        _put(files, carpeta, path, Path(src).read_bytes())

    monkeypatch.setattr(flow_inbox, "_list_dirs", lambda: list(files))
    monkeypatch.setattr(flow_inbox, "_list_files", lambda carpeta: [
        {"Name": name, "Size": len(_bytes(value))}
        for name, value in files.get(carpeta, {}).items() if "/" not in name])
    monkeypatch.setattr(flow_inbox, "_download", download)
    monkeypatch.setattr(flow_inbox, "_read_text", read_text)
    monkeypatch.setattr(flow_inbox, "_upload", upload)
    monkeypatch.setattr(flow_inbox, "_write_text",
                        lambda carpeta, path, text: _put(files, carpeta, path, text))
    return files


@pytest.fixture
def studio(monkeypatch):
    """The GitHub Studio side: uploads, dispatches, runs and artifacts, all recorded."""
    state = {"uploads": [], "deleted": [], "dispatched": [], "runs": {}, "artifacts": {}}

    def upload(data, name):
        asset = {"id": len(state["uploads"]) + 1, "name": name, "size": len(data),
                 "url": f"https://api.github.com/repos/o/r/releases/assets/{len(state['uploads']) + 1}"}
        state["uploads"].append({**asset, "data": data})
        return asset

    def dispatch(request_id, pkg):
        state["dispatched"].append({"request_id": request_id, "package": pkg})
        return {"run_id": 7, "run_url": "https://github.com/o/r/actions/runs/7"}

    def artifact(run_id, name, dest):
        wanted = state["artifacts"].get(name)
        if wanted is None:
            return None
        dest = Path(dest)
        for filename, content in wanted.items():
            path = dest / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return dest

    monkeypatch.setattr(github_studio, "upload_flow_input", upload)
    monkeypatch.setattr(github_studio, "delete_flow_input",
                        lambda asset_id: state["deleted"].append(asset_id))
    monkeypatch.setattr(github_studio, "dispatch", dispatch)
    monkeypatch.setattr(github_studio, "get_run", lambda run_id: state["runs"].get(int(run_id)))
    monkeypatch.setattr(github_studio, "find_run", lambda request_id: None)
    monkeypatch.setattr(github_studio, "download_artifact", artifact)
    return state


@pytest.fixture
def published(monkeypatch):
    """The series publishing code: records the call and marks the Short on YouTube."""
    calls: list[dict] = []

    def fake(item, entry, video, cover, srt, save, report):
        calls.append({"item": item, "video": video, "cover": cover, "srt": srt,
                      "video_bytes": Path(video).read_bytes(),
                      "cover_bytes": Path(cover).read_bytes() if cover else None,
                      "srt_bytes": Path(srt).read_bytes() if srt else None})
        entry["youtube"] = {"id": "yt1", "url": "https://youtu.be/yt1",
                            "at": datetime.now(UTC).isoformat(timespec="seconds")}
        save()

    monkeypatch.setattr(series, "_publish_files", fake)
    return calls


def _messages(home: Path, rows: list[tuple[str, float]]) -> None:
    """A real state.db with the owner's Telegram messages, one per (content, timestamp)."""
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source TEXT, chat_id TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, session_id TEXT, "
                 "role TEXT, content TEXT, timestamp REAL)")
    for index, (content, timestamp) in enumerate(rows):
        conn.execute("INSERT OR IGNORE INTO sessions (id, source, chat_id) VALUES (?, 'telegram', ?)",
                     (f"tg-{index}", CHAT))
        conn.execute("INSERT INTO messages (session_id, role, content, timestamp) "
                     "VALUES (?, 'user', ?, ?)", (f"tg-{index}", content, timestamp))
    conn.commit()
    conn.close()


def _seed(drive, carpeta=CARPETA, pack=PACK, footage=b"flow footage") -> None:
    _put(drive, carpeta, "footage.mp4", footage)
    if pack is not None:
        _put(drive, carpeta, "prompts.md", pack)


FLAGS = {"first_word_s": 0.9, "late_start": True, "over_30s": True, "no_speech": False,
         "doubtful": [{"word": "Maps", "at_s": 1.2, "probability": 0.4}]}


def _finish(studio, request_id, *, passed=True, flags=FLAGS,
            transcript="Hola, esto se oye en el vídeo.", footage_s=None) -> None:
    studio["runs"][7] = {"id": 7, "status": "completed", "conclusion": "success",
                         "html_url": "https://github.com/o/r/actions/runs/7"}
    manifest = {"request_id": request_id, "format": "flow", "duration_s": 34.5,
                "footage_s": footage_s, "flags": flags,
                "qa": {"passed": passed, "errors": [] if passed else ["audio: too quiet"],
                       "warnings": []}}
    studio["artifacts"][f"short-{request_id}"] = {
        "manifest.json": json.dumps(manifest).encode(),
        "master.mp4": b"master",
        "cover.jpg": b"\xff\xd8\xff\xe0cover",
        "captions.srt": b"1\n00:00:00,000 --> 00:00:01,000\nHola\n",
        "transcript.txt": transcript.encode(),
    }


def _to_rendered(env, drive, studio, carpeta=CARPETA) -> str:
    _seed(drive, carpeta)
    flow_inbox.advance()
    flow_inbox.render(carpeta, ARTICLE, "Tu negocio sale en Maps", "SEO local")
    request_id = flow_inbox._load()[carpeta]["request_id"]
    _finish(studio, request_id)
    flow_inbox.advance()
    return request_id


def _to_ready(env, drive, studio, carpeta=CARPETA, metadata=None) -> None:
    _to_rendered(env, drive, studio, carpeta)
    flow_inbox.texts(carpeta, metadata or TEXTOS)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_a_folder_without_an_mp4_is_left_alone(env, drive, studio):
    _put(drive, CARPETA, "prompts.md", PACK)
    report = flow_inbox.advance()
    assert report["needs_cover"] == []
    assert flow_inbox._load() == {}


def test_two_mp4s_are_an_error_and_the_folder_is_not_added(env, drive, studio):
    _seed(drive)
    _put(drive, CARPETA, "second.mp4", b"other")
    report = flow_inbox.advance()
    assert any("more than one .mp4" in error for error in report["errors"])
    assert CARPETA not in flow_inbox._load()


def test_a_bad_folder_name_is_ignored(env, drive, studio):
    _seed(drive, "no-es-una-carpeta")
    report = flow_inbox.advance()
    assert report["needs_cover"] == [] and flow_inbox._load() == {}


def test_a_new_folder_waits_for_its_cover_with_the_pack(env, drive, studio):
    _seed(drive)
    report = flow_inbox.advance()
    assert [item["carpeta"] for item in report["needs_cover"]] == [CARPETA]
    item = report["needs_cover"][0]
    assert item["lang"] == "es" and item["seconds"] == 42.0
    assert "Artículo:" in item["pack"]


def test_footage_longer_than_a_minute_fails(env, drive, studio, monkeypatch):
    monkeypatch.setattr(series, "_check_short", lambda path: 75.0)
    _seed(drive)
    report = flow_inbox.advance()
    assert report["needs_cover"] == []
    assert flow_inbox._load()[CARPETA]["state"] == "failed"
    assert "60" in report["failed"][0]["error"]


def test_a_folder_that_fails_at_discovery_leaves_no_directory(env, drive, studio, monkeypatch):
    monkeypatch.setattr(series, "_check_short", lambda path: 75.0)
    _seed(drive)
    flow_inbox.advance()
    assert not (env / "workspace" / "shorts" / "flow" / CARPETA).exists()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_render_encrypts_the_footage_and_dispatches(env, drive, studio):
    plain = b"the owner's real footage"
    _seed(drive, footage=plain)
    flow_inbox.advance()
    result = flow_inbox.render(CARPETA, ARTICLE, "Tu negocio sale en Maps", "SEO local", 3.5)
    assert result["state"] == "rendering"
    upload = studio["uploads"][0]
    assert upload["data"] != plain
    assert Fernet(flow_inbox.asset_key()).decrypt(upload["data"]) == plain
    package = studio["dispatched"][0]["package"]
    assert package["format"] == "flow" and package["lang"] == "es"
    assert package["article_url"] == ARTICLE
    assert package["footage"] == {"url": upload["url"], "encrypted": True}
    assert package["cover"] == {"title": "Tu negocio sale en Maps", "kicker": "SEO local", "frame_s": 3.5}
    entry = flow_inbox._load()[CARPETA]
    assert entry["state"] == "rendering" and entry["asset_id"] == 1


def test_render_lists_every_validation_problem(env, drive, studio):
    _seed(drive)
    flow_inbox.advance()
    with pytest.raises(flow_inbox.FlowError) as excinfo:
        flow_inbox.render(CARPETA, "http://example.com/x", "ab", "x" * 41, 61)
    message = str(excinfo.value)
    for problem in ("articulo", "cover_title", "cover_kicker", "cover_frame_s"):
        assert problem in message
    assert studio["uploads"] == [] and studio["dispatched"] == []


def test_a_finished_render_comes_back_to_drive(env, drive, studio):
    _to_rendered(env, drive, studio)
    entry = flow_inbox._load()[CARPETA]
    assert entry["state"] == "rendered"
    for path in ("final/short.mp4", "final/portada.jpg", "final/captions.srt", "final/transcripcion.txt"):
        assert path in drive[CARPETA]
    assert studio["deleted"] == [1]
    report = flow_inbox.advance()
    item = report["needs_texts"][0]
    assert item["carpeta"] == CARPETA and item["article_url"] == ARTICLE
    assert "Hola, esto se oye" in item["transcript"]
    assert item["flags"]["late_start"] is True and item["flags"]["over_30s"] is True
    assert item["duration_s"] == 34.5


def test_three_failed_renders_are_terminal_and_delete_every_asset(env, drive, studio):
    _seed(drive)
    flow_inbox.advance()
    report = {}
    for _ in range(3):
        flow_inbox.render(CARPETA, ARTICLE, "Tu negocio sale en Maps")
        request_id = flow_inbox._load()[CARPETA]["request_id"]
        _finish(studio, request_id, passed=False)
        report = flow_inbox.advance()
    entry = flow_inbox._load()[CARPETA]
    assert entry["state"] == "failed" and entry["attempts"] == 3
    assert studio["deleted"] == [1, 2, 3]
    assert [item["carpeta"] for item in report["failed"]] == [CARPETA]
    assert "audio: too quiet" in entry["error"]


# ---------------------------------------------------------------------------
# Texts and the owner's approval
# ---------------------------------------------------------------------------

def test_texts_validates_then_writes_the_file_and_the_preview(env, drive, studio):
    _to_rendered(env, drive, studio)
    bad = {**TEXTOS, "titulo": "Corto", "redes": {**TEXTOS["redes"], "instagram": "dos\nlíneas"}}
    with pytest.raises(flow_inbox.FlowError) as excinfo:
        flow_inbox.texts(CARPETA, bad)
    message = str(excinfo.value)
    assert "titulo" in message and "redes.instagram" in message
    result = flow_inbox.texts(CARPETA, TEXTOS)
    stored = json.loads(drive[CARPETA]["final/textos.json"])
    assert stored["titulo"] == TEXTOS["titulo"] and stored["redes"] == TEXTOS["redes"]
    assert result["state"] == "ready"
    preview = result["message"]
    assert f"publica {CARPETA}" in preview
    assert preview.count("MEDIA:") == 2
    assert "master.mp4" in preview and "cover.jpg" in preview
    assert "La primera palabra se oye en 0,9 s" in preview


def test_the_preview_shows_a_footage_over_thirty_seconds(env, drive, studio):
    _seed(drive)
    flow_inbox.advance()
    flow_inbox.render(CARPETA, ARTICLE, "Tu negocio sale en Maps")
    _finish(studio, flow_inbox._load()[CARPETA]["request_id"], footage_s=31.0)
    flow_inbox.advance()
    message = flow_inbox.texts(CARPETA, TEXTOS)["message"]
    assert "31" in message and "más de 30" in message


def test_unheard_ignores_case_and_accents():
    assert flow_inbox._unheard("Sales en Google Maps", "¿sales en google maps o no existes?") is False
    assert flow_inbox._unheard("Página", "la pagina del negocio") is False
    assert flow_inbox._unheard("Sales en Bing", "¿sales en google maps o no existes?") is True


def test_the_preview_warns_only_about_cover_texts_the_video_does_not_say(env, drive, studio):
    _seed(drive)
    flow_inbox.advance()
    flow_inbox.render(CARPETA, ARTICLE, "Tu negocio sale en Maps", "SEO local")
    _finish(studio, flow_inbox._load()[CARPETA]["request_id"],
            transcript="Tu negocio sale en Maps o no existes.")
    flow_inbox.advance()
    message = flow_inbox.texts(CARPETA, TEXTOS)["message"]
    warnings = [line for line in message.splitlines() if "La portada dice" in line]
    assert len(warnings) == 1 and "«SEO local»" in warnings[0]
    assert "«Tu negocio sale en Maps»" not in message


def test_ready_without_approval_waits(env, drive, studio, published):
    _to_ready(env, drive, studio)
    report = flow_inbox.advance()
    assert [item["carpeta"] for item in report["waiting_approval"]] == [CARPETA]
    assert published == []
    assert flow_inbox._load()[CARPETA]["state"] == "ready"


def test_only_an_approval_after_the_short_was_ready_counts(env, drive, studio, published):
    _to_ready(env, drive, studio)
    ready_at = flow_inbox._load()[CARPETA]["ready_at"]
    _messages(env, [(f"publica {CARPETA}", ready_at - 10)])
    assert flow_inbox.advance()["waiting_approval"]
    assert published == []
    _messages(env, [(f"publica {CARPETA}", ready_at + 10)])
    report = flow_inbox.advance()
    assert [item["carpeta"] for item in report["published"]] == [CARPETA]
    entry = flow_inbox._load()[CARPETA]
    assert entry["state"] == "published" and entry["approval"]["at"] == ready_at + 10
    assert "PUBLICADO" in drive[CARPETA]
    assert not (env / "workspace" / "shorts" / "flow" / CARPETA).exists()


def test_a_second_run_while_one_is_publishing_is_turned_away(env, drive, studio, published, monkeypatch):
    # 2026-11-16: the chat agent and the hourly job both ran flow on a ready Short.
    _to_ready(env, drive, studio)
    _messages(env, [(f"publica {CARPETA}", flow_inbox._load()[CARPETA]["ready_at"] + 10)])
    seen = []
    publish_files = series._publish_files

    def publish_while_another_run_comes_in(*args, **kwargs):
        with pytest.raises(flow_inbox.FlowError, match="another Flow run"):
            flow_inbox.advance()
        seen.append("turned away")
        return publish_files(*args, **kwargs)

    monkeypatch.setattr(series, "_publish_files", publish_while_another_run_comes_in)
    report = flow_inbox.advance()
    assert seen == ["turned away"]
    assert [item["carpeta"] for item in report["published"]] == [CARPETA]
    assert len(published) == 1
    assert flow_inbox.advance()["published"] == []   # the lock is released afterwards


def test_the_owners_edit_of_the_texts_is_what_gets_published(env, drive, studio, published):
    _to_ready(env, drive, studio)
    edited = {**TEXTOS, "titulo": "El título que el dueño prefirió en Drive"}
    _put(drive, CARPETA, "final/textos.json", json.dumps(edited))
    _messages(env, [(f"publica {CARPETA}", time.time() + 1)])
    flow_inbox.advance()
    assert published[0]["item"]["titulo"] == edited["titulo"]


def test_the_cover_the_owner_replaces_in_drive_is_the_one_published(env, drive, studio, published):
    _to_ready(env, drive, studio)
    _put(drive, CARPETA, "final/portada.jpg", b"\xff\xd8\xff\xe0owner-cover")
    _messages(env, [(f"publica {CARPETA}", time.time() + 1)])
    report = flow_inbox.advance()
    assert [item["carpeta"] for item in report["published"]] == [CARPETA]
    assert published[0]["cover_bytes"] == b"\xff\xd8\xff\xe0owner-cover"
    assert published[0]["cover"].parent.name == "publish"


def test_a_missing_video_in_drive_counts_a_publish_attempt(env, drive, studio, published, monkeypatch):
    _to_ready(env, drive, studio)
    del drive[CARPETA]["final/short.mp4"]
    download = flow_inbox._download

    def fail_when_missing(carpeta, name, dest):
        if name not in drive.get(carpeta, {}):
            raise flow_inbox.FlowError(f"rclone copyto failed: {name}")
        download(carpeta, name, dest)

    monkeypatch.setattr(flow_inbox, "_download", fail_when_missing)
    _messages(env, [(f"publica {CARPETA}", time.time() + 1)])
    report = flow_inbox.advance()
    assert published == [] and report["published"] == []
    assert flow_inbox._load()[CARPETA]["publish_attempts"] == 1
    assert report["failed"] == []


def test_the_daily_cap_holds_the_second_short(env, drive, studio, published, monkeypatch):
    monkeypatch.setenv("FLOW_MAX_PER_DAY", "1")
    for carpeta in (CARPETA, CARPETA_B):
        _seed(drive, carpeta)
    flow_inbox.advance()
    for carpeta in (CARPETA, CARPETA_B):
        flow_inbox.render(carpeta, ARTICLE, "Tu negocio sale en Maps")
        _finish(studio, flow_inbox._load()[carpeta]["request_id"])
    flow_inbox.advance()
    for carpeta in (CARPETA, CARPETA_B):
        flow_inbox.texts(carpeta, TEXTOS)
    now = time.time()
    _messages(env, [(f"publica {CARPETA}", now + 1), (f"publica {CARPETA_B}", now + 2)])
    report = flow_inbox.advance()
    assert [item["carpeta"] for item in report["published"]] == [CARPETA]
    assert [item["carpeta"] for item in report["waiting"]] == [CARPETA_B]
    assert len(published) == 1
    assert flow_inbox._load()[CARPETA_B]["state"] == "publishing"


def test_shadow_mode_waits_and_publishes_nothing(env, drive, studio, published, monkeypatch):
    _to_ready(env, drive, studio)
    _messages(env, [(f"publica {CARPETA}", time.time() + 1)])
    monkeypatch.setenv("SHORTS_PUBLISH_MODE", "shadow")
    report = flow_inbox.advance()
    assert [item["carpeta"] for item in report["waiting"]] == [CARPETA]
    assert "shadow" in report["waiting"][0]["why"]
    assert published == []
    assert flow_inbox._load()[CARPETA]["state"] == "publishing"


def test_preview_shows_qa_findings_from_the_owners_footage(env):
    """Footage findings pass QA as warnings, so the preview is the only place he sees them."""
    entry = {"duration_s": 34.5, "manifest_flags": {}, "texts": {"redes": {}},
             "footage_warnings": ["footage: 2.1s of silence at 4.0s"]}
    message = flow_inbox._preview("2026-11-14_es-seo-local-google-maps", entry)
    assert "⚠️ En tu vídeo: 2.1s of silence at 4.0s" in message
    assert "footage: " not in message


def test_a_render_that_never_starts_deletes_its_encrypted_input(env, drive, studio):
    """hermes-auditor on #526: the timeout path popped asset_id without deleting the blob."""
    _seed(drive)
    flow_inbox.advance()
    flow_inbox.render(CARPETA, ARTICLE, "Sales en Google Maps", "SEO local", 2.5)
    ledger = flow_inbox._load()
    ledger[CARPETA].update(run_id=None, dispatched_at="2020-01-01T00:00:00Z")
    flow_inbox._save(ledger)
    flow_inbox.advance()
    entry = flow_inbox._load()[CARPETA]
    assert studio["deleted"] == [1]
    assert entry["state"] == "new" and "asset_id" not in entry


# ---------------------------------------------------------------------------
# The script packs: lessons from the owner's edits, captions and clip count to the render
# ---------------------------------------------------------------------------

def test_discovery_keeps_the_owners_edits_of_the_pack_as_a_lesson(env, drive, studio):
    carpeta = "2026-11-14_es-guion-nuevo"
    flow_packs.write(carpeta, FLOW_PACK)
    edited = FLOW_PACK.replace("Atrae más clientes hoy", "Atrae más clientes esta semana")
    _put(drive, carpeta, "footage.mp4", b"flow footage")
    _put(drive, carpeta, "prompts.md", edited)
    flow_inbox.advance()
    lesson = flow_packs._lessons_dir() / f"{carpeta}.diff"
    assert lesson.is_file()
    body = lesson.read_text(encoding="utf-8-sig")
    assert "--- escrito" in body and "+++ rodado" in body
    assert "+| 1 | abajo | 1 | Atrae más clientes esta semana |" in body
    assert flow_inbox._load()[carpeta]["pack"] == edited


def test_render_sends_the_packs_captions_and_clip_count(env, drive, studio):
    _seed(drive, pack=FLOW_PACK)
    flow_inbox.advance()
    flow_inbox.render(CARPETA, ARTICLE, "Tu tienda sale en Maps", "SEO local")
    package = studio["dispatched"][0]["package"]
    assert package["overlays"] == [
        {"clip": 0, "position": "top", "delay_s": 0.0, "text": "¿Tu tienda vacía?"},
        {"clip": 1, "position": "bottom", "delay_s": 1.0, "text": "Atrae más clientes hoy"},
    ]
    assert package["expected_clips"] == 4


def test_render_of_a_pack_without_rotulos_sends_no_captions(env, drive, studio):
    _seed(drive)                                    # the old free-text pack
    flow_inbox.advance()
    flow_inbox.render(CARPETA, ARTICLE, "Tu negocio sale en Maps")
    package = studio["dispatched"][0]["package"]
    assert package["overlays"] == []
    assert package["expected_clips"] is None
