"""Tests for the text check the social inbox uses and the YouTube client's post-upload writes.

YouTube is faked; nothing leaves the machine.
"""
from __future__ import annotations

import json

import pytest

from plugins.shorts import post_upload
from plugins.shorts.social import youtube

ES = "esVideoId01"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path, raising=False)
    for k, v in {"YOUTUBE_CLIENT_ID": "c", "YOUTUBE_CLIENT_SECRET": "s",
                 "YOUTUBE_REFRESH_TOKEN": "r"}.items():
        monkeypatch.setenv(k, v)
    return tmp_path


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


RENT = "https://biglobster.top/es/agentes-en-alquiler.html?utm_source=youtube&utm_medium=short&utm_campaign=whatsapp"


def _cut_once(http, published):
    """YouTube returns ``published`` as the description on the first read, then what was sent."""
    reads = iter([published])

    def get(url, **kw):
        http.calls.append(("get", url, kw))
        sent = _puts(http)[-1][1]["snippet"]
        return Resp(200, {"items": [{"snippet": {**sent, "description": next(reads, sent["description"])}}]})
    http.get = get


def test_update_resends_a_description_whose_link_youtube_cut(env, monkeypatch):
    http = Http(VIDEO)
    _cut_once(http, "Tu agente de WhatsApp: https://biglobster.top/")
    monkeypatch.setattr(youtube, "_client", lambda: http)
    social = {"youtube": {"title": "Agente de WhatsApp", "description": f"Tu agente de WhatsApp: {RENT}", "tags": []}}
    out = youtube.update(ES, social, "es")
    assert len(_puts(http)) == 2 and _puts(http)[1][1]["snippet"]["description"].endswith(RENT)
    assert out["warnings"] == []


def test_update_warns_when_the_link_stays_cut_and_counts_hashtags(env, monkeypatch):
    http = Http(VIDEO)
    monkeypatch.setattr(youtube, "_client", lambda: http)
    http.get = lambda url, **kw: Resp(200, {"items": [{"snippet": {"description": "https://biglobster.top/"}}]})
    social = {"youtube": {"title": "Agente", "description": f"{RENT} #IA #pymes #WhatsApp #ventas #agentes #automatizacion #atencion #negocio #BigLobster", "tags": []}}
    out = youtube.update(ES, social, "es")
    assert len(_puts(http)) == 2            # one retry, not a loop
    assert out["warnings"] == [f"description link lost on YouTube: {RENT}", "9 hashtags in the description (max 8)"]


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
