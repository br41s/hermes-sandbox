"""Tests for the `social` plugin (Facebook Page + Instagram comments inbox).

Meta's Graph API is mocked everywhere (a scripted fake client for the two
functions tested for real, plain monkeypatched stand-ins for the rest) and the
Buzz CLI is a fake subprocess: nothing leaves the machine.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from plugins.social import inbox

IST = timezone(timedelta(hours=7))


def _today() -> str:
    return datetime.now(IST).date().isoformat()


def _comment(comment_id, network, *, answered=False, created="2026-11-13T10:00:00+0000",
             author="Ana", text="Hola"):
    return {"network": network, "comment_id": comment_id, "author": author, "text": text,
            "created": created, "post_url": f"https://example.test/{comment_id}", "answered": answered}


def _write_ledger(home, **data):
    path = home / "workspace" / "social" / "ledger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _read_ledger(home):
    return json.loads((home / "workspace" / "social" / "ledger.json").read_text(encoding="utf-8"))


def _reply(**over):
    args = {"action": "reply", "network": "facebook", "comment_id": "c1", "text": "Gracias por el comentario 🙌"}
    args.update(over)
    return json.loads(inbox.handle_social_inbox(args))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path, raising=False)
    for k, v in {"META_PAGE_ID": "1", "META_PAGE_ACCESS_TOKEN": "tok", "META_IG_USER_ID": "2"}.items():
        monkeypatch.setenv(k, v)
    for k in ("SOCIAL_REPLY_MODE", "SOCIAL_REPLIES_MAX_PER_DAY", "SOCIAL_METRICS_BUZZ_CHANNEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(inbox.buzz_share, "cli", lambda: "/usr/local/bin/buzz")
    return tmp_path


@pytest.fixture
def meta_net(monkeypatch):
    """Fake Meta network calls; ``state`` holds what each one answers and what was sent."""
    state = {"posts": [], "page_comments": [], "media": [], "ig_comments": [], "replies": [], "rows": []}
    monkeypatch.setattr(inbox.meta, "page_posts", lambda limit=25: state["posts"])
    monkeypatch.setattr(inbox.meta, "page_comments", lambda posts: state["page_comments"])
    monkeypatch.setattr(inbox.meta, "ig_media", lambda limit=25: state["media"])
    monkeypatch.setattr(inbox.meta, "ig_comments", lambda media: state["ig_comments"])
    monkeypatch.setattr(inbox.meta, "post_metrics", lambda days=14: state["rows"])

    def fake_reply(network, comment_id, text):
        state["replies"].append((network, comment_id, text))
        return {"id": f"r{len(state['replies'])}"}

    monkeypatch.setattr(inbox.meta, "reply_comment", fake_reply)
    return state


# ---------------------------------------------------------------------------
# new_comments
# ---------------------------------------------------------------------------

def test_new_comments_drops_answered_and_ledgered_ones(env, meta_net):
    _write_ledger(env, comments={"c-done": {"state": "replied"}, "c-shadow": {"state": "shadow"},
                                 "c-skip": {"state": "skipped"}, "c-pending": {"state": "seen"}},
                  replies_by_day={}, metrics_posted=None)
    meta_net["page_comments"] = [
        _comment("c1", "facebook", created="2026-11-14T10:00:00+0000"),
        _comment("c2", "facebook", answered=True, created="2026-11-14T11:00:00+0000"),
        _comment("c-done", "facebook", created="2026-11-14T09:00:00+0000"),
        _comment("c-shadow", "facebook", created="2026-11-14T08:00:00+0000"),
        _comment("c-skip", "facebook", created="2026-11-14T07:00:00+0000"),
        _comment("c-pending", "facebook", created="2026-11-14T06:00:00+0000"),
    ]
    meta_net["ig_comments"] = [
        _comment("i1", "instagram", created="2026-11-14T12:00:00+0000"),
        _comment("i2", "instagram", answered=True, created="2026-11-14T13:00:00+0000"),
    ]

    payload = json.loads(inbox.handle_social_inbox({"action": "new_comments"}))

    assert payload["success"] is True
    assert payload["count"] == 3
    assert [c["comment_id"] for c in payload["comments"]] == ["i1", "c1", "c-pending"]
    assert payload["reply_mode"] == "shadow"
    assert payload["replies_left_today"] == 15


# ---------------------------------------------------------------------------
# reply
# ---------------------------------------------------------------------------

def test_reply_in_shadow_records_and_never_calls_meta(env, meta_net):
    payload = _reply()

    assert payload["success"] is True and payload["shadow"] is True
    assert payload["would_reply"] == "Gracias por el comentario 🙌"
    assert meta_net["replies"] == []
    entry = _read_ledger(env)["comments"]["c1"]
    assert entry["state"] == "shadow" and entry["text"] == "Gracias por el comentario 🙌"


def test_a_shadow_draft_gets_its_real_reply_once_live(env, meta_net, monkeypatch):
    assert _reply()["shadow"] is True
    assert _reply()["success"] is False                      # shadow: drafted once, not again

    monkeypatch.setenv("SOCIAL_REPLY_MODE", "live")
    live = _reply()
    assert live["success"] is True and live["replied"] is True
    assert meta_net["replies"] == [("facebook", "c1", "Gracias por el comentario 🙌")]
    assert _reply()["success"] is False                      # live: never twice


def test_reply_live_calls_meta_once_and_a_second_reply_is_refused(env, meta_net, monkeypatch):
    monkeypatch.setenv("SOCIAL_REPLY_MODE", "live")

    first = _reply()
    assert first["success"] is True and first["replied"] is True
    assert meta_net["replies"] == [("facebook", "c1", "Gracias por el comentario 🙌")]
    data = _read_ledger(env)
    assert data["comments"]["c1"]["state"] == "replied"
    assert data["comments"]["c1"]["reply_id"] == "r1"
    assert data["replies_by_day"][_today()] == 1

    second = _reply()
    assert second["success"] is False and "already" in second["error"]
    assert len(meta_net["replies"]) == 1


def test_the_daily_cap_refuses_at_the_limit(env, meta_net, monkeypatch):
    monkeypatch.setenv("SOCIAL_REPLY_MODE", "live")
    monkeypatch.setenv("SOCIAL_REPLIES_MAX_PER_DAY", "1")

    assert _reply()["success"] is True
    payload = _reply(comment_id="c2")

    assert payload["success"] is False and "cap" in payload["error"]
    assert [call[1] for call in meta_net["replies"]] == ["c1"]


@pytest.mark.parametrize("bad", [
    "escríbeme a ana@example.com y te cuento",
    "mira https://spam.example/oferta",
])
def test_reply_refuses_text_that_breaks_the_guardrails(env, meta_net, monkeypatch, bad):
    monkeypatch.setenv("SOCIAL_REPLY_MODE", "live")

    payload = _reply(text=bad)

    assert payload["success"] is False
    assert meta_net["replies"] == []
    assert not (env / "workspace" / "social" / "ledger.json").exists()


# ---------------------------------------------------------------------------
# skip
# ---------------------------------------------------------------------------

def test_skip_records_the_reason(env, meta_net):
    payload = json.loads(inbox.handle_social_inbox(
        {"action": "skip", "comment_id": "c9", "reason": "Spam de otro bot"}))

    assert payload["success"] is True
    entry = _read_ledger(env)["comments"]["c9"]
    assert entry["state"] == "skipped" and entry["reason"] == "Spam de otro bot"


# ---------------------------------------------------------------------------
# metrics_daily
# ---------------------------------------------------------------------------

ROW = {"fecha": "2026-11-13", "red": "facebook", "enlace": "https://www.facebook.com/1",
       "tipo": "post", "alcance": 10, "vistas": None, "likes": 2, "comentarios": 1,
       "compartidos": 0, "guardados": None}


def test_metrics_daily_posts_once_per_day_and_fails_without_a_channel(env, meta_net, monkeypatch):
    meta_net["rows"] = [ROW]
    sent = []

    def fake_run(args, **kw):
        sent.append((args, kw))
        return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(inbox.subprocess, "run", fake_run)

    missing = json.loads(inbox.handle_social_inbox({"action": "metrics_daily"}))
    assert missing["success"] is False and "SOCIAL_METRICS_BUZZ_CHANNEL" in missing["error"]
    assert sent == [] and not (env / "workspace" / "social" / "ledger.json").exists()

    monkeypatch.setenv("SOCIAL_METRICS_BUZZ_CHANNEL", "chan-1")
    first = json.loads(inbox.handle_social_inbox({"action": "metrics_daily"}))
    assert first["success"] is True and first["posted"] is True and first["rows"] == 1
    args, kw = sent[0]
    assert args[1:5] == ["messages", "send", "--channel", "chan-1"]
    assert args[-2:] == ["--content", "-"]
    body = kw["input"]
    assert body.startswith("📊 Métricas FB/IG últimos 14 días (Hermes, " + _today() + " ICT)")
    assert "| Fecha | Red | Enlace | Tipo | Alcance | Vistas | Likes | Com | Shares | Saves |" in body
    assert "| 2026-11-13 | facebook |" in body
    assert "—" in body
    assert _read_ledger(env)["metrics_posted"] == _today()

    second = json.loads(inbox.handle_social_inbox({"action": "metrics_daily"}))
    assert second["success"] is True and second["already"] is True
    assert len(sent) == 1


def test_metrics_daily_leaves_the_day_unposted_when_the_cli_fails(env, meta_net, monkeypatch):
    monkeypatch.setenv("SOCIAL_METRICS_BUZZ_CHANNEL", "chan-1")
    monkeypatch.setattr(inbox.subprocess, "run", lambda *a, **kw: type(
        "Done", (), {"returncode": 1, "stdout": "", "stderr": "relay refused"})())

    payload = json.loads(inbox.handle_social_inbox({"action": "metrics_daily"}))

    assert payload["success"] is False and "relay refused" in payload["error"]
    assert not (env / "workspace" / "social" / "ledger.json").exists()


# ---------------------------------------------------------------------------
# The real Meta functions, against a scripted client
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, body):
        self.status_code, self._body = 200, body

    def json(self):
        return self._body


class _ErrResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    @property
    def text(self):
        return json.dumps(self._body)

    def json(self):
        return self._body


class _Client:
    """Scripted Graph client: one item per call, ``(status, body)`` is an HTTP error."""

    def __init__(self, script=()):
        self.script, self.calls = list(script), []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, **kw):
        self.calls.append((url, kw))
        item = self.script.pop(0)
        return _ErrResp(*item) if isinstance(item, tuple) else _Resp(item)

    def post(self, url, **kw):
        return self.get(url, **kw)


def test_page_comments_marks_answered_when_the_page_replied(env, monkeypatch):
    from plugins.shorts.social import meta as meta_mod

    client = _Client([{"data": [
        {"id": "c1", "from": {"id": "999", "name": "Ana"}, "message": "Hola",
         "created_time": "2026-11-14T10:00:00+0000", "comments": {"data": [{"from": {"id": "1"}}]}},
        {"id": "c2", "from": {"id": "998", "name": "Luis"}, "message": "Pregunta",
         "created_time": "2026-11-14T09:00:00+0000", "comments": {"data": [{"from": {"id": "999"}}]}},
        {"id": "c3", "from": {"id": "1", "name": "BigLobster"}, "message": "Propio",
         "created_time": "2026-11-14T08:00:00+0000"},
    ]}])
    monkeypatch.setattr(meta_mod, "_client", lambda: client)

    out = meta_mod.page_comments([{"id": "p1", "url": "https://www.facebook.com/p1"}])

    assert [c["comment_id"] for c in out] == ["c1", "c2"]
    assert out[0]["answered"] is True and out[1]["answered"] is False
    assert out[0]["network"] == "facebook" and out[0]["post_url"] == "https://www.facebook.com/p1"
    assert client.calls[0][0].endswith("/p1/comments")
    assert client.calls[0][1]["params"]["filter"] == "toplevel"
    assert client.calls[0][1]["params"]["order"] == "reverse_chronological"


def test_reply_comment_posts_to_the_right_endpoints(env, monkeypatch):
    from plugins.shorts.social import meta as meta_mod

    client = _Client([{"id": "r1"}, {"id": "r2"}])
    monkeypatch.setattr(meta_mod, "_client", lambda: client)

    assert meta_mod.reply_comment("facebook", "c1", "Hola") == {"id": "r1"}
    assert meta_mod.reply_comment("instagram", "c2", "Hola") == {"id": "r2"}
    assert client.calls[0][0].endswith("/c1/comments") and client.calls[1][0].endswith("/c2/replies")
    assert client.calls[0][1]["data"] == {"message": "Hola", "access_token": "tok"}
    with pytest.raises(meta_mod.MetaError):
        meta_mod.reply_comment("tiktok", "c3", "Hola")


class _MetricsClient:
    """Facebook totals answer; every insights metric but ``views`` is refused by Meta."""

    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, **kw):
        self.calls.append((url, kw))
        metric = (kw.get("params") or {}).get("metric", "")
        if metric == "views":
            return _Resp({"data": [{"name": "views", "values": [{"value": 40}]}]})
        if "insights" in url:
            return _ErrResp(400, {"error": {"message": "Unknown metric"}})
        if url.endswith("/p1"):
            return _Resp({"reactions": {"summary": {"total_count": 5}},
                          "comments": {"summary": {"total_count": 2}},
                          "shares": {"count": 1}})
        return _Resp({})


def test_post_metrics_survives_an_insights_meta_error(env, monkeypatch):
    from plugins.shorts.social import meta as meta_mod

    now = datetime.now(UTC)
    posts = [{"id": "p1", "url": "https://www.facebook.com/reel/p1", "text": "Reel",
              "created": (now - timedelta(days=1)).isoformat()}]
    media = [{"id": "m1", "url": "https://www.instagram.com/p/m1/", "text": "Foto",
              "type": "VIDEO", "likes": 7, "comments": 3,
              "created": (now - timedelta(days=2)).isoformat()}]
    monkeypatch.setattr(meta_mod, "page_posts", lambda limit=25: posts)
    monkeypatch.setattr(meta_mod, "ig_media", lambda limit=25: media)
    monkeypatch.setattr(meta_mod, "_client", _MetricsClient)

    rows = meta_mod.post_metrics(14)

    assert [row["red"] for row in rows] == ["facebook", "instagram"]
    facebook, instagram = rows
    assert facebook["likes"] == 5 and facebook["comentarios"] == 2 and facebook["compartidos"] == 1
    assert facebook["alcance"] is None and facebook["vistas"] is None and facebook["tipo"] == "reel"
    assert instagram["likes"] == 7 and instagram["comentarios"] == 3
    assert instagram["vistas"] == 40
    assert instagram["alcance"] is None and instagram["compartidos"] is None and instagram["guardados"] is None
