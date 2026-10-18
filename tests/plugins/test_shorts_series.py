"""Tests for the "Agentes BigLobster" series: episode requests from Buzz, published on YouTube.

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
                 "SHORTS_PUBLISH_MODE": "live"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(series.buzz_share, "cli", lambda: "/usr/local/bin/buzz")
    monkeypatch.setattr(series, "_check_video", lambda path: 123.0)
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
            dest.write_bytes(b"\xff\xd8\xff\xe0cover" if "portada" in dest.name else b"video")
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
    assert "is not a blog Short uploaded by Hermes" in out["invalid"][0]["error"]
    replies = {a[a.index("--reply-to") + 1]: kw["input"] for a, kw in _sends(buzz)}
    assert replies["a" * 64].startswith("Short es-limite") and "No puedo actualizar" in replies["b" * 64]


def test_youtube_update_keeps_the_shorts_tag_for_a_short(monkeypatch):
    social = {"youtube": {"title": "¿Dónde pones el límite a la IA?", "description": "d", "tags": []}}
    assert "#Shorts" in youtube.build_metadata(social, "es", synthetic=False, short=True)["snippet"]["title"]
