"""Tests for the Flow short publishing helpers: the vertical master check, the YouTube Short upload
and the Meta Reels (``META_PUBLISH_MODE=live``, shadow by default).

YouTube and Meta are faked; nothing leaves the machine.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugins.shorts import series
from plugins.shorts.social import youtube

FLOW = "2026-10-21_es-ia-empleados-junior"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path, raising=False)
    for k, v in {"YOUTUBE_CLIENT_ID": "c", "YOUTUBE_CLIENT_SECRET": "s",
                 "YOUTUBE_REFRESH_TOKEN": "r"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(series, "_check_short", lambda path: 42.0)
    return tmp_path


def test_long_video_metadata_has_no_shorts_tag_and_own_privacy():
    social = {"youtube": {"title": "Content Gap Hunter: the article you're missing", "description": "d", "tags": ["a"]}}
    meta = youtube.build_metadata(social, "en", synthetic=False, privacy="unlisted")
    assert "#Shorts" not in meta["snippet"]["title"] and meta["status"]["privacyStatus"] == "unlisted"


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

        def get(self, url, **kw):            # the description read-back: YouTube kept what was sent
            calls.append(("get", url, kw))
            sent = json.loads(next(c for c in reversed(calls) if c[0] == "put")[2]["content"])
            return Resp(200, {"items": [{"snippet": sent["snippet"]}]})

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


def test_a_short_title_and_description_go_out_as_written(monkeypatch):
    social = {"youtube": {"title": "¿Dónde pones el límite a la IA?", "description": "d #IA", "tags": []}}
    snippet = youtube.build_metadata(social, "es", synthetic=False)["snippet"]
    assert snippet["title"] == "¿Dónde pones el límite a la IA?" and snippet["description"] == "d #IA"


# ---------------------------------------------------------------------------
# Flow shorts: the master check and the publish helpers
# ---------------------------------------------------------------------------


def _short_item(**over):
    item = {
        "short": FLOW, "idioma": "es", "articulo": "https://biglobster.top/es/blog/ia-empleados-junior/",
        "titulo": "Tu empleado junior ya es una IA",
        "descripcion": "Qué cambia en tu pyme cuando el trabajo de entrada lo hace una IA.",
        "etiquetas": ["ia", "pymes"],
        "redes": {"instagram": "Texto IG #IA", "facebook": "Texto FB", "x": "Texto X"},
    }
    item.update(over)
    return item


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


def _publish(tmp_path, item=None, entry=None, cover=True, srt=True, report=None):
    """Call ``series._publish_files`` on files on disk, recording every save in ``entry``."""
    item = item or _short_item()
    entry = {} if entry is None else entry
    video = tmp_path / "master.mp4"
    video.write_bytes(b"video")
    cover_path = tmp_path / "cover.jpg"
    if cover:
        cover_path.write_bytes(b"\xff\xd8\xff\xe0cover")
    srt_path = tmp_path / "captions.srt"
    if srt:
        srt_path.write_bytes(b"video")
    report = report if report is not None else {"meta_shadow": []}

    def save():
        entry["saved"] = True

    series._publish_files(item, entry, video, cover_path if cover else None, srt_path if srt else None,
                          save, report)
    return entry, report


def test_a_flow_short_is_uploaded_as_a_youtube_short(env, short_uploads):
    entry, _ = _publish(env)
    (up,) = short_uploads
    assert up["short"] is True and up["synthetic"] is True and up["privacy"] == "public" and up["lang"] == "es"
    assert up["thumb"].startswith(b"\xff\xd8") and up["srt"] == b"video"
    assert "article" not in up and up["social"]["youtube"]["tags"] == ["ia", "pymes"]
    assert entry["youtube"]["id"] == "flowVid0001" and entry["youtube"]["seconds"] == 42.0
    assert series._short_summary(_short_item(), entry) == (
        f"Short {FLOW} publicado en YouTube por Hermes:\n"
        "https://youtube.com/shorts/flowVid0001 (id flowVid0001, public)")


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


# ---------------------------------------------------------------------------
# Meta (Facebook + Instagram): META_PUBLISH_MODE=live, and shadow by default
# ---------------------------------------------------------------------------


def _meta_env(monkeypatch, instagram=True):
    for k, v in {"META_PAGE_ID": "1", "META_PAGE_ACCESS_TOKEN": "tok", "META_PUBLISH_MODE": "live"}.items():
        monkeypatch.setenv(k, v)
    if instagram:
        monkeypatch.setenv("META_IG_USER_ID", "2")
    else:
        monkeypatch.delenv("META_IG_USER_ID", raising=False)


def _meta_calls(monkeypatch):
    """Fake Meta poster: records ("facebook"|"instagram", kwargs) in order and returns a link."""
    from plugins.shorts.social import meta as meta_mod

    calls = []

    def seq(kind):
        return 1 + sum(1 for c in calls if c[0] == kind)

    def fake_reel(video, description, ai_generated=False):
        n = seq("facebook")
        calls.append(("facebook", {"description": description, "ai_generated": ai_generated}))
        return {"id": f"fb{n}", "url": f"https://www.facebook.com/reel/fb{n}"}

    def fake_instagram(video, **kw):
        n = seq("instagram")
        calls.append(("instagram", kw))
        return {"id": f"ig{n}", "url": f"https://www.instagram.com/reel/ig{n}/"}

    monkeypatch.setattr(meta_mod, "facebook_reel", fake_reel)
    monkeypatch.setattr(meta_mod, "instagram", fake_instagram)
    return calls


class _MetaResp:
    def __init__(self, body):
        self.status_code, self._body = 200, body

    def json(self):
        return self._body


class _MetaClient:
    """The Graph calls ``facebook_reel`` and ``instagram`` make, answered from a script."""

    def __init__(self, script):
        self.script, self.calls = list(script), []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return _MetaResp(self.script.pop(0))

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return _MetaResp(self.script.pop(0))


class _MetaErrorResp:
    """An HTTP error from the Graph API, with the body Meta really sends."""

    def __init__(self, status, body):
        self.status_code, self._body = status, body

    @property
    def text(self):
        return json.dumps(self._body)

    def json(self):
        return self._body


class _MetaFlakyClient(_MetaClient):
    """``_MetaClient`` whose script may also hold ``(status, body)``: an HTTP error."""

    def post(self, url, **kw):
        self.calls.append((url, kw))
        item = self.script.pop(0)
        return _MetaErrorResp(*item) if isinstance(item, tuple) else _MetaResp(item)

    def get(self, url, **kw):
        return self.post(url, **kw)


# What rupload.facebook.com answered to about half of the identical uploads (2026-11-13).
PROCESSING_FAILED = (400, {"debug_info": {"retriable": False, "type": "ProcessingFailedError",
                                          "message": "Request processing failed"}})


def test_meta_shadow_by_default_publishes_nothing_to_meta(env, short_uploads, monkeypatch):
    _meta_env(monkeypatch)
    monkeypatch.delenv("META_PUBLISH_MODE")
    calls = _meta_calls(monkeypatch)
    _, report = _publish(env)
    assert calls == []
    assert report["meta_shadow"] == [{"short": FLOW, "facebook": "Texto FB", "instagram": "Texto IG #IA"}]


def test_meta_live_posts_fb_and_ig_reels_after_youtube(env, short_uploads, monkeypatch):
    _meta_env(monkeypatch)
    calls = _meta_calls(monkeypatch)
    upload = youtube.upload

    def ordered_upload(*a, **kw):
        calls.append(("youtube", {}))
        return upload(*a, **kw)

    monkeypatch.setattr(youtube, "upload", ordered_upload)
    entry, _ = _publish(env)
    assert [c[0] for c in calls] == ["youtube", "facebook", "instagram"]
    assert calls[1][1]["description"] == "Texto FB" and calls[2][1]["caption"] == "Texto IG #IA"
    assert calls[2][1]["kind"] == "reel"

    reply = series._short_summary(_short_item(), entry)
    assert "Facebook: https://www.facebook.com/reel/fb1" in reply
    assert "Instagram: https://www.instagram.com/reel/ig1/" in reply


def test_meta_live_ig_failure_asks_the_owner_to_upload_by_hand(env, short_uploads, monkeypatch):
    from plugins.shorts.social import meta as meta_mod

    _meta_env(monkeypatch)
    calls = _meta_calls(monkeypatch)

    def broken(*a, **kw):
        raise meta_mod.MetaError("publish to Instagram: HTTP 500")

    monkeypatch.setattr(meta_mod, "instagram", broken)
    entry, _ = _publish(env)
    assert [c[0] for c in calls] == ["facebook"]
    assert "HTTP 500" in entry["instagram"]["failed"]
    assert "Instagram: Meta rechazó la subida del vídeo; súbelo a mano" in series._short_summary(_short_item(), entry)

    def boom(*a, **kw):
        raise AssertionError("Instagram is not retried once the fallback is recorded")

    monkeypatch.setattr(meta_mod, "instagram", boom)
    _publish(env, entry=entry)                  # a retry does only what is left
    assert [c[0] for c in calls] == ["facebook"] and len(short_uploads) == 1


def test_meta_live_ig_pending_is_resumed_next_run(env, short_uploads, monkeypatch):
    from plugins.shorts.social import meta as meta_mod

    _meta_env(monkeypatch)
    pending = []

    def pending_ig(video, **kw):
        pending.append(kw.get("pending_container"))
        return {"pending": "container-1", "status": "IN_PROGRESS"}

    monkeypatch.setattr(meta_mod, "facebook_reel", lambda video, text, **kw: {"id": "fb1", "url": "https://facebook.com/reel/fb1"})
    monkeypatch.setattr(meta_mod, "instagram", pending_ig)
    entry = {}
    with pytest.raises(series.SeriesPending):
        _publish(env, entry=entry)
    assert entry["instagram"] == {"pending": "container-1"}

    def resume_ig(video, **kw):
        pending.append(kw.get("pending_container"))
        return {"id": "ig1", "url": "https://instagram.com/reel/ig1"}

    monkeypatch.setattr(meta_mod, "instagram", resume_ig)
    _publish(env, entry=entry)
    assert pending == [None, "container-1"]
    assert len(short_uploads) == 1


def test_meta_live_without_instagram_id_posts_only_facebook(env, short_uploads, monkeypatch):
    from plugins.shorts.social import meta as meta_mod

    _meta_env(monkeypatch, instagram=False)
    calls = _meta_calls(monkeypatch)

    def boom(*a, **kw):
        raise AssertionError("Instagram is not configured: it must not be called")

    monkeypatch.setattr(meta_mod, "instagram", boom)
    entry, _ = _publish(env)
    assert [c[0] for c in calls] == ["facebook"]
    assert "Facebook: https://www.facebook.com/reel/fb1" in series._short_summary(_short_item(), entry)


def test_meta_facebook_reel_sends_is_ai_generated_only_when_asked(tmp_path, monkeypatch):
    from plugins.shorts.social import meta as meta_mod

    for k, v in {"META_PAGE_ID": "123", "META_PAGE_ACCESS_TOKEN": "tok"}.items():
        monkeypatch.setenv(k, v)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"abc")

    def finish_data(ai_generated):
        client = _MetaClient([{"video_id": "99", "upload_url": "https://rupload.facebook.com/x/99"},
                              {"success": True}, {"success": True}])
        monkeypatch.setattr(meta_mod, "_client", lambda: client)
        meta_mod.facebook_reel(video, "desc", ai_generated=ai_generated)
        return client.calls[2][1]["data"]

    assert finish_data(True)["is_ai_generated"] == "true"
    assert "is_ai_generated" not in finish_data(False)


def test_meta_instagram_sends_is_ai_generated_only_when_asked(tmp_path, monkeypatch):
    from plugins.shorts.social import meta as meta_mod

    for k, v in {"META_PAGE_ID": "1", "META_PAGE_ACCESS_TOKEN": "t", "META_IG_USER_ID": "2"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(meta_mod, "PROCESS_BUDGET", 0.0)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"abc")

    def container_data(ai_generated):
        client = _MetaClient([{"id": "c1"}, {"success": True}])
        monkeypatch.setattr(meta_mod, "_client", lambda: client)
        assert meta_mod.instagram(video, kind="story", ai_generated=ai_generated)["pending"] == "c1"
        return client.calls[0][1]["data"]

    assert container_data(True)["is_ai_generated"] == "true"
    assert "is_ai_generated" not in container_data(False)


def _ig_env(monkeypatch):
    for k, v in {"META_PAGE_ID": "1", "META_PAGE_ACCESS_TOKEN": "tok", "META_IG_USER_ID": "2"}.items():
        monkeypatch.setenv(k, v)


def _no_sleep(monkeypatch):
    """The waits are recorded, never slept: a retry is not worth 40 s of the suite."""
    from plugins.shorts.social import meta as meta_mod

    slept = []
    monkeypatch.setattr(meta_mod.time, "sleep", slept.append)
    return slept


def _ig_light_copy(monkeypatch):
    """Fake 720p conversion: writes ``b"light"`` beside the video, records each call."""
    from plugins.shorts.social import meta as meta_mod

    calls = []

    def fake(video):
        calls.append(video)
        out = video.with_name("v.ig720.mp4")
        out.write_bytes(b"light")
        return out

    monkeypatch.setattr(meta_mod, "_lighter_copy", fake)
    return calls


def test_meta_instagram_retries_a_processing_failure_with_a_fresh_container(tmp_path, monkeypatch):
    from plugins.shorts.social import meta as meta_mod

    _ig_env(monkeypatch)
    slept = _no_sleep(monkeypatch)
    light_calls = _ig_light_copy(monkeypatch)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"abc")
    client = _MetaFlakyClient([{"id": "c1"}, PROCESSING_FAILED, {"status_code": "IN_PROGRESS"},
                               {"id": "c2"}, PROCESSING_FAILED, {"status_code": "IN_PROGRESS"},
                               {"id": "c3"}, {"success": True}, {"status_code": "FINISHED"},
                               {"id": "media1"}, {"permalink": "https://www.instagram.com/reel/m1/"}])
    monkeypatch.setattr(meta_mod, "_client", lambda: client)

    result = meta_mod.instagram(video, kind="reel", caption="Texto IG")

    assert result["id"] == "media1" and result["url"] == "https://www.instagram.com/reel/m1/"
    uploads = [(url, kw) for url, kw in client.calls if "/ig-api-upload/" in url]
    assert len([url for url, _kw in client.calls if url.endswith("/media")]) == 3
    assert [url.rsplit("/", 1)[-1] for url, _kw in uploads] == ["c1", "c2", "c3"]
    assert [kw["content"] for _url, kw in uploads] == [b"abc", b"light", b"light"]
    assert len(light_calls) == 1
    assert slept == [meta_mod.IG_RETRY_WAIT, meta_mod.IG_RETRY_WAIT]
    assert "Instagram got a 720p copy: the full-resolution upload ran past Meta's processing deadline" \
        in result["warnings"]
    assert not video.with_name("v.ig720.mp4").exists()


def test_meta_instagram_gives_up_after_three_processing_failures(tmp_path, monkeypatch):
    from plugins.shorts.social import meta as meta_mod

    _ig_env(monkeypatch)
    slept = _no_sleep(monkeypatch)
    _ig_light_copy(monkeypatch)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"abc")
    client = _MetaFlakyClient([{"id": "c1"}, PROCESSING_FAILED, {"status_code": "IN_PROGRESS"},
                               {"id": "c2"}, PROCESSING_FAILED, {"status_code": "IN_PROGRESS"},
                               {"id": "c3"}, PROCESSING_FAILED, {"status_code": "IN_PROGRESS"}])
    monkeypatch.setattr(meta_mod, "_client", lambda: client)

    with pytest.raises(meta_mod.MetaError) as exc:
        meta_mod.instagram(video, kind="reel", caption="Texto IG")

    assert str(exc.value).startswith("Instagram upload failed after 3 attempts")
    assert "ProcessingFailedError" in str(exc.value)
    assert len([url for url, _kw in client.calls if "/ig-api-upload/" in url]) == 3
    assert slept == [meta_mod.IG_RETRY_WAIT, meta_mod.IG_RETRY_WAIT, meta_mod.IG_RETRY_WAIT]
    assert not video.with_name("v.ig720.mp4").exists()


def test_meta_instagram_does_not_retry_other_errors(tmp_path, monkeypatch):
    from plugins.shorts.social import meta as meta_mod

    _ig_env(monkeypatch)
    slept = _no_sleep(monkeypatch)
    light_calls = _ig_light_copy(monkeypatch)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"abc")
    client = _MetaFlakyClient([{"id": "c1"}, (400, {"error": {"message": "Invalid OAuth access token."}})])
    monkeypatch.setattr(meta_mod, "_client", lambda: client)

    with pytest.raises(meta_mod.MetaError, match="HTTP 400 Invalid OAuth"):
        meta_mod.instagram(video, kind="reel", caption="Texto IG")

    assert len([url for url, _kw in client.calls if "/ig-api-upload/" in url]) == 1
    assert slept == []
    assert light_calls == []


def test_meta_instagram_stages_the_cover_once_across_retries(tmp_path, monkeypatch):
    from plugins.shorts.social import meta as meta_mod

    _ig_env(monkeypatch)
    _no_sleep(monkeypatch)
    _ig_light_copy(monkeypatch)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"abc")
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff\xe0cover")
    client = _MetaFlakyClient([{"id": "photo1"},
                               {"images": [{"width": 1080, "source": "https://cdn.example/cover.jpg"}]},
                               {"id": "c1"}, PROCESSING_FAILED, {"status_code": "IN_PROGRESS"},
                               {"id": "c2"}, {"success": True},
                               {"status_code": "FINISHED"}, {"id": "media1"},
                               {"permalink": "https://www.instagram.com/reel/m1/"}])
    monkeypatch.setattr(meta_mod, "_client", lambda: client)

    meta_mod.instagram(video, kind="reel", caption="Texto IG", cover=cover)

    assert len([url for url, _kw in client.calls if url.endswith("/photos")]) == 1
    containers = [kw["data"] for url, kw in client.calls if url.endswith("/media")]
    assert [data.get("cover_url") for data in containers] == ["https://cdn.example/cover.jpg"] * 2


def test_meta_instagram_publishes_a_container_that_finished_despite_the_400(tmp_path, monkeypatch):
    from plugins.shorts.social import meta as meta_mod

    _ig_env(monkeypatch)
    slept = _no_sleep(monkeypatch)
    light_calls = _ig_light_copy(monkeypatch)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"abc")
    client = _MetaFlakyClient([{"id": "c1"}, PROCESSING_FAILED, {"status_code": "FINISHED"},
                               {"status_code": "FINISHED"}, {"id": "media1"},
                               {"permalink": "https://www.instagram.com/reel/m1/"}])
    monkeypatch.setattr(meta_mod, "_client", lambda: client)

    result = meta_mod.instagram(video, kind="reel", caption="Texto IG")

    assert result["id"] == "media1" and result["url"] == "https://www.instagram.com/reel/m1/"
    assert len([url for url, _kw in client.calls if url.endswith("/media")]) == 1
    assert len([url for url, _kw in client.calls if "/ig-api-upload/" in url]) == 1
    assert light_calls == []
    assert slept == [meta_mod.IG_RETRY_WAIT]
    assert "Instagram answered ProcessingFailedError but finished processing the upload" in result["warnings"]


def test_meta_instagram_retries_when_the_status_read_after_the_400_fails(tmp_path, monkeypatch):
    import httpx

    from plugins.shorts.social import meta as meta_mod

    _ig_env(monkeypatch)
    _no_sleep(monkeypatch)
    _ig_light_copy(monkeypatch)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"abc")
    client = _MetaFlakyClient([{"id": "c1"}, PROCESSING_FAILED, (500, {"error": {"message": "boom"}}),
                               {"id": "c2"}, PROCESSING_FAILED, "transport",
                               {"id": "c3"}, {"success": True}, {"status_code": "FINISHED"},
                               {"id": "media1"}, {"permalink": "https://www.instagram.com/reel/m1/"}])
    get = client.get

    def flaky_get(url, **kw):
        if client.script and client.script[0] == "transport":
            client.script.pop(0)
            raise httpx.ConnectError("connection reset")
        return get(url, **kw)

    monkeypatch.setattr(client, "get", flaky_get)
    monkeypatch.setattr(meta_mod, "_client", lambda: client)

    result = meta_mod.instagram(video, kind="reel", caption="Texto IG")

    assert result["id"] == "media1"
    assert [url.rsplit("/", 1)[-1] for url, _kw in client.calls if "/ig-api-upload/" in url] == ["c1", "c2", "c3"]


def test_meta_instagram_warns_720p_when_the_light_copy_finished_despite_the_400(tmp_path, monkeypatch):
    from plugins.shorts.social import meta as meta_mod

    _ig_env(monkeypatch)
    _no_sleep(monkeypatch)
    _ig_light_copy(monkeypatch)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"abc")
    client = _MetaFlakyClient([{"id": "c1"}, PROCESSING_FAILED, {"status_code": "IN_PROGRESS"},
                               {"id": "c2"}, PROCESSING_FAILED, {"status_code": "FINISHED"},
                               {"status_code": "FINISHED"}, {"id": "media1"},
                               {"permalink": "https://www.instagram.com/reel/m1/"}])
    monkeypatch.setattr(meta_mod, "_client", lambda: client)

    result = meta_mod.instagram(video, kind="reel", caption="Texto IG")

    assert result["id"] == "media1"
    assert any("720p" in warning for warning in result["warnings"])
    assert not video.with_name("v.ig720.mp4").exists()


def test_meta_instagram_retries_with_the_original_when_the_light_copy_fails(tmp_path, monkeypatch):
    from plugins.shorts.social import meta as meta_mod

    _ig_env(monkeypatch)
    _no_sleep(monkeypatch)
    monkeypatch.setattr(meta_mod, "_lighter_copy", lambda video: None)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"abc")
    client = _MetaFlakyClient([{"id": "c1"}, PROCESSING_FAILED, {"status_code": "IN_PROGRESS"},
                               {"id": "c2"}, {"success": True}, {"status_code": "FINISHED"},
                               {"id": "media1"}, {"permalink": "https://www.instagram.com/reel/m1/"}])
    monkeypatch.setattr(meta_mod, "_client", lambda: client)

    result = meta_mod.instagram(video, kind="reel", caption="Texto IG")

    uploads = [kw["content"] for url, kw in client.calls if "/ig-api-upload/" in url]
    assert uploads == [b"abc", b"abc"]
    assert not any("720p" in warning for warning in result["warnings"])


def test_lighter_copy_returns_none_and_cleans_up_when_ffmpeg_fails(tmp_path, monkeypatch):
    from plugins.shorts.social import meta as meta_mod

    video = tmp_path / "v.mp4"
    video.write_bytes(b"abc")

    def failing_run(args, **kw):
        Path(args[-1]).write_bytes(b"partial")
        raise meta_mod.subprocess.CalledProcessError(1, "ffmpeg")

    monkeypatch.setattr(meta_mod.subprocess, "run", failing_run)

    assert meta_mod._lighter_copy(video) is None
    assert not video.with_name("v.ig720.mp4").exists()


def test_lighter_copy_asks_ffmpeg_for_720_wide(tmp_path, monkeypatch):
    from plugins.shorts.social import meta as meta_mod

    video = tmp_path / "v.mp4"
    video.write_bytes(b"abc")
    seen = {}

    def recording_run(args, **kw):
        seen.update(kw)
        seen["args"] = args
        Path(args[-1]).write_bytes(b"light")

    monkeypatch.setattr(meta_mod.subprocess, "run", recording_run)

    out = meta_mod._lighter_copy(video)

    assert out == video.with_name("v.ig720.mp4") and out.exists()
    assert "scale='min(720,iw)':-2" in seen["args"]
    assert seen["stdin"] is meta_mod.subprocess.DEVNULL


def test_a_flow_short_marks_its_meta_posts_as_ai_generated(env, short_uploads, monkeypatch):
    _meta_env(monkeypatch)
    calls = _meta_calls(monkeypatch)
    _publish(env)
    fb = next(c for c in calls if c[0] == "facebook")
    ig = next(c for c in calls if c[0] == "instagram")
    assert fb[1]["ai_generated"] is True and ig[1]["ai_generated"] is True


def test_meta_live_short_over_90s_skips_facebook(env, short_uploads, monkeypatch):
    monkeypatch.setattr(series, "_check_short", lambda path: 120.0)
    _meta_env(monkeypatch)
    calls = _meta_calls(monkeypatch)
    entry, _ = _publish(env)
    assert [c[0] for c in calls] == ["instagram"]

    assert "Facebook: dura más de 90 s, Reels no lo admite; súbelo a mano" in series._short_summary(_short_item(), entry)


@pytest.mark.parametrize("value,expected", [(None, "shadow"), ("live", "live"), (" LIVE ", "live"),
                                           ("shadow", "shadow"), ("yes", "shadow")])
def test_meta_publish_mode_is_live_only_for_live(monkeypatch, value, expected):
    from plugins.shorts.social import meta as meta_mod

    if value is None:
        monkeypatch.delenv("META_PUBLISH_MODE", raising=False)
    else:
        monkeypatch.setenv("META_PUBLISH_MODE", value)
    assert meta_mod.publish_mode() == expected
