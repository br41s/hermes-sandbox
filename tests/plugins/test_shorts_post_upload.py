"""Tests for the post-upload writes of ``#actualizar-video``: translations, captions, playlist, comment, replies.

Buzz (the ``buzz`` CLI) and YouTube are faked; nothing leaves the machine.
"""
from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest

from plugins.shorts import post_upload, series
from plugins.shorts.social import youtube

RELAY = "https://relay.example"
VIDEODESK = "d8" * 32
SEODESK = "5e" * 32
STRANGER = "ab" * 32
ES = "esVideoId01"
SRT = f"{RELAY}/media/{'8' * 64}.srt"
SRT2 = f"{RELAY}/media/{'9' * 64}.srt"


def _event(content, pubkey=VIDEODESK, eid="e" * 64, at=100):
    return {"id": eid, "pubkey": pubkey, "content": content, "created_at": at}


def _request():
    body = {
        "episodio": "01", "agente": "content-gap-hunter",
        "es": {"video": f"{RELAY}/media/{'1' * 64}.mp4", "srt": None,
               "titulo": "Content Gap Hunter: el artículo que te falta",
               "descripcion": "Cómo trabaja el agente que encuentra los huecos de tu blog.", "etiquetas": ["ia"]},
        "en": {"video": f"{RELAY}/media/{'3' * 64}.mp4", "srt": None,
               "titulo": "Content Gap Hunter: the article you're missing",
               "descripcion": "How the agent that finds your blog's gaps works.", "etiquetas": ["ai"]},
    }
    return f"{series.MARK}\n```json\n{json.dumps(body, ensure_ascii=False)}\n```"


def _post(es=None, **top):
    """An ``#actualizar-video`` for the episode's Spanish video; ``es=None`` leaves the language out."""
    body = {"episodio": "01", "agente": "content-gap-hunter", **top}
    if es is not None:
        body["es"] = {"id": ES, **es}
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
    monkeypatch.setattr(post_upload, "_check_srt", lambda path: None)
    return tmp_path


@pytest.fixture
def buzz(monkeypatch):
    state = {"events": [], "calls": []}

    def fake_run(args, **kw):
        state["calls"].append((args, kw))
        out = "{}"
        if args[1:3] == ["messages", "get"]:
            out = json.dumps(state["events"])
        elif args[1:3] == ["media", "get"]:
            Path(args[args.index("-o") + 1]).write_bytes(b"video")
            out = ""
        return type("Done", (), {"returncode": 0, "stdout": out, "stderr": ""})()

    monkeypatch.setattr(series.subprocess, "run", fake_run)
    return state


@pytest.fixture
def edits(monkeypatch):
    """Fakes for the video uploads and the two ``videos.update`` paths."""
    done = {"update": [], "localize": []}

    def fake_upload(video, thumb, srt, social, lang, **kw):
        return {"id": f"{lang}VideoId01", "url": f"https://www.youtube.com/watch?v={lang}VideoId01",
                "privacy": "public", "warnings": []}

    def fake_update(video_id, social, lang, thumb=None, short=False, localizations=None):
        done["update"].append({"id": video_id, "title": social["youtube"]["title"], "localizations": localizations})
        return {"id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}", "thumbnail": False,
                "warnings": [], **({"localized": sorted(localizations)} if localizations else {})}

    def fake_localize(video_id, lang, localizations):
        done["localize"].append({"id": video_id, "localizations": localizations})
        return {"id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}",
                "localized": sorted(localizations), "warnings": []}

    monkeypatch.setattr(youtube, "upload", fake_upload)
    monkeypatch.setattr(youtube, "update", fake_update)
    monkeypatch.setattr(youtube, "localize", fake_localize)
    return done


class FakeYouTube:
    """A small channel behind ``youtube.api()``: every call is recorded, a few resources are kept."""

    def __init__(self):
        self.calls = []
        self.fail = {}                  # resource -> error text
        self.tracks = set()             # caption languages the video already has
        self.captions = []              # (video, language) inserted
        self.playlists = {}             # id -> title
        self.items = set()              # (playlist, video)
        self.threads = {}               # video -> authors of its top-level comments
        self.comments = {}              # id -> snippet
        self.answers = {}               # comment id -> authors of its replies
        self.posted = []                # bodies of commentThreads.insert
        self.replies = []               # bodies of comments.insert

    def insert_caption(self, video_id, lang, srt):
        self.calls.append(("POST", "captions", video_id, lang))
        self.captions.append((video_id, lang))
        self.tracks.add(lang)
        return {}

    def call(self, method, resource, params=None, body=None):
        self.calls.append((method, resource, params, body))
        if resource in self.fail:
            raise youtube.YouTubeError(self.fail[resource])
        params = params or {}
        if (method, resource) == ("GET", "channels"):
            return {"items": [{"id": "UCme"}]}
        if (method, resource) == ("GET", "captions"):
            return {"items": [{"snippet": {"language": lang, "trackKind": "standard"}} for lang in self.tracks]}
        if (method, resource) == ("GET", "playlists"):
            return {"items": [{"id": pid, "snippet": {"title": title}} for pid, title in self.playlists.items()]}
        if (method, resource) == ("POST", "playlists"):
            pid = f"PL{len(self.playlists) + 1}"
            self.playlists[pid] = body["snippet"]["title"]
            return {"id": pid}
        if (method, resource) == ("GET", "playlistItems"):
            return {"items": [{"id": "i"}] if (params["playlistId"], params["videoId"]) in self.items else []}
        if (method, resource) == ("POST", "playlistItems"):
            self.items.add((body["snippet"]["playlistId"], body["snippet"]["resourceId"]["videoId"]))
            return {"id": "item"}
        if (method, resource) == ("GET", "commentThreads"):
            return {"items": [{"snippet": {"topLevelComment": {"snippet": {"authorChannelId": {"value": a}}}}}
                              for a in self.threads.get(params["videoId"], [])]}
        if (method, resource) == ("POST", "commentThreads"):
            self.posted.append(body)
            self.threads.setdefault(body["snippet"]["videoId"], []).append("UCme")
            return {"id": "UgThread0001"}
        if (method, resource) == ("GET", "comments") and "id" in params:
            return {"items": [{"snippet": self.comments[params["id"]]}] if params["id"] in self.comments else []}
        if (method, resource) == ("GET", "comments"):
            return {"items": [{"snippet": {"authorChannelId": {"value": a}}} for a in self.answers.get(params["parentId"], [])]}
        if (method, resource) == ("POST", "comments"):
            self.replies.append(body)
            self.answers.setdefault(body["snippet"]["parentId"], []).append("UCme")
            return {"id": f"UgReply{len(self.replies):04d}"}
        raise AssertionError(f"unexpected call {method} {resource}")


@pytest.fixture
def yt(monkeypatch):
    fake = FakeYouTube()

    @contextlib.contextmanager
    def api():
        yield fake

    monkeypatch.setattr(youtube, "api", api)
    return fake


def _fan(video=ES, author="UCfan"):
    return {"videoId": video, "authorChannelId": {"value": author}}


def _scenario(buzz, *requests):
    """The episode is published, then SEODesk sends each request."""
    buzz["events"] = [_event(_request(), eid="a" * 64, at=1)] + [
        _event(r, pubkey=SEODESK, eid=chr(ord("b") + n) * 64, at=2 + n) for n, r in enumerate(requests)]


def _replies(buzz):
    return {a[a.index("--reply-to") + 1]: kw["input"] for a, kw in buzz["calls"]
            if a[1:3] == ["messages", "send"] and "--reply-to" in a}


def _sent_to(buzz, eid):
    return [kw["input"] for a, kw in buzz["calls"]
            if a[1:3] == ["messages", "send"] and a[a.index("--reply-to") + 1] == eid]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Más en https://biglobster.top/es/blog/ia/ y www.biglobster.top.",
    "https://blog.biglobster.top/a, biglobster.top/es",
    "Sin enlaces, e.g. 3.5 veces más rápido.",
])
def test_links_on_biglobster_top_are_allowed(text):
    assert post_upload._link_problem(text) == ""


@pytest.mark.parametrize("text", [
    "https://evil.example/x", "http://biglobster.top/x", "https://biglobster.top.evil.example/a",
    "https://user@biglobster.top/x", "https://biglobster.top:8443/x", "mira evil.com", "www.evil.example",
    "escríbeme a a@b.com", "javascript:alert(1)", "https://example.com/biglobster.top",
])
def test_any_other_link_or_address_is_refused(text):
    assert post_upload._link_problem(text)


def test_parse_update_accepts_post_upload_keys_without_a_rewrite(env):
    item = series.parse_update(_post({
        "localizaciones": {"id": {"titulo": "Artikel yang hilang", "descripcion": "Cara kerjanya."}},
        "subtitulos": {"id": SRT}, "lista": " Agentes   BigLobster ",
        "comentario": "¿Qué agente quieres ver? https://biglobster.top/es/agentes/"},
        respuestas=[{"comentario_id": "UgComment001", "texto": "Gracias."}]))
    es = item["es"]
    assert "titulo" not in es and es["lista"] == "Agentes BigLobster" and es["subtitulos"] == {"id": SRT}
    assert es["localizaciones"] == {"id": {"title": "Artikel yang hilang", "description": "Cara kerjanya."}}
    assert item["respuestas"] == [{"comentario_id": "UgComment001", "texto": "Gracias."}]


def test_the_request_level_keys_apply_to_each_language_and_the_own_key_wins(env):
    body = {"episodio": "01", "agente": "content-gap-hunter", "lista": "Serie",
            "es": {"id": ES, "lista": "Serie ES"}, "en": {"id": "enVideoId01"}}
    item = series.parse_update(f"{series.MARK_UPDATE}\n{json.dumps(body)}")
    assert (item["es"]["lista"], item["en"]["lista"]) == ("Serie ES", "Serie")


def test_parse_update_lists_every_problem_of_the_new_keys(env):
    with pytest.raises(series.SeriesError) as exc:
        series.parse_update(_post({
            "localizaciones": {"id": {"titulo": "<b>x</b>", "descripcion": ""}, "No Code": {}},
            "subtitulos": {"fil": f"{RELAY}/media/{'8' * 64}.mp4", "id": "https://evil.example/media/x.srt"},
            "lista": "ab", "comentario": "mira https://evil.example/oferta"},
            respuestas=[{"comentario_id": "UgA.reply", "texto": "x@y.com"}, {"comentario_id": "UgA.reply", "texto": "ok"}]))
    text = str(exc.value)
    for fragment in ("es.localizaciones.id.descripcion", "es.localizaciones.id: YouTube rejects < and >",
                     "es.localizaciones.No Code", "es.subtitulos.fil",
                     "es.subtitulos.id", "es.lista", "es.comentario", "evil.example", "respuestas[1].comentario_id",
                     "respuestas[1].texto", "once per request"):
        assert fragment in text


def test_an_entry_with_a_title_still_needs_the_whole_rewrite(env):
    with pytest.raises(series.SeriesError) as exc:
        series.parse_update(_post({"titulo": "El artículo que te falta | CGH", "lista": "Serie"}))
    assert "es.descripcion" in str(exc.value)
    with pytest.raises(series.SeriesError, match="es.portada"):
        series.parse_update(_post({"lista": "Serie", "portada": f"{RELAY}/media/{'4' * 64}.jpg"}))
    with pytest.raises(series.SeriesError, match="es.titulo"):          # no new key: the old rule
        series.parse_update(_post({"etiquetas": ["x"]}))


def test_replies_are_capped_per_request(env, monkeypatch):
    monkeypatch.setenv("SERIES_REPLIES_MAX_PER_RUN", "2")
    replies = [{"comentario_id": f"UgComment00{n}", "texto": "Gracias."} for n in (1, 2, 3)]
    with pytest.raises(series.SeriesError, match="1 to 2"):
        series.parse_update(_post(respuestas=replies))


def test_check_srt_wants_small_utf8_subtitles(tmp_path):
    good =tmp_path / "a.srt"
    good.write_text("1\n00:00:00,000 --> 00:00:01,000\nHola\n", encoding="utf-8")
    post_upload._check_srt(good)
    for name, raw in (("big.srt", b"1\n0 --> 1\n" + b"x" * post_upload.MAX_SRT_BYTES),
                      ("latin.srt", "0 --> 1 é".encode("latin-1")), ("html.srt", b"<html></html>")):
        (tmp_path / name).write_bytes(raw)
        with pytest.raises(series.SeriesError):
            post_upload._check_srt(tmp_path / name)


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

def test_translations_travel_with_the_rewrite_and_cost_one_update(env, buzz, edits, yt):
    loc = {"id": {"titulo": "Artikel yang hilang", "descripcion": "Cara kerjanya."}}
    _scenario(buzz, _post({"titulo": "El artículo que te falta | CGH", "descripcion": "Cómo trabaja el agente.",
                           "etiquetas": ["ia"], "localizaciones": loc}))
    out = series.run()
    assert edits["update"] == [{"id": ES, "title": "El artículo que te falta | CGH",
                                "localizations": {"id": {"title": "Artikel yang hilang", "description": "Cara kerjanya."}}}]
    assert not edits["localize"] and not yt.calls
    assert out["updated"][0]["langs"] == ["es"] and "extras" in out["updated"][0]
    (reply,) = _sent_to(buzz, "b" * 64)
    assert "título, descripción y etiquetas · localizaciones (id)" in reply
    assert post_upload.spent_today() == post_upload.WRITE + post_upload.READ


def test_translations_alone_leave_the_title_and_description(env, buzz, edits, yt):
    _scenario(buzz, _post({"localizaciones": {"id": {"titulo": "Artikel", "descripcion": "Cara kerjanya."}}}))
    series.run()
    assert not edits["update"] and [c["id"] for c in edits["localize"]] == [ES]
    assert "ES: https://www.youtube.com/watch?v=esVideoId01 · localizaciones (id)" in _sent_to(buzz, "b" * 64)[0]


def test_a_language_with_a_track_is_skipped_and_the_others_added_once(env, buzz, edits, yt):
    yt.tracks = {"fil"}
    _scenario(buzz, _post({"subtitulos": {"id": SRT, "fil": SRT2}}), _post({"subtitulos": {"id": SRT}}))
    series.run()
    assert yt.captions == [(ES, "id")]
    first, second = (_sent_to(buzz, "b" * 64)[0], _sent_to(buzz, "c" * 64)[0])
    assert "subtítulos (id)" in first and "ya existían, sin tocar (fil)" in first
    assert "ya existían, sin tocar (id)" in second
    assert post_upload.spent_today() == post_upload.CAPTION_LIST + post_upload.CAPTION_INSERT + post_upload.CAPTION_LIST
    series.run()
    assert yt.captions == [(ES, "id")] and len(_sent_to(buzz, "b" * 64)) == 1


def test_the_playlist_is_created_once_and_the_video_never_added_twice(env, buzz, edits, yt):
    _scenario(buzz, _post({"lista": "Agentes BigLobster"}), _post({"lista": "agentes  biglobster"}))
    series.run()
    assert list(yt.playlists.values()) == ["Agentes BigLobster"] and yt.items == {("PL1", ES)}
    assert [c[1] for c in yt.calls if c[0] == "POST"] == ["playlists", "playlistItems"]
    assert "creada y vídeo añadido" in _sent_to(buzz, "b" * 64)[0] and "ya estaba" in _sent_to(buzz, "c" * 64)[0]


def test_an_existing_playlist_is_reused(env, buzz, edits, yt):
    yt.playlists = {"PLold": "Agentes BigLobster"}
    _scenario(buzz, _post({"lista": "Agentes BigLobster"}))
    series.run()
    assert yt.items == {("PLold", ES)} and list(yt.playlists) == ["PLold"]
    assert "vídeo añadido" in _sent_to(buzz, "b" * 64)[0]


def test_a_failed_add_does_not_create_the_playlist_again(env, buzz, edits, yt):
    yt.fail = {"playlistItems": "POST playlistItems: HTTP 500"}
    _scenario(buzz, _post({"lista": "Agentes BigLobster"}))
    assert series.run()["failed"][0]["attempt"] == 1
    yt.fail = {}
    yt.playlists = {}               # YouTube's list lags: the id is remembered, not looked up again
    assert series.run()["updated"] and yt.items == {("PL1", ES)}
    assert [c[1] for c in yt.calls if c[0] == "POST"].count("playlists") == 1


def test_the_comment_is_posted_once_per_video_and_the_sender_pins_it(env, buzz, edits, yt):
    _scenario(buzz, _post({"comentario": "¿Qué agente quieres ver? biglobster.top/es"}),
              _post({"comentario": "Otro comentario"}))
    series.run()
    assert [b["snippet"]["topLevelComment"]["snippet"]["textOriginal"] for b in yt.posted] == [
        "¿Qué agente quieres ver? biglobster.top/es"]
    assert "fíjalo tú en Studio" in _sent_to(buzz, "b" * 64)[0]
    assert "comentario no publicado: Hermes ya comentó este vídeo" in _sent_to(buzz, "c" * 64)[0]


def test_a_video_the_channel_already_commented_gets_no_second_comment(env, buzz, edits, yt):
    yt.threads = {ES: ["UCfan", "UCme"]}
    _scenario(buzz, _post({"comentario": "Hola"}))
    series.run()
    assert not yt.posted and "el canal ya tiene un comentario" in _sent_to(buzz, "b" * 64)[0]


def test_replies_go_only_to_open_comments_on_hermes_videos(env, buzz, edits, yt):
    yt.comments = {"UgComment001": _fan(), "UgForeign001": _fan("zzzzzzzzzzz"), "UgAnswered01": _fan(),
                   "UgOwnText001": _fan(author="UCme"), "UgReplyTo001": {**_fan(), "parentId": "UgComment001"}}
    yt.answers = {"UgAnswered01": ["UCme"]}
    ids = ["UgComment001", "UgForeign001", "UgAnswered01", "UgOwnText001", "UgReplyTo001", "UgMissing001"]
    _scenario(buzz, _post(respuestas=[{"comentario_id": i, "texto": f"Respuesta a {i}"} for i in ids]))
    out = series.run()
    assert [r["snippet"]["parentId"] for r in yt.replies] == ["UgComment001"]
    assert yt.replies[0]["snippet"]["textOriginal"] == "Respuesta a UgComment001"
    (reply,) = _sent_to(buzz, "b" * 64)
    assert "Respuestas: 1 publicadas; omitidas:" in reply
    for why in ("no es de un vídeo subido por Hermes", "ya respondió", "es del propio canal",
                "es una respuesta, no un comentario principal", "no encuentro ese comentario"):
        assert why in reply
    assert out["replies"] == 1


def test_a_comment_answered_by_an_earlier_request_is_not_answered_again(env, buzz, edits, yt):
    yt.comments = {"UgComment001": _fan()}
    one = [{"comentario_id": "UgComment001", "texto": "Gracias."}]
    _scenario(buzz, _post(respuestas=one), _post(respuestas=one))
    series.run()
    assert len(yt.replies) == 1 and "ya respondido" in _sent_to(buzz, "c" * 64)[0]


def test_the_replies_per_run_are_capped_and_the_rest_wait_for_the_next_run(env, buzz, edits, yt, monkeypatch):
    monkeypatch.setenv("SERIES_REPLIES_MAX_PER_RUN", "2")
    yt.comments = {f"UgComment00{n}": _fan() for n in (1, 2, 3)}
    reply = lambda *ns: _post(respuestas=[{"comentario_id": f"UgComment00{n}", "texto": "Gracias."} for n in ns])
    _scenario(buzz, reply(1, 2), reply(3))
    first = series.run()
    assert len(yt.replies) == 2 and first["replies"] == 2
    assert first["waiting"][0]["why"].startswith("1 respuestas y esta ejecución sólo admite 0 más")
    assert "aplazado" in _sent_to(buzz, "c" * 64)[0]
    second = series.run()
    assert len(yt.replies) == 3 and second["replies"] == 1 and len(_sent_to(buzz, "c" * 64)) == 2


def test_nothing_is_written_in_shadow_mode_or_for_a_stranger(env, buzz, edits, yt, monkeypatch):
    _scenario(buzz, _post({"lista": "Serie", "comentario": "Hola"}))
    buzz["events"][1]["pubkey"] = STRANGER
    series.run()
    assert not yt.calls
    monkeypatch.setenv("SHORTS_PUBLISH_MODE", "shadow")
    buzz["events"][1]["pubkey"] = SEODESK
    assert "shadow" in series.run()["skipped"] and not yt.calls


def test_a_request_that_does_not_fit_today_waits_and_the_sender_is_told_once(env, buzz, edits, yt):
    _scenario(buzz, _post({"comentario": "Hola"}))
    post_upload.spend(7_000)
    out = series.run()
    assert out["waiting"][0]["why"].startswith("cuesta ~52 unidades y hoy ya van ~")
    series.run()
    assert not yt.calls and len(_sent_to(buzz, "b" * 64)) == 1 and "aplazado" in _sent_to(buzz, "b" * 64)[0]


def test_a_request_that_can_never_fit_is_refused(env, buzz, edits, yt, monkeypatch):
    monkeypatch.setattr(post_upload, "DAILY_UNITS", 2_500)
    _scenario(buzz, _post({"subtitulos": {"id": SRT, "fil": SRT2}}))
    out = series.run()
    assert out["invalid"] and not yt.calls
    assert "No aplico este cambio: costaría ~850 unidades" in _sent_to(buzz, "b" * 64)[0]


def test_a_quota_refusal_costs_an_attempt_only_to_its_own_request(env, buzz, edits, yt):
    yt.fail = {"commentThreads": "GET commentThreads: HTTP 403 quotaExceeded"}
    _scenario(buzz, _post({"comentario": "Hola"}), _post({"lista": "Serie"}))
    runs = [series.run() for _ in range(4)]
    assert [r["failed"][0]["attempt"] for r in runs[:3]] == [1, 2, 3]
    assert all(r["waiting"] for r in runs[:3]) and "failed" not in runs[3] and runs[3]["updated"]
    (final,) = [t for t in _sent_to(buzz, "b" * 64)]
    assert "tras 3 intentos" in final and "quotaExceeded" in final
    assert [c[1] for c in yt.calls].count("commentThreads") == 3
    assert yt.items == {("PL1", ES)}


def test_the_steps_done_before_a_failure_are_kept_and_reported(env, buzz, edits, yt):
    yt.fail = {"commentThreads": "GET commentThreads: HTTP 403 commentsDisabled"}
    _scenario(buzz, _post({"lista": "Serie", "comentario": "Hola"}))
    for _ in range(3):
        series.run()
    (final,) = _sent_to(buzz, "b" * 64)
    assert "es.comentario" in final and "commentsDisabled" in final and "lista «Serie»: creada y vídeo añadido" in final
    assert len(yt.items) == 1 and len(yt.playlists) == 1


# ---------------------------------------------------------------------------
# The YouTube client
# ---------------------------------------------------------------------------

class Resp:
    def __init__(self, code, body=None):
        self.status_code, self._body, self.text = code, body or {}, json.dumps(body or {})

    def json(self):
        return self._body


class Http:
    """An httpx client with one video on the channel."""

    def __init__(self, video, status=200):
        self.video, self.status, self.calls = video, status, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, **kw):
        self.calls.append(("post", url, kw))
        return Resp(200, {"access_token": "t"} if "token" in url else {})

    def get(self, url, **kw):
        self.calls.append(("get", url, kw))
        return Resp(200, {"items": [self.video]})

    def put(self, url, **kw):
        self.calls.append(("put", url, kw))
        return Resp(200, {"id": "esVideoId0"})

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return Resp(self.status, {"error": {"errors": [{"reason": "quotaExceeded"}]}} if self.status != 200 else {"items": []})


VIDEO = {"snippet": {"title": "Old", "description": "d", "tags": ["a"], "categoryId": "27", "defaultLanguage": "es",
                     "channelId": "UC", "thumbnails": {}, "localized": {}},
         "localizations": {"de": {"title": "Alt", "description": "Beschreibung"}}}


def _puts(http):
    return [(c[2]["params"], json.loads(c[2]["content"])) for c in http.calls if c[0] == "put"]


def test_update_adds_localizations_to_the_ones_the_video_has(env, monkeypatch):
    http = Http(VIDEO)
    monkeypatch.setattr(youtube, "_client", lambda: http)
    social = {"youtube": {"title": "El artículo que te falta", "description": "0:00 Inicio", "tags": ["seo"]}}
    out = youtube.update(ES, social, "es", localizations={"id": {"title": "Artikel", "description": "Cara"},
                                                          "es": {"title": "no", "description": "no"}})
    ((params, body),) = _puts(http)
    assert params == {"part": "snippet,localizations"}
    assert body["localizations"] == {"de": VIDEO["localizations"]["de"], "id": {"title": "Artikel", "description": "Cara"}}
    assert body["snippet"]["title"] == "El artículo que te falta" and body["snippet"]["defaultLanguage"] == "es"
    assert out["localized"] == ["id"] and "own language (es)" in out["warnings"][0]


def test_localize_sends_the_snippet_back_complete_and_writable_only(env, monkeypatch):
    http = Http(VIDEO)
    monkeypatch.setattr(youtube, "_client", lambda: http)
    out = youtube.localize(ES, "es", {"fil": {"title": "Artikulo", "description": "Paano"}})
    ((params, body),) = _puts(http)
    assert params == {"part": "snippet,localizations"} and out["localized"] == ["fil"]
    assert body["snippet"] == {"title": "Old", "description": "d", "tags": ["a"], "categoryId": "27", "defaultLanguage": "es"}
    assert set(body["localizations"]) == {"de", "fil"}

    only_own = youtube.localize(ES, "es", {"es": {"title": "x", "description": "y"}})
    assert only_own["localized"] == [] and len(_puts(http)) == 1


def test_api_call_builds_the_request_and_names_the_refusal(env, monkeypatch):
    http = Http(VIDEO)
    monkeypatch.setattr(youtube, "_client", lambda: http)
    with youtube.api() as api:
        assert api.call("POST", "comments", {"part": "snippet"}, {"snippet": {"parentId": "x"}}) == {"items": []}
        method, url, kw = http.calls[-1]
        assert (method, url) == ("POST", f"{youtube.API_URL}/comments") and kw["params"] == {"part": "snippet"}
        assert json.loads(kw["content"]) == {"snippet": {"parentId": "x"}}
        assert kw["headers"]["Authorization"] == "Bearer t" and "json" in kw["headers"]["Content-Type"]
        http.status = 403
        with pytest.raises(youtube.YouTubeError, match="quotaExceeded"):
            api.call("GET", "playlists", {"mine": "true"})
