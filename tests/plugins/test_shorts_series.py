"""Tests for the "Agentes BigLobster" series: episode and Flow short requests from Buzz, published on YouTube.

Buzz (the ``buzz`` CLI) and YouTube are faked; nothing leaves the machine.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugins.shorts import series
from plugins.shorts import studio_tool as st
from plugins.shorts.social import youtube

RELAY = "https://relay.example"
VIDEODESK = "d8" * 32
SEODESK = "5e" * 32
STRANGER = "ab" * 32


def _request(**over):
    body = {
        "episodio": "01", "agente": "content-gap-hunter",
        "es": {"video": f"{RELAY}/media/{'1' * 64}.mp4", "srt": f"{RELAY}/media/{'2' * 64}.srt",
               "titulo": "Content Gap Hunter: el artículo que te falta",
               "descripcion": "Cómo trabaja el agente que encuentra los huecos de tu blog.",
               "etiquetas": ["ia", "blog"]},
        "en": {"video": f"{RELAY}/media/{'3' * 64}.mp4", "srt": None,
               "titulo": "Content Gap Hunter: the article you're missing",
               "descripcion": "How the agent that finds your blog's gaps works.",
               "etiquetas": ["ai", "blog"]},
    }
    body.update(over)
    return f"{series.MARK}\n```json\n{json.dumps(body, ensure_ascii=False)}\n```"


def _update(**over):
    body = {
        "episodio": "01", "agente": "content-gap-hunter",
        "es": {"id": "esVideoId01", "titulo": "El artículo que te falta | Content Gap Hunter",
               "descripcion": "0:00 El problema\n0:20 Cómo trabaja\n1:30 Qué recibes\n\n#SEO #IA",
               "etiquetas": ["seo", "ia"], "portada": f"{RELAY}/media/{'4' * 64}.jpg"},
    }
    body.update(over)
    return f"{series.MARK_UPDATE}\n```json\n{json.dumps(body, ensure_ascii=False)}\n```"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path, raising=False)
    for k, v in {"SERIES_BUZZ_CHANNEL": "f4a74eae-a548-56fe-9a2e-f89bfd929f0e",
                 "SERIES_BUZZ_FROM": f"{VIDEODESK},{SEODESK}",
                 "BUZZ_RELAY_URL": "wss://relay.example", "BUZZ_PRIVATE_KEY": "nsec1test", "BUZZ_AUTH_TAG": "[]",
                 "YOUTUBE_CLIENT_ID": "c", "YOUTUBE_CLIENT_SECRET": "s", "YOUTUBE_REFRESH_TOKEN": "r",
                 "SHORTS_BUZZ_CHANNEL": "ccc2bc1a-7a82-5a8f-8c4e-57a070cbe7cd", "SHORTS_PUBLISH_MODE": "live"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(series.buzz_share, "cli", lambda: "/usr/local/bin/buzz")
    monkeypatch.setattr(series, "_check_video", lambda path: 123.0)
    monkeypatch.setattr(series, "_check_short", lambda path: 42.0)
    monkeypatch.setattr(series.buzz_share, "canonical", lambda path: Path(path))
    return tmp_path


@pytest.fixture
def buzz(monkeypatch):
    """A fake channel: ``events`` is what `messages get` returns; every call is recorded."""
    state = {"events": [], "calls": []}

    def fake_run(args, **kw):
        state["calls"].append((args, kw))
        if args[1:3] == ["messages", "get"]:
            out = json.dumps(state["events"])
        elif args[1:3] == ["media", "get"]:
            dest = Path(args[args.index("-o") + 1])
            dest.write_bytes(b"\xff\xd8\xff\xe0cover" if dest.name.startswith("cover") or "portada" in dest.name else b"video")
            out = ""
        else:
            out = "{}"
        return type("Done", (), {"returncode": 0, "stdout": out, "stderr": ""})()

    monkeypatch.setattr(series.subprocess, "run", fake_run)
    return state


@pytest.fixture
def uploads(monkeypatch):
    done = []

    def fake_upload(video, thumb, srt, social, lang, **kw):
        done.append({"lang": lang, "srt": srt, "social": social, **kw})
        vid = f"{lang}VideoId01"
        return {"id": vid, "url": f"https://www.youtube.com/watch?v={vid}", "privacy": "public", "warnings": []}

    monkeypatch.setattr(youtube, "upload", fake_upload)
    return done


def _event(content, pubkey=VIDEODESK, eid="e" * 64, at=100):
    return {"id": eid, "pubkey": pubkey, "content": content, "created_at": at}


def _sends(buzz):
    return [(a, kw) for a, kw in buzz["calls"] if a[1:3] == ["messages", "send"]]


def test_long_video_metadata_has_no_shorts_tag_and_own_privacy():
    social = {"youtube": {"title": "Content Gap Hunter: the article you're missing", "description": "d", "tags": ["a"]}}
    meta = youtube.build_metadata(social, "en", synthetic=False, short=False, privacy="unlisted")
    assert "#Shorts" not in meta["snippet"]["title"] and meta["status"]["privacyStatus"] == "unlisted"
    assert "#Shorts" in youtube.build_metadata(social, "en", synthetic=False)["snippet"]["title"]


def test_parse_accepts_a_good_request_and_lists_every_problem(env):
    item = series.parse(_request())
    assert item["agente"] == "content-gap-hunter" and item["en"]["srt"] is None
    with pytest.raises(series.SeriesError) as exc:
        series.parse(_request(agente="Content Gap!", es={"video": "https://evil.example/media/x.mp4",
                                                         "titulo": "x", "descripcion": "", "etiquetas": "ia"}))
    text = str(exc.value)
    assert "agente" in text and "es.video" in text and "es.titulo" in text and "es.etiquetas" in text
    with pytest.raises(series.SeriesError):
        series.parse("hola")


def test_an_episode_is_published_in_both_languages_once(env, buzz, uploads):
    buzz["events"] = [_event(_request()), _event(_request(), pubkey=STRANGER, eid="f" * 64)]
    out = json.loads(st.handle_shorts_studio({"action": "series"}))
    assert out["success"] and len(out["published"]) == 1
    assert [u["lang"] for u in uploads] == ["es", "en"]
    assert all(u["short"] is False and u["privacy"] == "public" for u in uploads)
    assert uploads[0]["srt"] is not None and uploads[1]["srt"] is None
    assert uploads[0]["social"]["youtube"]["title"].startswith("Content Gap Hunter")
    (args, kw), = _sends(buzz)
    assert args[args.index("--reply-to") + 1] == "e" * 64 and args[args.index("--mention") + 1] == VIDEODESK
    assert "watch?v=esVideoId0" in kw["input"] and "watch?v=enVideoId0" in kw["input"]
    assert "videosAgentes.json" in kw["input"]
    # Only the Buzz credentials reach the CLI.
    assert set(kw["env"]) <= {"PATH", "HOME", "BUZZ_RELAY_URL", "BUZZ_PRIVATE_KEY", "BUZZ_AUTH_TAG"}
    # The downloaded masters are not kept.
    assert not any((env / "workspace" / "shorts" / "series").glob("*/*.mp4"))

    again = json.loads(st.handle_shorts_studio({"action": "series"}))
    assert again["success"] and "published" not in again and len(uploads) == 2 and len(_sends(buzz)) == 1


def test_one_episode_a_day_by_default(env, buzz, uploads):
    buzz["events"] = [_event(_request(), eid="a" * 64, at=1),
                      _event(_request(episodio="02", agente="site-launch"), eid="b" * 64, at=2)]
    out = series.run()
    assert [p["agente"] for p in out["published"]] == ["content-gap-hunter"]
    assert out["waiting"][0]["agente"] == "site-launch" and len(uploads) == 2


def test_shadow_mode_publishes_nothing(env, buzz, uploads, monkeypatch):
    monkeypatch.setenv("SHORTS_PUBLISH_MODE", "shadow")
    buzz["events"] = [_event(_request())]
    assert "shadow" in series.run()["skipped"] and not uploads and not buzz["calls"]


def test_a_malformed_request_is_answered_once_and_never_uploaded(env, buzz, uploads):
    buzz["events"] = [_event(f"{series.MARK}\n{{not json")]
    out = series.run()
    assert out["invalid"] and not uploads
    (args, kw), = _sends(buzz)
    assert "No puedo publicar" in kw["input"]
    series.run()
    assert len(_sends(buzz)) == 1


def test_a_failed_language_is_retried_alone_next_run(env, buzz, uploads, monkeypatch):
    buzz["events"] = [_event(_request())]
    real = youtube.upload

    def flaky(video, thumb, srt, social, lang, **kw):
        if lang == "en":
            raise youtube.YouTubeError("upload video: HTTP 503")
        return real(video, thumb, srt, social, lang, **kw)

    monkeypatch.setattr(youtube, "upload", flaky)
    out = series.run()
    assert out["failed"][0]["attempt"] == 1 and [u["lang"] for u in uploads] == ["es"] and not _sends(buzz)

    monkeypatch.setattr(youtube, "upload", real)
    out = series.run()
    assert out["published"] and [u["lang"] for u in uploads] == ["es", "en"] and len(_sends(buzz)) == 1


def test_not_configured_is_a_quiet_skip(tmp_path, monkeypatch):
    monkeypatch.delenv("SERIES_BUZZ_CHANNEL", raising=False)
    assert "not configured" in series.run()["skipped"]


@pytest.fixture
def updates(monkeypatch):
    done = []

    def fake_update(video_id, social, lang, thumb=None, short=False):
        done.append({"id": video_id, "lang": lang, "social": social, "short": short,
                     "thumb": thumb.read_bytes() if thumb else None})
        return {"id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}", "thumbnail": bool(thumb),
                "warnings": []}

    monkeypatch.setattr(youtube, "update", fake_update)
    return done


def test_parse_update_lists_every_problem(env):
    item = series.parse_update(_update())
    assert item["langs"] == ["es"] and item["es"]["portada"].endswith(".jpg")
    with pytest.raises(series.SeriesError) as exc:
        series.parse_update(_update(es={"id": "short", "titulo": "Un <título> largo", "descripcion": "corta",
                                        "portada": "https://evil.example/x.jpg"}))
    text = str(exc.value)
    assert "es.id" in text and "es.descripcion" in text and "< and >" in text and "es.portada" in text
    with pytest.raises(series.SeriesError, match="at least one language"):
        series.parse_update(_update(es=None))


def test_seodesk_optimises_a_published_episode_once(env, buzz, uploads, updates):
    buzz["events"] = [_event(_request(), eid="a" * 64, at=1),
                      _event(_update(), pubkey=SEODESK, eid="b" * 64, at=2),
                      _event(_update(), pubkey=STRANGER, eid="c" * 64, at=3)]
    out = series.run()
    assert out["published"] and out["updated"] == [{"episodio": "01", "agente": "content-gap-hunter", "langs": ["es"]}]
    (up,) = updates
    assert up["id"] == "esVideoId01" and up["thumb"].startswith(b"\xff\xd8")
    assert up["social"]["youtube"]["description"].startswith("0:00 El problema")
    reply = [kw["input"] for a, kw in _sends(buzz) if a[a.index("--reply-to") + 1] == "b" * 64]
    assert len(reply) == 1 and "actualizado" in reply[0] and "portada" in reply[0]
    assert not any((env / "workspace" / "shorts" / "series").glob("u-*/*"))

    again = series.run()
    assert "updated" not in again and len(updates) == 1


def test_an_update_for_a_video_outside_the_series_is_refused(env, buzz, uploads, updates):
    buzz["events"] = [_event(_update(), pubkey=SEODESK)]
    out = series.run()
    assert out["invalid"] and "not a video of the series" in out["invalid"][0]["error"] and not updates
    (args, kw), = _sends(buzz)
    assert "No puedo actualizar" in kw["input"]


def test_youtube_update_rewrites_the_snippet_and_sets_the_cover(env, tmp_path, monkeypatch):
    calls = []

    class Resp:
        def __init__(self, code, body=None):
            self.status_code, self._body, self.text = code, body or {}, json.dumps(body or {})

        def json(self):
            return self._body

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kw):
            calls.append(("post", url, kw))
            return Resp(200, {"access_token": "t"} if "token" in url else {})

        def put(self, url, **kw):
            calls.append(("put", url, kw))
            return Resp(200, {"id": "esVideoId0"})

    monkeypatch.setattr(youtube, "_client", lambda: Client())
    cover = tmp_path / "c.jpg"
    cover.write_bytes(b"\xff\xd8\xff")
    social = {"youtube": {"title": "El artículo que te falta", "description": "0:00 Inicio", "tags": ["seo"]}}
    out = youtube.update("esVideoId0", social, "es", cover)
    put = next(c for c in calls if c[0] == "put")
    body = json.loads(put[2]["content"])
    assert put[2]["params"] == {"part": "snippet"} and body["id"] == "esVideoId0"
    assert body["snippet"]["title"] == "El artículo que te falta" and body["snippet"]["categoryId"]
    assert any(c[1] == youtube.THUMB_URL for c in calls) and out["thumbnail"] is True


def _short_update(rid="es-limite-decision-ia-pymes-2026-20261017040713", vid="shortVid001"):
    body = {"short": rid, "es": {"id": vid, "titulo": "¿Dónde pones el límite a la IA en tu pyme?",
                                 "descripcion": "La línea de decisión que tu equipo necesita, en una tarde. #IA #pymes",
                                 "etiquetas": ["IA", "pymes"], "portada": None}}
    return f"{series.MARK_UPDATE}\n```json\n{json.dumps(body, ensure_ascii=False)}\n```"


def test_seodesk_optimises_a_blog_short_only_with_its_own_id(env, buzz, uploads, updates, monkeypatch):
    monkeypatch.setattr(series, "_short_ids", lambda: {"es-limite-decision-ia-pymes-2026-20261017040713": "shortVid001"})
    buzz["events"] = [_event(_short_update(), pubkey=SEODESK, eid="a" * 64, at=1),
                      _event(_short_update(vid="otherVid001"), pubkey=SEODESK, eid="b" * 64, at=2)]
    out = series.run()
    assert out["updated"] == [{"short": "es-limite-decision-ia-pymes-2026-20261017040713", "langs": ["es"]}]
    assert [(u["id"], u["short"]) for u in updates] == [("shortVid001", True)]
    assert "is not a Short uploaded by Hermes" in out["invalid"][0]["error"]
    replies = {a[a.index("--reply-to") + 1]: kw["input"] for a, kw in _sends(buzz)}
    assert replies["a" * 64].startswith("Short es-limite") and "No puedo actualizar" in replies["b" * 64]


def test_youtube_update_keeps_the_shorts_tag_for_a_short(monkeypatch):
    social = {"youtube": {"title": "¿Dónde pones el límite a la IA?", "description": "d", "tags": []}}
    assert "#Shorts" in youtube.build_metadata(social, "es", synthetic=False, short=True)["snippet"]["title"]


# ---------------------------------------------------------------------------
# Flow shorts: #publicar-short


FLOW = "2026-10-21_es-ia-empleados-junior"


def _short_request(**over):
    body = {
        "short": FLOW, "idioma": "es", "articulo": "https://biglobster.top/es/blog/ia-empleados-junior/",
        "video": f"{RELAY}/media/{'5' * 64}.mp4", "srt": f"{RELAY}/media/{'6' * 64}.srt",
        "portada": f"{RELAY}/media/{'7' * 64}.jpg",
        "titulo": "Tu empleado junior ya es una IA",
        "descripcion": "Qué cambia en tu pyme cuando el trabajo de entrada lo hace una IA.",
        "etiquetas": ["ia", "pymes"],
        "redes": {"instagram": "Texto IG #IA", "facebook": "Texto FB", "x": "Texto X"},
    }
    body.update(over)
    return f"{series.MARK_SHORT}\n```json\n{json.dumps(body, ensure_ascii=False)}\n```"


def _handoffs(buzz):
    return [(a, kw) for a, kw in _sends(buzz) if "--file" in a]


def _replies(buzz):
    return {a[a.index("--reply-to") + 1]: kw["input"] for a, kw in _sends(buzz) if "--reply-to" in a}


@pytest.fixture
def short_uploads(monkeypatch):
    done = []

    def fake_upload(video, thumb, srt, social, lang, **kw):
        done.append({"lang": lang, "thumb": thumb.read_bytes() if thumb else None,
                     "srt": srt.read_bytes() if srt else None, "social": social, **kw})
        vid = f"flowVid{len(done):04d}"
        return {"id": vid, "url": f"https://youtube.com/shorts/{vid}", "privacy": "public", "warnings": []}

    monkeypatch.setattr(youtube, "upload", fake_upload)
    return done


def test_parse_short_accepts_a_good_request_and_lists_every_problem(env):
    item = series.parse_short(_short_request())
    assert item["short"] == FLOW and item["idioma"] == "es" and item["redes"]["x"] == "Texto X"
    bare = series.parse_short(_short_request(srt=None, portada=None, redes=None))
    assert bare["srt"] is None and bare["portada"] is None and bare["redes"] == {"instagram": "", "facebook": "", "x": ""}
    with pytest.raises(series.SeriesError) as exc:
        series.parse_short(_short_request(short="Flow Short!", idioma="fr", articulo="http://biglobster.top/x",
                                          video="https://evil.example/media/x.mp4", portada="https://evil.example/c.jpg",
                                          titulo="x", descripcion="", etiquetas="ia", redes={"x": 5}))
    text = str(exc.value)
    for field in ("short:", "idioma", "articulo", "video", "portada", "titulo", "descripcion", "etiquetas", "redes"):
        assert field in text
    assert "es.titulo" not in text
    for url in ("https://biglobster.top.evil.example/a", "https://user@biglobster.top/a", "https://example.com/biglobster.top"):
        with pytest.raises(series.SeriesError, match="articulo"):
            series.parse_short(_short_request(articulo=url))
    assert series.parse_short(_short_request(articulo="https://www.biglobster.top/a"))["articulo"]
    with pytest.raises(series.SeriesError):
        series.parse_short("hola")


def test_a_flow_short_is_uploaded_replied_and_handed_to_socialbot_once(env, buzz, short_uploads):
    buzz["events"] = [_event(_short_request()), _event(_short_request(), pubkey=STRANGER, eid="f" * 64)]
    out = json.loads(st.handle_shorts_studio({"action": "series"}))
    assert out["success"] and out["published"] == [{"short": FLOW, "handoff": True, "id": "flowVid0001",
                                                    "url": "https://youtube.com/shorts/flowVid0001", "privacy": "public"}]
    (up,) = short_uploads
    assert up["short"] is True and up["synthetic"] is True and up["privacy"] == "public" and up["lang"] == "es"
    assert up["thumb"].startswith(b"\xff\xd8") and up["srt"] == b"video"
    assert up["article"]["url"].startswith("https://biglobster.top/") and up["social"]["youtube"]["tags"] == ["ia", "pymes"]

    replies = _replies(buzz)
    assert set(replies) == {"e" * 64}
    reply_args = next(a for a, kw in _sends(buzz) if "--reply-to" in a)
    assert reply_args[reply_args.index("--mention") + 1] == VIDEODESK
    assert "youtube.com/shorts/flowVid0001" in replies["e" * 64] and "SocialBot" in replies["e" * 64]

    (args, kw), = _handoffs(buzz)
    assert args[args.index("--channel") + 1] == "ccc2bc1a-7a82-5a8f-8c4e-57a070cbe7cd"
    assert [Path(args[i + 1]).name for i, a in enumerate(args) if a == "--file"] == ["master.mp4", "cover.jpg"]
    text = kw["input"]
    assert "@SocialBot" in text and "youtube.com/shorts/flowVid0001" in text and f"id: {FLOW}" in text
    assert "Texto IG #IA" in text and "Texto FB" in text and "Texto X" in text and "contenido IA" in text
    assert "https://biglobster.top/es/blog/ia-empleados-junior/" in text
    assert "Story" not in text and "Reel con cover.jpg como portada" in text
    assert set(kw["env"]) <= {"PATH", "HOME", "BUZZ_RELAY_URL", "BUZZ_PRIVATE_KEY", "BUZZ_AUTH_TAG"}
    assert not any((env / "workspace" / "shorts" / "series").glob("*/*.mp4"))

    ledger = json.loads((env / "workspace" / "shorts" / "series" / "ledger.json").read_text())
    assert ledger["e" * 64]["kind"] == "short" and ledger["e" * 64]["youtube"]["id"] == "flowVid0001"

    again = series.run()
    assert "published" not in again and len(short_uploads) == 1 and len(_sends(buzz)) == 2


def test_the_hand_off_falls_back_to_the_description_without_redes(env, buzz, short_uploads):
    buzz["events"] = [_event(_short_request(redes=None, portada=None))]
    series.run()
    (args, kw), = _handoffs(buzz)
    assert [Path(args[i + 1]).name for i, a in enumerate(args) if a == "--file"] == ["master.mp4"]
    assert kw["input"].count("Qué cambia en tu pyme") == 3 and "Reel con cover.jpg" not in kw["input"]


def test_a_png_cover_is_not_attached_to_the_hand_off(env, buzz, short_uploads, monkeypatch):
    real = series._download

    def png(url, dest):
        real(url, dest)
        if dest.name == "cover.jpg":
            dest.write_bytes(b"\x89PNG\r\n\x1a\ncover")
        return dest

    monkeypatch.setattr(series, "_download", png)
    buzz["events"] = [_event(_short_request())]
    series.run()
    (args, kw), = _handoffs(buzz)
    assert [Path(args[i + 1]).name for i, a in enumerate(args) if a == "--file"] == ["master.mp4"]
    assert short_uploads[0]["thumb"].startswith(b"\x89PNG")
    assert "sólo acepta JPEG" in _replies(buzz)["e" * 64]


def test_flow_shorts_have_their_own_daily_cap_apart_from_the_episodes(env, buzz, uploads):
    buzz["events"] = [_event(_request(), eid="a" * 64, at=1)] + [
        _event(_short_request(short=f"2026-10-21_es-tema-{n}"), eid=str(n) * 64, at=10 + n) for n in (1, 2, 3)]
    out = series.run()
    assert [p.get("short") or p["agente"] for p in out["published"]] == [
        "content-gap-hunter", "2026-10-21_es-tema-1", "2026-10-21_es-tema-2"]
    assert out["waiting"] == [{"short": "2026-10-21_es-tema-3", "why": "2 short(s) a day (YouTube quota)"}]
    assert [u["short"] for u in uploads] == [False, False, True, True]


def test_the_shorts_cap_is_configurable(env, buzz, short_uploads, monkeypatch):
    monkeypatch.setenv("SERIES_SHORTS_MAX_PER_DAY", "1")
    buzz["events"] = [_event(_short_request(short=f"2026-10-21_es-tema-{n}"), eid=str(n) * 64, at=n) for n in (1, 2)]
    out = series.run()
    assert len(out["published"]) == 1 and len(out["waiting"]) == 1 and len(short_uploads) == 1


def test_shadow_mode_publishes_no_flow_short(env, buzz, short_uploads, monkeypatch):
    monkeypatch.setenv("SHORTS_PUBLISH_MODE", "shadow")
    buzz["events"] = [_event(_short_request())]
    assert "shadow" in series.run()["skipped"] and not short_uploads and not buzz["calls"]


def test_a_malformed_short_request_is_answered_once_and_never_uploaded(env, buzz, short_uploads):
    buzz["events"] = [_event(_short_request(articulo="https://evil.example/a", idioma="fr"))]
    out = series.run()
    assert out["invalid"] and not short_uploads
    (args, kw), = _sends(buzz)
    assert "No puedo publicar este short" in kw["input"] and "articulo" in kw["input"] and "idioma" in kw["input"]
    series.run()
    assert len(_sends(buzz)) == 1


def test_the_same_flow_short_is_not_uploaded_twice_from_two_messages(env, buzz, short_uploads):
    buzz["events"] = [_event(_short_request(), eid="a" * 64, at=1), _event(_short_request(), eid="b" * 64, at=2)]
    out = series.run()
    assert len(short_uploads) == 1 and out["published"][0]["short"] == FLOW
    assert "is already on YouTube" in out["invalid"][0]["error"]
    assert "No puedo publicar este short" in _replies(buzz)["b" * 64]


def test_a_failed_hand_off_is_retried_without_uploading_again(env, buzz, short_uploads, monkeypatch):
    buzz["events"] = [_event(_short_request())]
    real = series.subprocess.run

    def refuse_files(args, **kw):
        if "--file" in args:
            return type("Done", (), {"returncode": 1, "stdout": "", "stderr": "relay said no"})()
        return real(args, **kw)

    monkeypatch.setattr(series.subprocess, "run", lambda args, **kw: refuse_files(args, **kw))
    first = series.run()
    assert first["failed"][0]["attempt"] == 1 and "relay said no" in first["failed"][0]["error"]
    assert len(short_uploads) == 1 and not _replies(buzz)

    monkeypatch.setattr(series.subprocess, "run", real)
    out = series.run()
    assert out["published"] and len(short_uploads) == 1 and len(_handoffs(buzz)) == 1
    assert "youtube.com/shorts/flowVid0001" in _replies(buzz)["e" * 64]


def test_the_sender_is_told_once_after_three_failed_attempts(env, buzz, short_uploads, monkeypatch):
    def broken(*a, **kw):
        raise youtube.YouTubeError("upload video: HTTP 503")

    monkeypatch.setattr(youtube, "upload", broken)
    buzz["events"] = [_event(_short_request())]
    assert [series.run()["failed"][0]["attempt"] for _ in range(3)] == [1, 2, 3]
    assert "failed" not in series.run()
    (args, kw), = _sends(buzz)
    assert "tras 3 intentos" in kw["input"] and "HTTP 503" in kw["input"]


def test_without_a_shorts_channel_the_short_is_published_and_the_sender_told(env, buzz, short_uploads, monkeypatch):
    monkeypatch.delenv("SHORTS_BUZZ_CHANNEL")
    buzz["events"] = [_event(_short_request())]
    out = series.run()
    assert out["published"][0]["handoff"] is False and not _handoffs(buzz)
    assert "falta SHORTS_BUZZ_CHANNEL" in _replies(buzz)["e" * 64]


def test_check_short_wants_vertical_audio_and_five_to_180_seconds(monkeypatch, tmp_path):
    from plugins.shorts.studio import media

    def probe_as(width, height, seconds, audio=True):
        streams = [{"codec_type": "video", "width": width, "height": height}] + ([{"codec_type": "audio"}] if audio else [])
        monkeypatch.setattr(media, "probe", lambda path: {"streams": streams, "format": {"duration": str(seconds)}})

    probe_as(1080, 1920, 42)
    assert series._check_short(tmp_path / "v.mp4") == 42.0
    for args, why in (((1920, 1080, 42), "vertical"), ((1080, 1920, 42, False), "no audio"),
                      ((1080, 1920, 3), "5-180"), ((1080, 1920, 200), "5-180")):
        probe_as(*args)
        with pytest.raises(series.SeriesError, match=why):
            series._check_short(tmp_path / "v.mp4")


def test_seodesk_optimises_a_flow_short_with_its_folder_name(env, buzz, short_uploads, updates):
    other = {"short": "2026-10-21_es-otro-tema", "es": {"id": "otherVid001", "titulo": "Otro título largo para el short",
                                                        "descripcion": "Una descripción que supera los veinte caracteres.",
                                                        "etiquetas": ["ia"]}}
    body = {"short": FLOW, "es": {"id": "flowVid0001", "titulo": "Tu empleado junior ya es una IA | pymes",
                                  "descripcion": "Qué cambia en tu pyme cuando el trabajo de entrada lo hace una IA. #IA",
                                  "etiquetas": ["ia", "pymes"], "portada": None}}
    mark = series.MARK_UPDATE
    buzz["events"] = [_event(_short_request(), eid="a" * 64, at=1),
                      _event(f"{mark}\n```json\n{json.dumps(body)}\n```", pubkey=SEODESK, eid="b" * 64, at=2),
                      _event(f"{mark}\n```json\n{json.dumps(other)}\n```", pubkey=SEODESK, eid="c" * 64, at=3)]
    out = series.run()
    assert out["updated"] == [{"short": FLOW, "langs": ["es"]}]
    assert [(u["id"], u["short"]) for u in updates] == [("flowVid0001", True)]
    assert "is not a Short uploaded by Hermes" in out["invalid"][0]["error"]
    replies = _replies(buzz)
    assert replies["b" * 64].startswith(f"Short {FLOW} actualizado") and "No puedo actualizar" in replies["c" * 64]


def test_the_socialbot_message_is_unchanged_for_a_studio_short_with_its_story():
    text = series.buzz_share.message({"lang": "es", "title": "T", "request_id": "r", "article_url": "u"},
                                     {"instagram": "ig"}, "https://youtube.com/shorts/x",
                                     ["master.mp4", "story.mp4", "cover.jpg"])
    assert "(Reel; la Story es story.mp4, sin texto)" in text and "Instagram (Reel con cover.jpg como portada, y Story)" in text
