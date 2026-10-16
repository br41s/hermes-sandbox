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


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path, raising=False)
    for k, v in {"SERIES_BUZZ_CHANNEL": "f4a74eae-a548-56fe-9a2e-f89bfd929f0e", "SERIES_BUZZ_FROM": VIDEODESK,
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
            Path(args[args.index("-o") + 1]).write_bytes(b"video")
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
        vid = f"{lang}VideoId0"[:11]
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
