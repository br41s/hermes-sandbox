"""Tests for the Shorts Studio: package contract, grounding, ledger, the
``shorts_studio`` tool, the render-side helpers and the publishers.

Network (GitHub, YouTube, Meta, article fetches) is mocked throughout. The
ffmpeg-backed tests run only where ffmpeg is installed; the Remotion render
itself is proven by the workflow's smoke render, not here.
"""

from __future__ import annotations

import base64
import copy
import io
import json
import re
import shutil
import zipfile
from pathlib import Path

import pytest

from plugins.shorts import github_studio, ledger
from plugins.shorts import studio_tool as st
from plugins.shorts.studio import article as article_mod
from plugins.shorts.studio import build as build_mod
from plugins.shorts.studio import genvideo
from plugins.shorts.studio import presenter as presenter_mod
from plugins.shorts.studio import package as pkg_mod
from plugins.shorts.studio import voice as voice_mod

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "shorts" / "studio" / "samples" / "en-sample.json"
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

ARTICLE_TEXT = (
    "When should a small business adopt AI? Seventy-three percent — 73% — of small firms "
    "saw time savings within 3 months of automating one process. " * 8
)


@pytest.fixture
def sample():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


TIP_SAMPLE = REPO / "shorts" / "studio" / "samples" / "es-sample.json"


@pytest.fixture
def tip():
    """The tip format, the only one the studio tool submits: the Spanish sample."""
    return json.loads(TIP_SAMPLE.read_text(encoding="utf-8"))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path, raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# Package contract
# ---------------------------------------------------------------------------

def test_sample_package_is_valid(sample):
    pkg = pkg_mod.validate_package(sample)
    assert pkg["voice"] == "en-US-ChristopherNeural"
    assert pkg["beats"][0]["kind"] == "hook" and pkg["beats"][-1]["kind"] == "cta"


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda p: p["beats"].__setitem__(0, {**p["beats"][0], "kind": "point"}), "first beat must be kind 'hook'"),
        (lambda p: p["beats"][-1].__setitem__("url", "https://evil.example/x"), "must be on the article's own site"),
        (lambda p: p["style"].__setitem__("palette", "rainbow"), "style.palette"),
        (lambda p: p["beats"][1].__setitem__("onscreen", "one two three four five six seven eight nine"), "max 8"),
        (lambda p: p.__setitem__("lang", "fr"), "lang: must be one of"),
        (lambda p: p.__setitem__("voice", "es-ES-AlvaroNeural"), "does not speak lang"),
        (lambda p: p["social"].__setitem__("x", "x" * 281), "social.x"),
        (lambda p: p["beats"][3].__setitem__("value", "lots"), "a stat needs a number"),
        (lambda p: p.__setitem__("beats", p["beats"][:2] + p["beats"][-1:]), "need 4-10"),
    ],
)
def test_package_problems_are_all_reported(sample, mutate, expected):
    mutate(sample)
    with pytest.raises(pkg_mod.PackageError) as excinfo:
        pkg_mod.validate_package(sample)
    assert any(expected in p for p in excinfo.value.problems), excinfo.value.problems


def test_word_count_walls(sample):
    for beat in sample["beats"]:
        beat["vo"] = "Short line here."
    with pytest.raises(pkg_mod.PackageError) as excinfo:
        pkg_mod.validate_package(sample)
    assert any("spoken words in total" in p for p in excinfo.value.problems)


def test_palettes_and_motifs_match_the_remotion_theme():
    theme = (REPO / "shorts/studio/remotion/src/theme.ts").read_text(encoding="utf-8")
    ts_palettes = re.findall(r"^\s+'([a-z]+(?:-[a-z]+)?)': \{", theme, re.M)
    ts_motifs = re.search(r"MOTIFS = \[(.*?)\]", theme).group(1)
    assert tuple(ts_palettes) == pkg_mod.PALETTES
    assert tuple(re.findall(r"'([a-z]+)'", ts_motifs)) == pkg_mod.MOTIFS


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------

def test_figures_ignore_counts_but_catch_claims():
    assert pkg_mod.figures("3 mistakes and step 2") == []
    assert pkg_mod.figures("73% of firms") == ["73"]
    assert pkg_mod.figures("€1,200 a month, 3x faster") == ["1200", "3"]
    assert pkg_mod.figures("3 Months later") == []  # not a multiplier


def test_grounding_flags_an_invented_number(sample):
    pkg = pkg_mod.validate_package(sample)
    assert pkg_mod.check_grounding(pkg, ARTICLE_TEXT) == []
    pkg["beats"][3]["value"] = "81%"
    problems = pkg_mod.check_grounding(pkg, ARTICLE_TEXT)
    assert problems and "'81'" in problems[0]


def test_grounding_accepts_spaced_percent_and_thousands():
    pkg = {"beats": [{"vo": "It costs €1.200 and 45 % fail."}]}
    assert pkg_mod.check_grounding(pkg, "Costs 1,200 euros; 45% of projects fail.") == []


# ---------------------------------------------------------------------------
# Article extraction
# ---------------------------------------------------------------------------

def test_html_to_text_prefers_the_article_body():
    body = "Real article sentence with 73% in it. " * 20
    html = (
        "<html><head><title>T</title><script>var x=1</script></head><body>"
        "<nav>Menu 999%</nav><article class='article-body container'>"
        f"<h1>Title</h1><p>{body}</p></article><footer>© 2026</footer></body></html>"
    )
    title, text = article_mod.html_to_text(html)
    assert title == "T"
    assert "73%" in text and "999%" not in text and "var x" not in text


# ---------------------------------------------------------------------------
# Voice timing, karaoke pages, SRT
# ---------------------------------------------------------------------------

def test_spread_words_covers_the_line_in_order():
    words = voice_mod.spread_words("one two three four", 2.0)
    assert [w["text"] for w in words] == ["one", "two", "three", "four"]
    assert all(a["end"] <= b["start"] for a, b in zip(words, words[1:]))
    assert words[-1]["end"] < 2.0


def test_attach_raw_restores_punctuation():
    words = [{"text": "Probably", "start": 0, "end": 0.3}, {"text": "not", "start": 0.3, "end": 0.5}]
    voice_mod.attach_raw(words, "Probably not.")
    assert [w["raw"] for w in words] == ["Probably", "not."]


def _abs_words(spec):
    return [{"text": t, "raw": r, "startMs": s, "endMs": e, "beat": b} for t, r, s, e, b in spec]


def test_paginate_breaks_on_beat_pause_and_size():
    words = _abs_words([
        ("a", "a", 0, 100, 0), ("b", "b", 110, 200, 0), ("c", "c", 900, 1000, 0),
        ("d", "d", 1010, 1100, 1),
    ])
    voice_mod.paginate(words)
    assert [w["page"] for w in words] == [0, 0, 1, 2]


def test_srt_lines_break_on_sentences_and_never_overlap():
    words = _abs_words([
        ("Probably", "Probably", 0, 300, 0), ("not", "not.", 320, 500, 0),
        ("But", "But", 520, 700, 0), ("wait", "wait.", 710, 900, 0),
    ])
    srt = voice_mod.build_srt(words)
    assert "Probably not.\n" in srt and "But wait.\n" in srt
    times = re.findall(r"(\d\d:\d\d:\d\d,\d{3}) --> (\d\d:\d\d:\d\d,\d{3})", srt)
    assert times[0][1] < times[1][0]


# ---------------------------------------------------------------------------
# Build helpers (pure)
# ---------------------------------------------------------------------------

def test_story_cut_is_whole_video_when_it_fits_else_a_beat_boundary():
    tl = [{"from": 0, "duration": 900}, {"from": 900, "duration": 800}, {"from": 1700, "duration": 300}]
    assert build_mod.story_cut_point(tl, 50.0) == 50.0
    assert build_mod.story_cut_point(tl, 2000 / 30) == pytest.approx(1700 / 30)


def test_remotion_props_numbers_middle_beats_and_carries_style(sample):
    pkg = pkg_mod.validate_package(sample)
    tl = {
        "timeline": [{"from": i * 60, "duration": 60, "broll": None, "avatar": None} for i in range(len(pkg["beats"]))],
        "words": [{"text": "x", "startMs": 0, "endMs": 10, "page": 0, "beat": 0}],
        "frames": 60 * len(pkg["beats"]),
    }
    props = build_mod.remotion_props(pkg, tl)
    assert [b["number"] for b in props["beats"]] == [0, 1, 2, 3, 4, 5, 0]
    assert props["palette"] == "indigo-coral" and props["cover"]["kicker"] == "2026 guide"
    assert set(props["words"][0]) == {"text", "startMs", "endMs", "page"}


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def _entry(rid, **kw):
    base = {"request_id": rid, "lang": "en", "slug": "s", "article_url": f"https://b.top/{rid}",
            "palette": "indigo-coral", "motif": "diamonds", "dir": "/tmp/x"}
    return {**base, **kw}


def test_ledger_tracks_done_style_and_assets(home):
    ledger.add(_entry("en-a-1"))
    ledger.add(_entry("es-a-1", lang="es", palette="teal-amber", motif="dots"))
    ledger.update("en-a-1", state="ready", broll_ids=["pexels-1"], music_id="516")
    ledger.add(_entry("en-b-1", palette="violet-lime"))
    ledger.update("en-b-1", state="render_failed")
    # a failed post counts as taken too: the producer moves on to another
    assert ledger.done_urls() == {"https://b.top/en-a-1", "https://b.top/es-a-1", "https://b.top/en-b-1"}
    assert ledger.recent_style()["palettes"] == ["indigo-coral", "teal-amber"]
    assert ledger.used_assets() == {"broll": ["pexels-1"], "music": ["516"]}
    with pytest.raises(ValueError):
        ledger.add(_entry("en-a-1"))


def test_corrupt_ledger_fails_loudly_instead_of_forgetting(home):
    ledger.ledger_path().parent.mkdir(parents=True)
    ledger.ledger_path().write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unreadable"):
        ledger.read()


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------

FEED = """<?xml version="1.0"?><rss><channel>
<item><title>New</title><link>https://biglobster.top/blog/new-post.html</link><pubDate>Thu</pubDate></item>
<item><title>Old</title><link>https://biglobster.top/blog/old-post.html</link><pubDate>Wed</pubDate></item>
</channel></rss>"""


def test_parse_feed_extracts_slugs_in_order():
    items = st.parse_feed(FEED)
    assert [i["slug"] for i in items] == ["new-post", "old-post"]


def test_posts_skips_done_and_reports_twin_style(home, monkeypatch):
    monkeypatch.setattr(st, "_http_get", lambda url: FEED)
    ledger.add(_entry("en-new-post-1", article_url="https://biglobster.top/blog/new-post.html", slug="new-post"))
    ledger.add(_entry("es-old-post-1", lang="es", slug="old-post",
                      article_url="https://biglobster.top/es/blog/old-post.html"))
    out = json.loads(st.handle_shorts_studio({"action": "posts", "lang": "en"}))
    assert [p["slug"] for p in out["posts"]] == ["old-post"]
    assert out["posts"][0]["twin"]["lang"] == "es"


@pytest.fixture
def submit_env(home, monkeypatch):
    dispatched = []
    monkeypatch.setattr("plugins.shorts.studio.article.fetch_article", lambda url: ("T", ARTICLE_TEXT))
    monkeypatch.setattr("plugins.shorts.critic.review", lambda pkg, text, **kw: [])   # the critic has its own tests
    monkeypatch.setattr(github_studio, "dispatch",
                        lambda rid, pkg: dispatched.append((rid, pkg)) or {"run_id": 7, "run_url": "u"})
    return dispatched


def test_submit_dispatches_and_records(tip, submit_env):
    out = json.loads(st.handle_shorts_studio({"action": "submit", "package": tip}))
    assert out["success"], out
    rid, sent = submit_env[0]
    assert rid.startswith("es-when-to-adopt-ai-sme-2026-") and sent["request_id"] == rid
    entry = ledger.get(rid)
    assert entry["state"] == "rendering" and entry["run_id"] == 7
    again = json.loads(st.handle_shorts_studio({"action": "submit", "package": tip}))
    assert not again["success"] and "already has a short" in again["error"]


def test_submit_rejects_invented_figures_and_a_repeated_look(tip, submit_env):
    ledger.add(_entry("es-x-1", palette="teal-amber", motif="grid"))
    tip["style"]["palette"] = "teal-amber"          # a rotating palette; "brand" is exempt (see below)
    tip["beats"][3]["value"] = "91%"
    out = json.loads(st.handle_shorts_studio({"action": "submit", "package": tip}))
    assert not out["success"]
    joined = " ".join(out["problems"])
    assert "style.palette" in joined and "'91'" in joined
    assert submit_env == []


def test_submit_accepts_only_tip_shorts(sample, tip, submit_env):
    # Every short is the presenter's, in their one voice: the classic narrator format is refused.
    out = json.loads(st.handle_shorts_studio({"action": "submit", "package": sample}))
    assert not out["success"] and "tip" in " ".join(out["problems"]) and submit_env == []
    out = json.loads(st.handle_shorts_studio({"action": "submit", "package": tip}))
    assert out["success"], out
    assert ledger.get(out["request_id"])["format"] == "tip"


@pytest.mark.parametrize("pattern,vo,problem", [
    ("clickbait", None, "one of"),
    ("pain-first", "¿Cuántas horas pierdes cada semana? Empieza por una tarea.", "is a question"),
    ("question", "Automatiza primero lo aburrido.", "needs a first sentence that asks"),
    ("number-payoff", "Automatiza primero lo aburrido.", "in digits"),
])
def test_the_hook_pattern_is_closed_and_matches_the_hook(tip, pattern, vo, problem):
    tip["hook_pattern"] = pattern
    if vo:
        tip["beats"][0]["vo"] = vo
    with pytest.raises(pkg_mod.PackageError) as err:
        pkg_mod.validate_package(tip)
    assert any(p.startswith("hook_pattern") and problem in p for p in err.value.problems), err.value.problems


def test_the_hook_pattern_is_optional_for_the_renderer(tip):
    # The renderer deploys on merge, Hermes on a manual deploy: an older Hermes's
    # package carries no pattern and must still render.
    assert pkg_mod.validate_package(tip)["hook_pattern"] == "contrarian"
    del tip["hook_pattern"]
    assert "hook_pattern" not in pkg_mod.validate_package(tip)
    tip["hook_pattern"] = "question"
    tip["beats"][0]["vo"] = "¿Automatizas lo aburrido primero? " + tip["beats"][0]["vo"]
    assert pkg_mod.validate_package(tip)["hook_pattern"] == "question"


def test_submit_rotates_hook_patterns_per_language(tip, submit_env):
    for i, pattern in enumerate(("warning", "contrarian", "result-first")):
        ledger.add(_entry(f"es-old-{i}", lang="es", motif="dots", hook_pattern=pattern))
    ledger.add(_entry("es-failed-1", lang="es", motif="dots", hook_pattern="pain-first"))
    ledger.update("es-failed-1", state="render_failed")       # never aired: takes no slot
    ledger.add(_entry("en-other-1", motif="dots", hook_pattern="question"))   # other language
    assert ledger.recent_hook_patterns(lang="es") == ["warning", "contrarian", "result-first"]

    out = json.loads(st.handle_shorts_studio({"action": "submit", "package": tip}))
    assert not out["success"] and submit_env == []
    assert any("hook_pattern 'contrarian'" in p for p in out["problems"]), out["problems"]

    del tip["hook_pattern"]
    out = json.loads(st.handle_shorts_studio({"action": "submit", "package": tip}))
    assert any(p.startswith("hook_pattern: required") for p in out["problems"]), out["problems"]

    tip["hook_pattern"] = "pain-first"
    out = json.loads(st.handle_shorts_studio({"action": "submit", "package": tip}))
    assert out["success"], out
    assert ledger.get(out["request_id"])["hook_pattern"] == "pain-first"
    taken = json.loads(st.handle_shorts_studio({"action": "ledger"}))["hook_patterns_taken"]
    assert taken == {"en": ["question"], "es": ["contrarian", "result-first", "pain-first"]}


def _fake_artifact(passed=True, **extra):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps({
            "duration_s": 58.2, "music": {"id": "516"}, "broll": [{"id": "pexels-9"}],
            "qa": {"passed": passed, "errors": [] if passed else ["loudness"], "warnings": []},
            **extra,
        }))
        z.writestr("master.mp4", b"x")
        z.writestr("cover.jpg", b"x")
    return buf.getvalue()


def test_status_collects_a_finished_render(tip, submit_env, monkeypatch):
    st.handle_shorts_studio({"action": "submit", "package": tip})
    rid = submit_env[0][0]
    monkeypatch.setattr(github_studio, "get_run",
                        lambda run_id: {"id": run_id, "status": "completed", "conclusion": "success", "html_url": "h"})

    def fake_download(run_id, name, dest):
        assert name == f"short-{rid}"
        with zipfile.ZipFile(io.BytesIO(_fake_artifact())) as z:
            github_studio._safe_extract(z, dest)
        return dest

    monkeypatch.setattr(github_studio, "download_artifact", fake_download)
    out = json.loads(st.handle_shorts_studio({"action": "status"}))
    item = out["shorts"][0]
    assert item["state"] == "ready" and item["files"]["master"].endswith("master.mp4")
    assert ledger.get(rid)["broll_ids"] == ["pexels-9"]


def test_publish_is_refused_in_shadow_mode_and_handoff_delivers_files(tip, submit_env, monkeypatch):
    monkeypatch.delenv("SHORTS_PUBLISH_MODE", raising=False)
    st.handle_shorts_studio({"action": "submit", "package": tip})
    rid = submit_env[0][0]
    out_dir = Path(ledger.get(rid)["dir"]) / "out"
    out_dir.mkdir(parents=True)
    for name in ("master.mp4", "cover.jpg"):
        (out_dir / name).write_bytes(b"x")
    ledger.update(rid, state="ready", qa={"passed": True})

    refused = json.loads(st.handle_shorts_studio({"action": "publish", "request_id": rid, "target": "youtube"}))
    assert not refused["success"] and refused.get("shadow")

    hand = json.loads(st.handle_shorts_studio({"action": "handoff", "request_id": rid}))
    assert f"MEDIA:{out_dir / 'master.mp4'}" in hand["message"] and "[SHADOW]" in hand["message"]
    assert ledger.get(rid)["state"] == "handed_off"


def test_publish_refuses_a_short_that_failed_qa(tip, submit_env, monkeypatch):
    monkeypatch.setenv("SHORTS_PUBLISH_MODE", "live")
    st.handle_shorts_studio({"action": "submit", "package": tip})
    rid = submit_env[0][0]
    ledger.update(rid, state="qa_failed")
    out = json.loads(st.handle_shorts_studio({"action": "publish", "request_id": rid, "target": "youtube"}))
    assert not out["success"] and "passed QA" in out["error"]


def test_studio_tool_is_hidden_without_a_token(monkeypatch):
    monkeypatch.delenv("SHORTS_STUDIO_GITHUB_TOKEN", raising=False)
    assert st.check_studio_available() is False
    monkeypatch.setenv("SHORTS_STUDIO_GITHUB_TOKEN", "t")
    assert st.check_studio_available() is True


# ---------------------------------------------------------------------------
# GitHub client
# ---------------------------------------------------------------------------

def test_package_encoding_round_trips():
    import base64
    import gzip

    pkg = {"lang": "es", "beats": [{"vo": "¿Tu pyme llega tarde?"}]}
    decoded = json.loads(gzip.decompress(base64.b64decode(github_studio.encode_package(pkg))))
    assert decoded == pkg


def test_artifact_extraction_refuses_path_traversal(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../escape.txt", "x")
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as z, pytest.raises(github_studio.StudioError):
        github_studio._safe_extract(z, tmp_path / "out")


# ---------------------------------------------------------------------------
# Publishers
# ---------------------------------------------------------------------------

def test_youtube_metadata_marks_shorts_and_synthetic_media(monkeypatch):
    from plugins.shorts.social import youtube

    monkeypatch.delenv("SHORTS_YOUTUBE_PRIVACY", raising=False)
    meta = youtube.build_metadata({"youtube": {"title": "When to adopt AI", "tags": ["a"]}}, "en", synthetic=True)
    assert meta["snippet"]["title"] == "When to adopt AI #Shorts"
    assert meta["status"] == {"privacyStatus": "public", "selfDeclaredMadeForKids": False,
                              "containsSyntheticMedia": True}


def test_youtube_description_links_the_article_with_utm(monkeypatch):
    from plugins.shorts.social import youtube

    article = {"url": "https://biglobster.top/blog/invoices.html", "slug": "invoices"}
    meta = youtube.build_metadata({"youtube": {"title": "T", "description": "Three ways."}}, "en",
                                  synthetic=False, article=article)
    assert meta["snippet"]["description"] == (
        "Three ways.\n\nhttps://biglobster.top/blog/invoices.html"
        "?utm_source=youtube&utm_medium=short&utm_campaign=short-invoices")
    # Already tagged by the writer: not added twice. No article: nothing added.
    tagged = {"youtube": {"title": "T", "description": "see https://biglobster.top/x?utm_source=youtube"}}
    assert meta_desc(youtube, tagged, article) == tagged["youtube"]["description"]
    assert meta_desc(youtube, {"youtube": {"title": "T", "description": "plain"}}, None) == "plain"
    assert youtube.article_link({"url": "http://insecure", "slug": "x"}) == ""


def meta_desc(youtube, social, article):
    return youtube.build_metadata(social, "en", synthetic=False, article=article)["snippet"]["description"]


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class _FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _next(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.script.pop(0)

    def post(self, url, **kw):
        return self._next("POST", url, **kw)

    def get(self, url, **kw):
        return self._next("GET", url, **kw)


def test_facebook_reel_runs_start_upload_finish(tmp_path, monkeypatch):
    from plugins.shorts.social import meta

    monkeypatch.setenv("META_PAGE_ID", "123")
    monkeypatch.setenv("META_PAGE_ACCESS_TOKEN", "tok")
    video = tmp_path / "v.mp4"
    video.write_bytes(b"abc")
    fake = _FakeClient([_Resp(body={"video_id": "99", "upload_url": "https://rupload.facebook.com/x/99"}),
                        _Resp(body={"success": True}), _Resp(body={"success": True})])
    monkeypatch.setattr(meta, "_client", lambda: fake)
    result = meta.facebook_reel(video, "desc")
    assert result == {"id": "99", "url": "https://www.facebook.com/reel/99"}
    assert fake.calls[1][2]["headers"]["file_size"] == "3"
    assert fake.calls[2][2]["data"]["video_state"] == "PUBLISHED"


def test_instagram_reel_returns_pending_when_processing_outlasts_budget(tmp_path, monkeypatch):
    from plugins.shorts.social import meta

    for k, v in {"META_PAGE_ID": "1", "META_PAGE_ACCESS_TOKEN": "t", "META_IG_USER_ID": "2"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(meta, "PROCESS_BUDGET", 0.0)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"abc")
    fake = _FakeClient([_Resp(body={"id": "c1"}), _Resp(body={"success": True})])
    monkeypatch.setattr(meta, "_client", lambda: fake)
    result = meta.instagram(video, kind="reel", caption="hi")
    assert result["pending"] == "c1"
    assert fake.calls[0][2]["data"]["media_type"] == "REELS"
    assert fake.calls[0][2]["data"]["thumb_offset"] == "1500"  # no cover given


# ---------------------------------------------------------------------------
# ffmpeg-backed checks
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_master_audio_lands_on_minus_14_lufs_and_qa_reads_it(tmp_path):
    from plugins.shorts.studio import media, qa

    voice = tmp_path / "voice.wav"
    media.ffmpeg("-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000:duration=24",
                 "-af", "volume=0.05", "-ac", "1", str(voice), what="voice")
    audio = media.master_audio(voice, None, tmp_path / "master.wav")
    video = tmp_path / "silent.mp4"
    media.ffmpeg("-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=24", "-c:v", "libx264",
                 "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(video), what="video")
    master = media.mux(video, audio, tmp_path / "out.mp4")
    report = qa.check_master(master, expected_seconds=24.0)
    assert abs(report["loudness"]["integrated_lufs"] - (-14.0)) < 1.0
    assert not [e for e in report["errors"] if "LUFS" in e or "resolution" in e]
    story = media.story_cut(master, tmp_path / "story.mp4", 10.0)
    assert qa.check_story(story)["passed"]
    assert abs(media.duration(story) - 10.0) < 0.2


def test_a_post_that_failed_is_not_tried_again(home):
    url = "https://b.top/post"
    ledger.add(_entry("en-post-1", article_url=url))
    assert url in ledger.done_urls()              # rendering: taken
    ledger.update("en-post-1", state="qa_failed")
    assert url in ledger.done_urls()              # failed once: the producer picks another post


def test_failures_are_reported_once(home, monkeypatch):
    ledger.add(_entry("en-f-1", dir=str(home / "x")))
    ledger.update("en-f-1", state="qa_failed", qa={"passed": False, "errors": ["loudness"]})
    first = json.loads(st.handle_shorts_studio({"action": "status"}))
    second = json.loads(st.handle_shorts_studio({"action": "status"}))
    assert [s["request_id"] for s in first["shorts"]] == ["en-f-1"]
    assert second["shorts"] == []


@pytest.mark.parametrize(
    "url, gets_token",
    [
        ("https://api.github.com/repos/br41s/hermes-sandbox/releases/assets/1", True),
        ("https://github.com/br41s/hermes-sandbox/releases/download/x/a.mp4", True),
        ("https://github.com.evil.example/a.mp4", False),
        ("https://evil.example/?next=https://api.github.com/", False),
        ("http://api.github.com/repos/x", False),
        ("https://objects.githubusercontent.com/a.mp4", False),
    ],
)
def test_workflow_token_only_goes_to_github_hosts(url, gets_token, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    assert ("Authorization" in build_mod._github_headers(url)) is gets_token


def test_shadow_handoff_does_not_relabel_a_published_short(tip, submit_env, monkeypatch):
    monkeypatch.delenv("SHORTS_PUBLISH_MODE", raising=False)
    st.handle_shorts_studio({"action": "submit", "package": tip})
    rid = submit_env[0][0]
    ledger.update(rid, state="published")
    out = json.loads(st.handle_shorts_studio({"action": "handoff", "request_id": rid}))
    assert not out["success"] and "shadow" in out["error"]
    assert ledger.get(rid)["state"] == "published"


def test_intervals_keep_a_condition_that_runs_to_the_end():
    from plugins.shorts.studio import qa

    log = "freeze_start: 1.0\nfreeze_end: 2.0\nfreeze_start: 8.5\n"
    spans = qa._intervals(log, "freeze_start", "freeze_end", until=12.0)
    assert spans == [{"start": 1.0, "end": 2.0, "length": 1.0},
                     {"start": 8.5, "end": 12.0, "length": 3.5}]


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_qa_catches_a_freeze_that_lasts_to_the_last_frame(tmp_path):
    from plugins.shorts.studio import media, qa

    video = tmp_path / "v.mp4"
    # 22s of motion, then the last frame held for 3s: the freeze never ends.
    media.ffmpeg("-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=22",
                 "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000:duration=25",
                 "-vf", "tpad=stop_mode=clone:stop_duration=3", "-c:v", "libx264", "-preset", "ultrafast",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video), what="frozen tail")
    report = qa.check_master(video)
    assert any("frozen picture" in e for e in report["errors"]), report["errors"]


def test_submit_refuses_a_colliding_request_id_before_dispatching(tip, submit_env, monkeypatch):
    monkeypatch.setattr(st, "_request_id", lambda pkg: "en-fixed-id-20260925000000")
    first = json.loads(st.handle_shorts_studio({"action": "submit", "package": tip}))
    assert first["success"], first
    other = copy.deepcopy(tip)
    other["article"]["url"] = "https://biglobster.top/blog/another-post"
    other["beats"][-1]["url"] = other["article"]["url"]
    other["style"] = {"palette": "violet-lime", "motif": "grid"}
    other["hook_pattern"] = "warning"
    second = json.loads(st.handle_shorts_studio({"action": "submit", "package": other}))
    assert not second["success"] and "submitted this second" in second["error"]
    assert len(submit_env) == 1  # no second render started


def test_publish_story_without_a_story_cut_is_a_clear_error(tip, submit_env, monkeypatch):
    monkeypatch.setenv("SHORTS_PUBLISH_MODE", "live")
    for k, v in {"META_PAGE_ID": "1", "META_PAGE_ACCESS_TOKEN": "t", "META_IG_USER_ID": "2"}.items():
        monkeypatch.setenv(k, v)
    st.handle_shorts_studio({"action": "submit", "package": tip})
    rid = submit_env[0][0]
    out_dir = Path(ledger.get(rid)["dir"]) / "out"
    out_dir.mkdir(parents=True)
    (out_dir / "master.mp4").write_bytes(b"x")
    ledger.update(rid, state="ready")
    out = json.loads(st.handle_shorts_studio({"action": "publish", "request_id": rid,
                                              "target": "instagram_story"}))
    assert not out["success"] and "Story cut" in out["error"]


# ---------------------------------------------------------------------------
# Multiplex: one process serves every profile, so os.environ is the default
# profile's env for everyone. The studio's keys must come from the run's scope.
# ---------------------------------------------------------------------------

_BL_KEYS = {
    "SHORTS_STUDIO_GITHUB_TOKEN": "bl-token",
    "YOUTUBE_CLIENT_ID": "a", "YOUTUBE_CLIENT_SECRET": "b", "YOUTUBE_REFRESH_TOKEN": "c",
    "META_PAGE_ID": "1", "META_PAGE_ACCESS_TOKEN": "t", "META_IG_USER_ID": "2",
    "SHORTS_PUBLISH_MODE": "live",
}


@pytest.fixture
def multiplex(monkeypatch):
    import agent.secret_scope as ss

    for k, v in _BL_KEYS.items():
        monkeypatch.setenv(k, v)  # BigLobster's keys in the shared process env
    ss.set_multiplex_active(True)
    yield ss
    ss.set_multiplex_active(False)


def test_a_rented_tenant_never_sees_the_studio_or_biglobster_keys(multiplex):
    from plugins.shorts.social import meta, youtube

    token = multiplex.set_secret_scope({"OPENROUTER_API_KEY": "tenant", "BL_SITE_URL": "https://c.example"})
    try:
        assert st.check_studio_available() is False
        assert github_studio.token() == ""
        assert youtube.configured() is False and meta.configured(instagram=True) is False
        assert st.publish_mode() == "shadow"
    finally:
        multiplex.reset_secret_scope(token)


def test_the_default_profile_still_resolves_its_own_keys(multiplex):
    from plugins.shorts.social import youtube

    token = multiplex.set_secret_scope(dict(_BL_KEYS))
    try:
        assert st.check_studio_available() is True
        assert github_studio.token() == "bl-token" and youtube.configured() is True
        assert st.publish_mode() == "live"
    finally:
        multiplex.reset_secret_scope(token)


def test_no_scope_under_multiplex_reads_as_unconfigured_not_as_the_process_env(multiplex):
    assert st.check_studio_available() is False
    assert st.publish_mode() == "shadow"


# ---------------------------------------------------------------------------
# Generated scenes (genvideo): schema, budget, key handling, fallback
# ---------------------------------------------------------------------------

SCENE = "Slow push-in on a small bakery counter at dawn, flour in the air, warm window light"


def test_a_scene_is_a_picture_only_and_at_most_three_per_short(sample):
    ok = copy.deepcopy(sample)
    ok["beats"][0]["scene"] = SCENE
    assert pkg_mod.validate_package(ok)["beats"][0]["scene"] == SCENE

    texty = copy.deepcopy(sample)
    texty["beats"][0]["scene"] = "A neon sign with the brand logo and big numbers on a wall"
    with pytest.raises(pkg_mod.PackageError) as exc:
        pkg_mod.validate_package(texty)
    assert "picture only" in str(exc.value) and "logo" in str(exc.value)

    on_cta = copy.deepcopy(sample)
    on_cta["beats"][-1]["scene"] = SCENE
    with pytest.raises(pkg_mod.PackageError, match="generated scene"):
        pkg_mod.validate_package(on_cta)

    too_many = copy.deepcopy(sample)
    for beat in too_many["beats"][:-1]:
        beat["scene"] = SCENE
    assert len(too_many["beats"]) - 1 > pkg_mod.MAX_SCENES
    with pytest.raises(pkg_mod.PackageError, match="max 3 per short"):
        pkg_mod.validate_package(too_many)


def test_clip_length_follows_the_scene_within_the_models_range():
    assert genvideo.clip_seconds(2.1) == 5
    assert genvideo.clip_seconds(7.0) == 7
    assert genvideo.clip_seconds(7.01) == 8
    assert genvideo.clip_seconds(31) == 15


def _scene_pkg(sample, n=3):
    pkg = pkg_mod.validate_package(sample)
    for beat in pkg["beats"][:n]:
        beat["scene"] = SCENE
    timeline = [{"from": 0, "duration": 8 * 30} for _ in pkg["beats"]]
    return pkg, timeline


def test_plan_keeps_each_short_inside_its_budget_and_the_day_inside_the_cap(sample):
    pkg, timeline = _scene_pkg(sample)
    cfg = {"price_per_s": 0.03, "short_budget": 0.5, "daily_cap": 1.0}

    def budget(used, committed=0.0):
        return {"cfg": cfg, "daily_used": used, "committed": committed}

    b = budget(0.0)
    jobs, skipped = genvideo.plan(pkg, timeline, b, fps=30)
    assert [j["beat"] for j in jobs] == [0, 1]           # 8 s x $0.03 = $0.24 each; the third breaks $0.50
    assert skipped[0]["beat"] == 2 and "short budget" in skipped[0]["reason"]
    assert b["committed"] == pytest.approx(0.48)

    jobs, skipped = genvideo.plan(pkg, timeline, budget(0.80), fps=30)
    assert jobs == [] and all("daily cap" in s["reason"] for s in skipped)

    jobs, _ = genvideo.plan(pkg, timeline, budget(None), fps=30)
    assert len(jobs) == 2                                # unknown spend: the per-short budget still holds
    assert "pink" not in jobs[0]["prompt"] and "No text" in jobs[0]["prompt"]

    jobs, _ = genvideo.plan(pkg, timeline, budget(0.0, committed=0.20), fps=30)
    assert [j["beat"] for j in jobs] == [0]              # the presenter planned first and took $0.20

    timeline[0]["presenter"] = "presenter/p00.mp4"
    jobs, skipped = genvideo.plan(pkg, timeline, budget(0.0), fps=30)
    assert [j["beat"] for j in jobs] == [1, 2] and skipped == []   # a presenter beat needs no background


@pytest.mark.parametrize("url,gets_key", [
    ("https://openrouter.ai/api/v1/videos/abc", True),
    ("https://storage.example-cdn.com/v/abc.mp4?sig=1", False),
    ("http://openrouter.ai/api/v1/videos/abc", False),
    ("https://openrouter.ai.evil.example/x", False),
])
def test_the_openrouter_key_never_leaves_openrouter(url, gets_key):
    assert ("Authorization" in genvideo._headers(url, "sk-test")) is gets_key


class _ORResp:
    def __init__(self, status=200, data=None, body=b""):
        self.status_code, self._data, self._body = status, data or {}, body
        self.text = json.dumps(self._data)

    def json(self):
        return self._data

    def iter_bytes(self):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpenRouter:
    """Submit -> one poll -> completed, with a reported cost; or a 402 on submit."""

    def __init__(self, *, refuse=False, usage_daily=0.10):
        self.refuse, self.usage_daily = refuse, usage_daily
        self.posts, self.calls = [], []

    def __call__(self, *a, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None):
        self.calls.append((url, headers))
        if url == genvideo.KEY_INFO:
            return _ORResp(data={"data": {"usage_daily": self.usage_daily}})
        return _ORResp(data={"status": "completed", "usage": {"cost": 0.12},
                           "unsigned_urls": ["https://cdn.example.com/clip.mp4"]})

    def post(self, url, headers=None, json=None):
        self.posts.append(json)
        if self.refuse:
            return _ORResp(402, {"error": "no credit"})
        return _ORResp(data={"id": f"job{len(self.posts)}"})

    def stream(self, method, url, headers=None):
        self.calls.append((url, headers))
        return _ORResp(body=b"mp4")


def _run_scenes(sample, tmp_path, monkeypatch, fake, **env):
    import httpx

    from plugins.shorts.studio import media

    monkeypatch.setattr(httpx, "Client", fake)
    monkeypatch.setattr(genvideo, "POLL_EVERY_S", 0)
    monkeypatch.setattr(media, "normalize_clip", lambda src, dst, seconds, start=0.0: dst)
    for k, v in {"SHORTS_OPENROUTER_API_KEY": "sk-test", **env}.items():
        monkeypatch.setenv(k, v)
    pkg, timeline = _scene_pkg(sample, n=2)
    report = genvideo.render_scenes(pkg, timeline, tmp_path, tmp_path / "public", "rid-1", fps=30, offline=False)
    return pkg, timeline, report


def test_generated_scenes_fill_their_slots_and_record_the_real_cost(sample, tmp_path, monkeypatch):
    fake = _FakeOpenRouter()
    pkg, timeline, report = _run_scenes(sample, tmp_path, monkeypatch, fake)
    assert report["generated"] == 2 and report["spent_usd"] == pytest.approx(0.24)
    assert [s["broll"] for s in timeline[:2]] == ["ai/s00.mp4", "ai/s01.mp4"]
    assert fake.posts[0]["model"] == "heygen/heygen-video-1" and fake.posts[0]["aspect_ratio"] == "9:16"
    assert fake.posts[0]["seed"] == fake.posts[1]["seed"]                 # one look per short
    cdn = [h for u, h in fake.calls if u.startswith("https://cdn.example.com")]
    assert cdn and all("Authorization" not in h for h in cdn)


def test_a_refused_or_unkeyed_scene_falls_back_to_stock_footage(sample, tmp_path, monkeypatch):
    _, timeline, report = _run_scenes(sample, tmp_path, monkeypatch, _FakeOpenRouter(refuse=True))
    assert report["generated"] == 0 and report["spent_usd"] == 0
    assert all(not s.get("broll") for s in timeline) and "402" in report["scenes"][0]["reason"]
    assert "no credit" in report["scenes"][0]["reason"]                 # OpenRouter's own words kept

    monkeypatch.delenv("SHORTS_OPENROUTER_API_KEY")
    pkg, timeline = _scene_pkg(sample, n=1)
    report = genvideo.render_scenes(pkg, timeline, tmp_path, tmp_path / "public", "rid-2", fps=30, offline=False)
    assert report["scenes"] == [{"beat": 0, "status": "skipped", "reason": "SHORTS_OPENROUTER_API_KEY is not set"}]


def test_only_the_hook_and_cta_are_said_to_camera_and_briefly(sample):
    raw = copy.deepcopy(sample)
    raw["beats"][0]["presenter"] = True
    raw["beats"][-1]["presenter"] = True
    pkg = pkg_mod.validate_package(raw)
    assert pkg["beats"][0]["presenter"] is True and pkg["beats"][-1]["presenter"] is True
    assert not any(b.get("presenter") for b in pkg["beats"][1:-1])

    middle = copy.deepcopy(sample)
    middle["beats"][1]["presenter"] = True
    with pytest.raises(pkg_mod.PackageError, match="only the hook and the CTA"):
        pkg_mod.validate_package(middle)

    wordy = copy.deepcopy(sample)
    wordy["beats"][-1]["presenter"] = True
    wordy["beats"][-1]["vo"] = " ".join(["palabra"] * (pkg_mod.MAX_PRESENTER_WORDS + 1))
    with pytest.raises(pkg_mod.PackageError, match="said to camera"):
        pkg_mod.validate_package(wordy)

    stringy = copy.deepcopy(sample)
    stringy["beats"][0]["presenter"] = "yes"
    with pytest.raises(pkg_mod.PackageError, match="true or absent"):
        pkg_mod.validate_package(stringy)


def test_every_language_has_a_presenter_photo():
    from plugins.shorts.studio import presenter

    assert set(presenter.PRESENTERS) == set(pkg_mod.DEFAULT_VOICES)
    for slug, _name in presenter.PRESENTERS.values():
        url = presenter.photo_data_url(slug)
        assert url and url.startswith("data:image/jpeg;base64,"), slug


def _presenter_pkg(sample):
    raw = copy.deepcopy(sample)
    raw["beats"][0]["presenter"] = True
    raw["beats"][-1]["presenter"] = True
    return pkg_mod.validate_package(raw)


def test_presenter_clips_are_requested_with_the_photo_and_share_the_budget(sample, tmp_path, monkeypatch):
    import httpx

    from plugins.shorts.studio import presenter

    fake = _FakeOpenRouter()
    monkeypatch.setattr(httpx, "Client", fake)
    monkeypatch.setattr(genvideo, "POLL_EVERY_S", 0)
    for k, v in {"SHORTS_OPENROUTER_API_KEY": "sk-test", "SHORTS_PRESENTER_VOICE_EN": "voice-en-1"}.items():
        monkeypatch.setenv(k, v)
    pkg = _presenter_pkg(sample)
    budget = genvideo.open_budget(offline=False)
    assert budget["daily_used"] == pytest.approx(0.10)

    got = presenter.render(pkg, tmp_path, budget, offline=False)
    last = len(pkg["beats"]) - 1
    assert sorted(got["clips"]) == [0, last] and got["report"]["presenter"] == ("Martín" if pkg["lang"] == "en" else "Lucía")
    body = fake.posts[0]
    assert body["model"] == "heygen/avatar-iv" and body["prompt"] == pkg["beats"][0]["vo"]
    assert body["input_references"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    heygen = body["provider"]["options"]["heygen"]
    assert "motion_prompt" in heygen
    # A pinned voice wins; otherwise the presenter's default (Avatar IV refuses a script without one).
    assert heygen["voice_id"] == ("voice-en-1" if pkg["lang"] == "en" else presenter.VOICES["es"])
    # Two clips at the fake's reported $0.12 each: the estimate is replaced by the real cost.
    assert got["report"]["spent_usd"] == pytest.approx(0.24) and budget["committed"] == pytest.approx(0.24)


def test_presenter_respects_the_budget_and_never_fails_the_render(sample, tmp_path, monkeypatch):
    from plugins.shorts.studio import presenter

    pkg = _presenter_pkg(sample)
    cfg = dict(genvideo.config(), key="sk-test", short_budget=0.05)
    got = presenter.render(pkg, tmp_path, {"cfg": cfg, "daily_used": 0.0, "committed": 0.0}, offline=False)
    assert got["clips"] == {} and all("short budget" in b["reason"] for b in got["report"]["beats"])

    got = presenter.render(pkg, tmp_path, genvideo.open_budget(offline=True), offline=True)
    assert got["clips"] == {} and got["report"]["beats"][0]["reason"] == "offline render"

    monkeypatch.setattr(presenter, "PHOTO_DIR", tmp_path / "nowhere")
    cfg = dict(genvideo.config(), key="sk-test")
    got = presenter.render(pkg, tmp_path, {"cfg": cfg, "daily_used": 0.0, "committed": 0.0}, offline=False)
    assert got["clips"] == {} and "missing" in got["report"]["beats"][0]["reason"]


def test_a_presenter_beat_gets_no_stock_footage_and_reaches_remotion(sample, tmp_path):
    pkg = _presenter_pkg(sample)
    timeline = [{"from": 0, "duration": 90} for _ in pkg["beats"]]
    timeline[0]["presenter"] = "presenter/p00.mp4"
    used = build_mod.gather_broll(pkg, timeline, tmp_path, tmp_path / "public", "seed", offline=True)
    assert used == [] and timeline[0]["broll"] is None
    props = build_mod.remotion_props(pkg, {"timeline": timeline, "words": [], "frames": 90 * len(timeline)})
    assert props["beats"][0]["presenter"] == "presenter/p00.mp4"
    assert props["beats"][0]["presenterName"] in ("Lucía", "Martín")
    assert "presenter" not in props["beats"][-1]          # its clip was not made: the template CTA


def test_stock_footage_never_overwrites_a_generated_scene(sample, tmp_path):
    pkg = pkg_mod.validate_package(sample)
    timeline = [{"from": 0, "duration": 90} for _ in pkg["beats"]]
    timeline[0]["broll"] = "ai/s00.mp4"
    build_mod.gather_broll(pkg, timeline, tmp_path, tmp_path / "public", "seed", offline=True)
    assert timeline[0]["broll"] == "ai/s00.mp4" and timeline[1]["broll"] is None


def test_a_short_with_generated_scenes_is_published_as_synthetic_media(tip, submit_env, monkeypatch):
    st.handle_shorts_studio({"action": "submit", "package": tip})
    rid = submit_env[0][0]
    monkeypatch.setattr(github_studio, "get_run",
                        lambda run_id: {"id": run_id, "status": "completed", "conclusion": "success", "html_url": "h"})

    def fake_download(run_id, name, dest):
        art = _fake_artifact(ai_video={"generated": 2, "spent_usd": 0.24}, synthetic=True)
        with zipfile.ZipFile(io.BytesIO(art)) as z:
            github_studio._safe_extract(z, dest)
        return dest

    monkeypatch.setattr(github_studio, "download_artifact", fake_download)
    st.handle_shorts_studio({"action": "status"})
    entry = ledger.get(rid)
    assert entry["ai_scenes"] == 2 and entry["ai_spend_usd"] == 0.24 and entry["synthetic"] is True

    seen = {}

    def fake_upload(video, thumb, srt, social, lang, *, synthetic=False, article=None):
        seen["synthetic"] = synthetic
        seen["article"] = article
        return {"id": "yt1", "url": "u", "privacy": "public", "warnings": []}

    monkeypatch.setenv("SHORTS_PUBLISH_MODE", "live")
    monkeypatch.setattr(st, "_enabled_targets", lambda: ["youtube"])
    from plugins.shorts.social import youtube as youtube_mod

    monkeypatch.setattr(youtube_mod, "upload", fake_upload)
    out = json.loads(st.handle_shorts_studio({"action": "publish", "request_id": rid, "target": "youtube"}))
    assert out["success"] and seen["synthetic"] is True
    assert seen["article"]["url"].startswith("https://") and seen["article"]["slug"]


# ---------------------------------------------------------------------------
# The first three seconds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vo,problem", [
    ("Hi, I'm Martin from BigLobster.", "never a greeting"),
    ("In this video: three ways to automate invoices.", "never a greeting"),
    ("Hola, hoy hablamos de facturas.", "never a greeting"),
    ("Soy Lucía y te explico cómo automatizar.", "never a greeting"),
    ("Most small businesses that try automation for the first time pick the wrong process. Here is why.",
     "first sentence has"),
])
def test_the_hook_opens_on_the_promise_within_three_seconds(sample, vo, problem):
    pkg = copy.deepcopy(sample)
    pkg["beats"][0]["vo"] = vo
    with pytest.raises(pkg_mod.PackageError) as exc:
        pkg_mod.validate_package(pkg)
    assert problem in str(exc.value)


def test_a_direct_hook_passes(sample):
    pkg = copy.deepcopy(sample)
    pkg["beats"][0]["vo"] = "¿Sigues escribiendo cada factura a mano? Hay una forma más rápida."
    pkg["beats"][0]["onscreen"] = "¿Cada *factura* a mano?"      # the written promise matches the spoken one
    assert pkg_mod.validate_package(pkg)["beats"][0]["vo"].startswith("¿Sigues")


# ---------------------------------------------------------------------------
# Hand-off to SocialBot in Buzz
# ---------------------------------------------------------------------------

@pytest.fixture
def buzz_env(monkeypatch):
    for k, v in {"SHORTS_BUZZ_CHANNEL": "ccc2bc1a-7a82-5a8f-8c4e-57a070cbe7cd",
                 "BUZZ_RELAY_URL": "wss://relay.example", "BUZZ_PRIVATE_KEY": "nsec1test",
                 "BUZZ_AUTH_TAG": "[]", "META_PAGE_ID": "1", "META_PAGE_ACCESS_TOKEN": "t",
                 "META_IG_USER_ID": "2", "YOUTUBE_CLIENT_ID": "c", "YOUTUBE_CLIENT_SECRET": "s",
                 "YOUTUBE_REFRESH_TOKEN": "r"}.items():
        monkeypatch.setenv(k, v)
    # The relay's metadata rule has its own test; the hand-off tests attach the files as they are.
    monkeypatch.setattr("plugins.shorts.buzz_share.canonical", lambda path: Path(path))


def test_buzz_takes_meta_away_from_hermes(buzz_env, monkeypatch):
    assert st._enabled_targets() == ["youtube"]
    # Half-configured (no attestation): not a hand-off, so Meta stays Hermes's.
    monkeypatch.delenv("BUZZ_AUTH_TAG")
    assert "facebook" in st._enabled_targets()
    monkeypatch.setenv("BUZZ_AUTH_TAG", "[]")
    monkeypatch.delenv("SHORTS_BUZZ_CHANNEL")
    assert "facebook" in st._enabled_targets() and "instagram_reel" in st._enabled_targets()


def _ready_short(sample, submit_env, **fields):
    st.handle_shorts_studio({"action": "submit", "package": sample})
    rid = submit_env[0][0]
    out_dir = Path(ledger.get(rid)["dir"]) / "out"
    out_dir.mkdir(parents=True)
    for name in ("master.mp4", "story.mp4", "cover.jpg"):
        (out_dir / name).write_bytes(b"x")
    ledger.update(rid, qa={"passed": True}, **fields)
    return rid


def test_a_live_short_is_handed_to_socialbot_once(tip, submit_env, buzz_env, monkeypatch):
    from plugins.shorts import buzz_share

    rid = _ready_short(tip, submit_env, state="published", synthetic=True,
                       published={"youtube": {"id": "yt1", "url": "https://youtube.com/shorts/yt1"}})
    monkeypatch.setenv("SHORTS_PUBLISH_MODE", "live")
    monkeypatch.setattr(buzz_share, "cli", lambda: "/usr/local/bin/buzz")
    calls = []

    def fake_run(args, **kw):
        calls.append((args, kw))
        return type("Done", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()

    monkeypatch.setattr(buzz_share.subprocess, "run", fake_run)
    out = json.loads(st.handle_shorts_studio({"action": "handoff", "request_id": rid}))
    assert out["success"] and "SocialBot" in out["message"] and "MEDIA:" not in out["message"]
    args, kw = calls[0]
    assert args[:5] == ["/usr/local/bin/buzz", "messages", "send", "--channel",
                        "ccc2bc1a-7a82-5a8f-8c4e-57a070cbe7cd"]
    assert sum(a == "--file" for a in args) == 3
    assert "@SocialBot" in kw["input"] and "youtube.com/shorts/yt1" in kw["input"]
    assert "contenido IA" in kw["input"]
    # Only the Buzz credentials reach the CLI, never the rest of the process env.
    assert set(kw["env"]) <= {"PATH", "HOME", "BUZZ_RELAY_URL", "BUZZ_PRIVATE_KEY", "BUZZ_AUTH_TAG"}
    assert ledger.get(rid)["buzz_posted_at"]

    again = json.loads(st.handle_shorts_studio({"action": "handoff", "request_id": rid}))
    assert again.get("already") and len(calls) == 1


def test_a_failed_buzz_post_is_retried_next_run(tip, submit_env, buzz_env, monkeypatch):
    from plugins.shorts import buzz_share

    rid = _ready_short(tip, submit_env, state="published")
    monkeypatch.setenv("SHORTS_PUBLISH_MODE", "live")
    monkeypatch.setattr(buzz_share, "cli", lambda: "/usr/local/bin/buzz")
    monkeypatch.setattr(buzz_share.subprocess, "run", lambda args, **kw: type(
        "Done", (), {"returncode": 1, "stdout": "", "stderr": "relay said no"})())
    out = json.loads(st.handle_shorts_studio({"action": "handoff", "request_id": rid}))
    assert not out["success"] and "relay said no" in out["error"]
    assert not ledger.get(rid).get("x_handoff_at")


def test_shadow_mode_never_posts_to_buzz(tip, submit_env, buzz_env, monkeypatch):
    from plugins.shorts import buzz_share

    rid = _ready_short(tip, submit_env, state="ready")
    monkeypatch.delenv("SHORTS_PUBLISH_MODE", raising=False)

    def boom(*a, **kw):
        raise AssertionError("shadow mode must not post to Buzz")

    monkeypatch.setattr(buzz_share, "post", boom)
    out = json.loads(st.handle_shorts_studio({"action": "handoff", "request_id": rid}))
    assert out["success"] and "[SHADOW]" in out["message"]


# ---------------------------------------------------------------------------
# The tip format: Lucía on camera in a setting, Martín's nod, his cards and loop


def test_the_tip_sample_is_one_tip_opened_by_lucia_and_narrated_by_martin(tip):
    pkg = pkg_mod.validate_package(tip)
    assert pkg["format"] == "tip" and pkg["version"] == 4
    assert pkg["voice"] == pkg_mod.TIP_VOICES["es"] == "es-ES-Tristan:DragonHDLatestNeural"   # Martín, in every short
    opener = pkg["opener"]
    assert opener["line"] and len(opener["setting"].split()) >= pkg_mod.TIP_MIN_SETTING_WORDS
    assert pkg_mod.TIP_PARTNER.search(opener["handoff"])
    assert [b["show"] for b in pkg["beats"]] == ["presenter", "card", "card", "card", "card", "card"]
    words = sum(len(b["vo"].split()) for b in pkg["beats"])
    assert pkg_mod.TIP_MIN_TOTAL_WORDS <= words <= pkg_mod.TIP_MAX_TOTAL_WORDS
    assert pkg_mod.TIP_MAX_ON_CAMERA_WORDS == 32          # the longer tip is Martín's; Lucía's paid take did not grow
    assert pkg["beats"][1]["before"] and pkg["beats"][1]["after"]


def test_a_package_cannot_pick_another_voice_for_the_narrator(tip):
    tip["voice"] = "es-MX-JorgeNeural"
    assert pkg_mod.validate_package(tip)["voice"] == "es-ES-Tristan:DragonHDLatestNeural"


@pytest.mark.parametrize("change,problem", [
    (lambda p: p["beats"][0].update(show="card"), "Lucía says the hook"),
    (lambda p: p["beats"][2].update(show="presenter"), "Martín narrates cards"),
    (lambda p: p["beats"][-1].update(show="presenter"), "the CTA too"),
    (lambda p: p["beats"][-1].update(vo="La guía completa está en biglobster punto top, enlace abajo."), "save the tip"),
    (lambda p: p["beats"][0].update(scene="Slow push-in on a bakery counter at dawn, warm light"), "no generated scenes"),
    (lambda p: p["beats"][0].update(presenter=True), "use show"),
    (lambda p: p["beats"][1].update(after=""), "after: required"),
    (lambda p: p["beats"][2].update(vo=p["beats"][2]["vo"] + " " + "muy " * 40), "one tip"),
    (lambda p: p["beats"].__delitem__(slice(2, 4)), "need at least 3"),
    (lambda p: p["beats"].insert(1, {"kind": "avatar", "vo": "Hola.", "clip_url": "https://x/c.mp4", "avatar": "lucia"}), "middle beats"),
    (lambda p: p.pop("opener"), "opener.line: required"),
    (lambda p: p["opener"].update(setting="a bakery"), "at least"),
    (lambda p: p["opener"].update(setting="a bakery at dawn with a big logo and neon text on the wall"), "picture only"),
    (lambda p: p["opener"].update(line=" ".join(["pan"] * 19)), "opener.line"),
    (lambda p: p["opener"].pop("handoff"), "opener.handoff: required"),
    (lambda p: p["opener"].update(handoff="Te lo cuenta mi compañero."), "names him"),
    (lambda p: p["opener"].update(handoff="Martín, " + "venga " * 8), "opener.handoff"),
    (lambda p: p["beats"][0].update(vo="Automatiza primero lo aburrido. " + "Es lo que más horas te quita cada semana " * 2), "says on camera"),
])
def test_the_tip_format_keeps_its_rules(tip, change, problem):
    change(tip)
    with pytest.raises(pkg_mod.PackageError, match=problem):
        pkg_mod.validate_package(tip)


def test_a_figure_in_the_opener_or_a_before_after_must_be_in_the_article(tip):
    pkg = pkg_mod.validate_package(tip)
    pkg["beats"][1]["after"] = "Ahorrar 40% de horas"
    pkg["opener"]["line"] = "Con un 91% de pan quemado, mejor automatiza."
    pkg["opener"]["handoff"] = "Martín, cuéntales lo del 12%."
    problems = " ".join(pkg_mod.check_grounding(pkg, ARTICLE_TEXT))
    assert "beats[1].after" in problems and "opener.line" in problems and "opener.handoff" in problems



def test_the_tip_short_runs_lucia_in_one_take_then_martins_cards(tip, monkeypatch):
    pkg = pkg_mod.validate_package(tip)
    beats = build_mod.tip_beats(pkg)
    take = beats[0]
    # Joke, hook and handoff in one take (one credit hold); the hook's kind, kicker and promise stay.
    assert take["kind"] == "hook" and take["show"] == "presenter" and take["kicker"] == pkg["beats"][0]["kicker"]
    assert take["vo"].startswith(pkg["opener"]["line"]) and take["vo"].endswith(pkg["opener"]["handoff"])
    assert pkg["beats"][0]["vo"] in take["vo"] and take["handoff"] == pkg["opener"]["handoff"]
    assert take["opener_line"] == pkg["opener"]["line"] and take["onscreen"] == pkg["beats"][0]["onscreen"]
    assert beats[1:-1] == pkg["beats"][1:] and not any(b.get("silent") for b in beats)
    assert beats[-2]["kind"] == "cta" and beats[-1]["kind"] == "outro"
    assert "biglobster" in beats[-1]["vo"] and "síguenos" in beats[-1]["vo"].lower()
    assert "!" not in beats[-1]["vo"] and "¡" not in beats[-1]["vo"]      # calm, not overexcited
    assert build_mod.on_camera(beats) == [0]                               # only her take is paid
    # Her free voice (for a missing take) follows the language's engine: Spanish is Azure by
    # default (Spain Spanish), so Elvira; on Kokoro, ef_dora. Martín's beats take the package's.
    assert take["voice"] == presenter_mod.HOST_FALLBACK_VOICES["es"]
    monkeypatch.setenv("SHORTS_TTS_ENGINE", "kokoro")
    assert build_mod.tip_beats(pkg)[0]["voice"] == voice_mod.KOKORO_HOST_VOICES["es"] == "ef_dora"
    assert all("voice" not in b for b in beats[1:])
    # A line without its full stop still reads as a sentence inside the take.
    bare = dict(pkg, opener={**pkg["opener"], "line": "Si ya madrugo, que las facturas se hagan solas"})
    assert build_mod.tip_beats(bare)[0]["vo"].startswith("Si ya madrugo, que las facturas se hagan solas. ")


class _FakeImageAPI(_FakeOpenRouter):
    """OpenRouter for the tip path: images on chat, Avatar IV jobs."""

    def __init__(self, *, refuse_models=(), **kw):
        super().__init__(**kw)
        self.refuse_models, self.chat = refuse_models, []

    def post(self, url, headers=None, json=None):
        if url == presenter_mod.CHAT_API:
            self.chat.append(json)
            if json["model"] in self.refuse_models:
                return _ORResp(403, {"error": {"message": "violation of provider Terms Of Service"}})
            png = base64.b64encode(b"\x89PNG fake").decode()
            return _ORResp(data={"usage": {"cost": 0.04}, "choices": [{"message": {"images": [
                {"image_url": {"url": "data:image/png;base64," + png}}]}}]})
        self.posts.append(json)
        if json.get("model") in self.refuse_models:
            return _ORResp(400, {"error": "unknown model"})
        return _ORResp(data={"id": f"job{len(self.posts)}"})


def _tip_env(monkeypatch, tmp_path, fake):
    import httpx

    from plugins.shorts.studio import media

    monkeypatch.setattr(httpx, "Client", fake)
    monkeypatch.setattr(genvideo, "POLL_EVERY_S", 0)
    monkeypatch.setenv("SHORTS_OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("SHORTS_ASSET_DIR", str(tmp_path / "assets"))
    monkeypatch.setattr(media, "silence", lambda dst, seconds: _touch(dst))


def _touch(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path



def test_lucia_is_paid_for_one_take_and_martin_costs_nothing(tip, tmp_path, monkeypatch):
    fake = _FakeImageAPI()
    _tip_env(monkeypatch, tmp_path, fake)
    pkg = dict(pkg_mod.validate_package(tip))
    pkg["beats"] = build_mod.tip_beats(pkg)
    cast = build_mod.tip_cast(pkg, tmp_path, genvideo.open_budget(offline=False), offline=False)
    # The setting: one image request, both photos as references, the article's scene in the prompt.
    assert len(fake.chat) == 1
    content = fake.chat[0]["messages"][0]["content"]
    assert "bakery" in content[0]["text"] and "physics" in content[0]["text"] and len(content) == 3
    # Avatar IV once, for joke + hook + handoff, in her HeyGen voice, on the setting; nothing for Martín.
    assert len(fake.posts) == 1
    assert fake.posts[0]["prompt"] == pkg["beats"][0]["vo"]
    assert fake.posts[0]["provider"]["options"]["heygen"]["voice_id"] == presenter_mod.HOST_VOICES["es"]
    assert fake.posts[0]["input_references"][0]["image_url"]["url"].startswith("data:image/png")
    assert fake.posts[0]["provider"]["options"]["heygen"]["motion_prompt"] == presenter_mod.HANDOFF_MOTION
    assert "last sentence" in presenter_mod.HANDOFF_MOTION and "camera" in presenter_mod.HANDOFF_MOTION
    assert sorted(cast["takes"]) == [0]
    # His loop is the owner's recording, committed: never generated, only looked up.
    assert cast["loop"] == presenter_mod.PHOTO_DIR / "assets" / "martin-loop.mp4"
    assert cast["report"]["loop"]["status"] == "reused"
    report = {}
    assert presenter_mod.bubble_loop(report, "nobody") is None and report["loop"]["status"] == "missing"



def test_lucias_take_is_refused_cleanly_under_the_budget(tip, tmp_path, monkeypatch):
    fake = _FakeImageAPI()
    _tip_env(monkeypatch, tmp_path, fake)
    monkeypatch.setenv("SHORTS_AI_SHORT_BUDGET_USD", "0.30")
    pkg = dict(pkg_mod.validate_package(tip))
    pkg["beats"] = build_mod.tip_beats(pkg)
    report = {}
    image = _touch(tmp_path / "setting.png")
    lines = {0: pkg["beats"][0]["vo"]}
    with fake() as client:
        takes = presenter_mod.host_takes(client, genvideo.open_budget(offline=False), image, lines, "es", 0,
                                         tmp_path, report)
    assert takes == {} and not fake.posts
    assert {t["status"] for t in report["takes"]} == {"skipped"} and "short budget" in report["takes"][0]["reason"]



def test_takes_go_one_at_a_time_and_stop_at_the_first_refusal(tip, tmp_path, monkeypatch):
    """host_takes still takes several lines (the classic format); they go one by one."""
    class Refuses2nd(_FakeImageAPI):
        def post(self, url, headers=None, json=None):
            if url != presenter_mod.CHAT_API and len(self.posts) == 1:
                self.posts.append(json)
                return _ORResp(402, {"error": {"message": "Insufficient credits"}})
            return super().post(url, headers, json)

    fake = Refuses2nd()
    _tip_env(monkeypatch, tmp_path, fake)
    order = []
    real_wait = genvideo.wait_for
    monkeypatch.setattr(genvideo, "wait_for", lambda client, key, jobs, *a, **kw: order.append(
        ("wait", [j["beat"] for j in jobs], len(fake.posts))) or real_wait(client, key, jobs, *a, **kw))
    report = {}
    lines = {0: "Si ya madrugo para hacer el pan, que las facturas se hagan solas.",
             1: "Automatiza primero lo aburrido.", 2: "Martín, cuéntales tú por dónde empezar."}
    with fake() as client:
        takes = presenter_mod.host_takes(client, genvideo.open_budget(offline=False), _touch(tmp_path / "s.png"),
                                         lines, "es", 2, tmp_path, report)
    assert order == [("wait", [0], 1)]          # take 0 finished before take 1 was sent
    assert len(fake.posts) == 2 and list(takes) == [0]   # take 2 never sent, never paid
    status = {t["beat"]: (t["status"], t.get("reason", "")) for t in report["takes"]}
    assert status[1][0] == "failed" and "Insufficient credits" in status[1][1]
    assert status[2][0] == "skipped" and report["spent_usd"] == 0.12


def test_a_refused_take_costs_nothing_more_and_is_reported(tip, tmp_path, monkeypatch):
    class Refuses(_FakeImageAPI):
        def post(self, url, headers=None, json=None):
            if url != presenter_mod.CHAT_API:
                self.posts.append(json)
                return _ORResp(402, {"error": {"message": "Insufficient credits"}})
            return super().post(url, headers, json)

    fake = Refuses()
    _tip_env(monkeypatch, tmp_path, fake)
    pkg = dict(pkg_mod.validate_package(tip))
    pkg["beats"] = build_mod.tip_beats(pkg)
    cast = build_mod.tip_cast(pkg, tmp_path, genvideo.open_budget(offline=False), offline=False)
    assert cast["takes"] == {} and len(fake.posts) == 1
    assert cast["report"]["takes"][0]["status"] == "failed" and "Insufficient credits" in cast["report"]["takes"][0]["reason"]
    assert cast["report"]["spent_usd"] == 0.04        # the setting only


def test_a_refused_setting_falls_through_to_the_next_model_for_free(tmp_path, monkeypatch):
    first, second, third = presenter_mod.SETTING_MODELS
    fake = _FakeImageAPI(refuse_models=(first,))
    _tip_env(monkeypatch, tmp_path, fake)
    report = {}
    with fake() as client:
        out = presenter_mod.setting_image(client, genvideo.open_budget(offline=False),
                                          "a small bakery at dawn with fresh bread", tmp_path / "s.png", report)
    assert out and out.exists()
    assert [c["model"] for c in fake.chat] == [first, second]
    assert fake.chat[0]["modalities"] == ["image"] and fake.chat[1]["modalities"] == ["image", "text"]
    assert report["setting"]["model"] == second and report["spent_usd"] == 0.04
    assert report["setting"]["tried"][0]["status"] == "failed"

    # Every model refuses: no image, nothing spent, she speaks from her own photo.
    fake = _FakeImageAPI(refuse_models=(first, second, third))
    _tip_env(monkeypatch, tmp_path, fake)
    report = {}
    with fake() as client:
        assert presenter_mod.setting_image(client, genvideo.open_budget(offline=False),
                                           "a small bakery at dawn with fresh bread", tmp_path / "t.png",
                                           report) is None
    assert report["setting"]["status"] == "failed" and len(report["setting"]["tried"]) == 3
    assert report.get("spent_usd", 0) == 0



def test_a_tip_short_without_lucia_is_published_with_a_warning():
    report = {"takes": [{"beat": 0, "status": "failed", "reason": "OpenRouter refused (402): Insufficient credits"}]}
    warning = build_mod.presenter_missing_warning(True, [0], True, report)
    assert warning and "Lucía's take missing" in warning and "published without her on camera" in warning
    assert warning.count("Insufficient credits") == 1
    assert build_mod.presenter_missing_warning(True, [], True, report) is None        # the take was made
    assert build_mod.presenter_missing_warning(True, [0], False, report) is None      # no key: smoke test
    assert build_mod.presenter_missing_warning(False, [0], True, report) is None      # classic format
    assert "not made" in build_mod.presenter_missing_warning(True, [0], True, {})
    assert not hasattr(build_mod, "presenter_missing_error")                          # no longer a QA error



def test_tip_props_carry_her_take_his_loop_and_the_cards(tip):
    pkg = dict(pkg_mod.validate_package(tip))
    pkg["beats"] = build_mod.tip_beats(pkg)
    timeline = [{"from": i * 90, "duration": 90, "show": b["show"]} for i, b in enumerate(pkg["beats"])]
    timeline[0]["presenter"] = "presenter/p00.mp4"
    props = build_mod.remotion_props(pkg, {"timeline": timeline, "words": [], "frames": 90 * len(timeline),
                                           "loop": "presenter/loop.mp4", "loop_frames": 300,
                                           "photo": "presenter/photo.png", "partner_photo": "presenter/partner.jpg",
                                           "face_y": 0.3, "loop_face_y": 0.21})
    assert props["format"] == "tip" and "presenterClip" not in props
    assert props["bubbleLoop"] == "presenter/loop.mp4" and props["bubbleLoopFrames"] == 300
    assert props["presenterName"] == "Lucía" and props["partnerName"] == "Martín"
    assert props["partnerPhoto"] == "presenter/partner.jpg" and props["faceY"] == 0.3 and props["loopFaceY"] == 0.21
    take = props["beats"][0]
    assert take["presenter"] == "presenter/p00.mp4" and take["presenterName"] == "Lucía" and take["kind"] == "hook"
    # The promise card comes in when her joke ends, by the joke's share of the words.
    joke, said = len(pkg["opener"]["line"].split()), len(pkg["beats"][0]["vo"].split())
    assert take["overlayFrom"] == round(90 * joke / said) and 0 < take["overlayFrom"] < 90
    assert "overlayFrom" not in props["beats"][1]
    assert [b["number"] for b in props["beats"][:2]] == [0, 1] and props["beats"][-2]["number"] == 0
    assert props["beats"][-1]["kind"] == "outro" and props["beats"][-1]["url"]
    assert props["cover"]["kicker"] == pkg["beats"][0].get("kicker", "")


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_a_take_is_cut_at_its_speech_so_the_face_never_freezes(tmp_path):
    import subprocess

    from plugins.shorts.studio import media

    take = tmp_path / "take.wav"
    # 0.8 s still, 2 s of "speech", 1.2 s still: what an Avatar IV take sounds like.
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=0.8",
                    "-f", "lavfi", "-i", "sine=frequency=300:duration=2:sample_rate=48000",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=1.2",
                    "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1", str(take)], check=True)
    begin, end = media.speech_bounds(take)
    assert 0.7 <= begin <= 0.9 and 2.7 <= end <= 2.9


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_a_take_plays_faster_with_lips_and_voice_together(tmp_path):
    import subprocess

    from plugins.shorts.studio import media

    take = tmp_path / "take.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x568:rate=30:duration=3.36",
                    "-f", "lavfi", "-i", "sine=frequency=300:duration=3.36:sample_rate=48000",
                    "-shortest", "-c:v", "libx264", "-c:a", "aac", str(take)], check=True)
    fast = media.tempo(take, tmp_path / "fast.mp4", 1.12)
    assert abs(media.duration(fast) - 3.0) < 0.1
    assert media.has_audio(fast) and media.has_video(fast)
    assert media.tempo(take, tmp_path / "same.mp4", 1.0) == take          # nothing to do at 1.0


def test_tip_shorts_play_jazz_or_lounge(tip, tmp_path, monkeypatch):
    from plugins.shorts.studio import sources

    seen = {}

    def find(mood, avoid, seed, pages=None):
        seen["pages"] = pages
        return None

    monkeypatch.setattr(sources, "find_music", find)
    build_mod.gather_music(pkg_mod.validate_package(tip), tmp_path, "seed", offline=False)
    assert seen["pages"] == sources.ELEGANT_MUSIC_PAGES
    assert all("jazz" in p or "lounge" in p for p in seen["pages"])


def test_martins_free_voice_is_made_before_anything_is_paid(tip, tmp_path, monkeypatch):
    from plugins.shorts.studio import voice as voice_mod

    pkg = dict(pkg_mod.validate_package(tip))
    pkg["beats"] = build_mod.tip_beats(pkg)
    said = []
    monkeypatch.setattr(voice_mod, "synthesize", lambda text, v, rate, mp3: said.append((text, v)) or [])
    voiced = build_mod.prevoice(pkg, tmp_path, fake_voice=False, skip=build_mod.on_camera(pkg["beats"]))
    # Every line of Martín's (cards, CTA, outro) in his voice; none of Lucía's.
    assert sorted(voiced) == list(range(1, len(pkg["beats"])))
    assert {v for _, v in said} == {pkg["voice"]}


def test_edge_is_retried_for_over_a_minute(monkeypatch, tmp_path):
    from plugins.shorts.studio import voice as voice_mod

    calls, waits = [], []

    async def refuse(*a, **kw):
        calls.append(1)
        raise RuntimeError("403, message='Invalid response status'")

    monkeypatch.setenv("SHORTS_TTS_ENGINE", "azure")
    monkeypatch.setattr(voice_mod, "_edge_stream", refuse)
    monkeypatch.setattr(voice_mod.time, "sleep", waits.append)
    with pytest.raises(voice_mod.VoiceError, match="403"):
        voice_mod.synthesize("hola", "es-ES-AlvaroNeural", "+0%", tmp_path / "a.mp3")
    assert len(calls) == len(voice_mod.BACKOFF_S) + 1 and sum(waits) >= 60


def test_azure_speech_comes_first_with_the_same_voice_and_edge_is_the_fallback(monkeypatch, tmp_path):
    import httpx

    from plugins.shorts.studio import media
    from plugins.shorts.studio import voice as voice_mod

    sent = []

    class Resp:
        def __init__(self, code, content=b"ID3 mp3"):
            self.status_code, self.content, self.text = code, content, "denied"

    monkeypatch.setenv("SHORTS_TTS_ENGINE", "azure")
    monkeypatch.setenv("SHORTS_AZURE_SPEECH_KEY", "az-key")
    monkeypatch.setenv("SHORTS_AZURE_SPEECH_REGION", "westeurope")
    monkeypatch.setattr(media, "duration", lambda p: 2.0)
    sped = []
    monkeypatch.setattr(media, "tempo", lambda src, dst, factor: sped.append(factor) or dst.write_bytes(src.read_bytes()))
    monkeypatch.setattr(httpx, "post", lambda url, content, timeout, headers: sent.append((url, content, headers)) or Resp(200))
    words = voice_mod.synthesize("Elige una tarea & mide", "es-ES-AlvaroNeural", "+5%", tmp_path / "a.mp3")
    url, ssml, headers = sent[0]
    assert url == "https://westeurope.tts.speech.microsoft.com/cognitiveservices/v1"
    assert b"es-ES-AlvaroNeural" in ssml and b"&amp; mide" in ssml and b"rate='+5%'" in ssml
    voice_mod.synthesize("Hola", "es-ES-Tristan:DragonHDLatestNeural", "+5%", tmp_path / "hd.mp3")
    assert b"name='es-ES-Tristan:DragonHDLatestNeural'" in sent[1][1] and b"prosody" not in sent[1][1]
    assert sped == [voice_mod.HD_TEMPO]                   # only the HD voice, which takes no rate, is sped up
    assert headers["Ocp-Apim-Subscription-Key"] == "az-key" and len(words) == 5

    # Azure refuses: Edge is asked.
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: Resp(401, b""))
    edge = []

    async def fake_edge(text, v, rate, mp3):
        edge.append(v)
        mp3.write_bytes(b"x")
        return [{"text": "hola", "start": 0.0, "end": 0.5}]

    monkeypatch.setattr(voice_mod, "_edge_stream", fake_edge)
    assert voice_mod.synthesize("hola", "es-ES-Tristan:DragonHDLatestNeural", "+0%", tmp_path / "b.mp3")[0]["text"] == "hola"
    assert edge == ["es-ES-AlvaroNeural"]                 # Edge has no HD voices


def test_a_normalised_tip_package_validates_again_in_the_renderer(tip):
    # Hermes submits the package validate_package returned, forced voice
    # included; the renderer validates it again. The first production run with
    # the HD voice died there (2026-10-12): the check only knew "…Neural".
    once = pkg_mod.validate_package(tip)
    assert ":" in once["voice"]
    twice = pkg_mod.validate_package(json.loads(json.dumps(once)))
    assert twice["voice"] == once["voice"] and twice["opener"] == once["opener"]
    tip["voice"] = "es-ES-Bad Voice"
    with pytest.raises(pkg_mod.PackageError, match="not a neural voice name"):
        pkg_mod.validate_package(tip)


# ---------------------------------------------------------------------------
# The free voice: Kokoro on the runner, timings aligned to the script
# ---------------------------------------------------------------------------

def _kokoro_env(monkeypatch, tmp_path, spoken, heard=None, *, fail=None):
    from plugins.shorts.studio import media

    def fake_kokoro(text, voice, lang, wav):
        if fail:
            raise fail
        spoken.append((text, voice, lang))
        wav.write_bytes(b"RIFF")
        return 4.0

    monkeypatch.setenv("SHORTS_TTS_ENGINE", "kokoro")                 # both languages on Kokoro for these tests
    monkeypatch.setenv("SHORTS_MODEL_DIR", str(tmp_path / "models"))
    monkeypatch.setattr(voice_mod, "_kokoro", fake_kokoro)
    monkeypatch.setattr(voice_mod, "heard_words", lambda wav, lang: list(heard or []))
    monkeypatch.setattr(media, "ffmpeg", lambda *a, what, **kw: Path(a[-1]).write_bytes(b"mp3"))


def test_kokoro_is_the_default_engine_and_maps_each_voice(tmp_path, monkeypatch):
    spoken = []
    _kokoro_env(monkeypatch, tmp_path, spoken)
    assert voice_mod.engine() == "kokoro"
    words = voice_mod.synthesize("Elige una tarea y mide.", pkg_mod.TIP_VOICES["es"], "+0%", tmp_path / "a.mp3")
    assert spoken == [("Elige una tarea y mide.", "em_alex", "es")]     # Martín's Azure name maps to his Kokoro voice
    assert (tmp_path / "a.mp3").exists() and (tmp_path / "a.wav").exists()
    assert [w["text"] for w in words] == ["Elige", "una", "tarea", "y", "mide."]   # nothing heard: spread evenly
    voice_mod.synthesize("Start with one boring task.", pkg_mod.TIP_VOICES["en"], "+0%", tmp_path / "b.mp3")
    assert spoken[-1][1:] == ("am_michael", "en")
    voice_mod.synthesize("Hola", "ef_dora", "+0%", tmp_path / "c.mp3")   # Lucía's free voice passes through
    assert spoken[-1][1:] == ("ef_dora", "es")
    monkeypatch.setenv("SHORTS_KOKORO_VOICE_ES", "em_santa")              # the owner's pick after the smoke render
    voice_mod.synthesize("Hola", pkg_mod.TIP_VOICES["es"], "+0%", tmp_path / "d.mp3")
    assert spoken[-1][1] == "em_santa"
    monkeypatch.setenv("SHORTS_KOKORO_VOICE_ES", "not a voice")
    voice_mod.synthesize("Hola", pkg_mod.TIP_VOICES["es"], "+0%", tmp_path / "e.mp3")
    assert spoken[-1][1] == "em_alex"
    assert voice_mod.host_voice("es", presenter_mod.HOST_FALLBACK_VOICES) == "ef_dora"
    monkeypatch.setenv("SHORTS_TTS_ENGINE", "azure")
    assert voice_mod.host_voice("en", presenter_mod.HOST_FALLBACK_VOICES) == presenter_mod.HOST_FALLBACK_VOICES["en"]
    monkeypatch.setenv("SHORTS_TTS_ENGINE", "nonsense")
    assert voice_mod.engine() == "kokoro"


def test_each_language_has_its_own_engine_and_spanish_is_azure_by_default(monkeypatch):
    for name in ("SHORTS_TTS_ENGINE", "SHORTS_TTS_ENGINE_ES", "SHORTS_TTS_ENGINE_EN"):
        monkeypatch.delenv(name, raising=False)
    # Spain Spanish lives on Azure (es-ES Tristan); Kokoro's Spanish male voices sounded Latin American (owner, 2026-10-15).
    assert voice_mod.engine("es") == "azure" and voice_mod.engine("en") == "kokoro" and voice_mod.engine() == "kokoro"
    assert voice_mod.host_voice("es", presenter_mod.HOST_FALLBACK_VOICES) == presenter_mod.HOST_FALLBACK_VOICES["es"]
    assert voice_mod.host_voice("en", presenter_mod.HOST_FALLBACK_VOICES) == "af_heart"
    monkeypatch.setenv("SHORTS_TTS_ENGINE", "kokoro")                 # the global switch covers both (the smoke matrix)
    assert voice_mod.engine("es") == "kokoro"
    monkeypatch.setenv("SHORTS_TTS_ENGINE_ES", "azure")               # a language's own switch wins
    assert voice_mod.engine("es") == "azure" and voice_mod.engine("en") == "kokoro"
    monkeypatch.setenv("SHORTS_TTS_ENGINE_ES", "nonsense")
    assert voice_mod.engine("es") == "kokoro"


def test_kokoro_words_follow_the_script_at_the_heard_times(tmp_path, monkeypatch):
    heard = [{"text": "Big", "start": 0.1, "end": 0.3}, {"text": "Lobster", "start": 0.3, "end": 0.6},
             {"text": "ahorra", "start": 0.7, "end": 1.0}, {"text": "mil", "start": 1.1, "end": 1.3},
             {"text": "cincuenta", "start": 1.3, "end": 1.7}, {"text": "euros", "start": 1.8, "end": 2.1}]
    spoken = []
    _kokoro_env(monkeypatch, tmp_path, spoken, heard)
    words = voice_mod.synthesize("BigLobster ahorra 1.050 euros al mes.", "em_alex", "+0%", tmp_path / "a.mp3")
    assert [w["text"] for w in words] == ["BigLobster", "ahorra", "1.050", "euros", "al", "mes."]
    by = {w["text"]: (w["start"], w["end"]) for w in words}
    assert by["BigLobster"][0] == 0.1 and 0.5 <= by["BigLobster"][1] <= 0.6   # heard as two words: their span
    assert by["ahorra"] == (0.7, 1.0)                                          # heard as is
    assert by["1.050"][0] == 1.1 and 1.6 <= by["1.050"][1] <= 1.7              # read as "mil cincuenta": their span
    assert by["euros"] == (1.8, 2.1)
    assert 2.1 <= by["al"][0] < by["mes."][0] < by["mes."][1] <= 4.0   # unheard tail sits between euros and the end
    # Nothing heard at all: an even spread, still the script's words.
    assert [w["text"] for w in voice_mod.align_words("Hola mundo", [], 1.0)] == ["Hola", "mundo"]


def test_kokoro_failure_falls_back_to_azure_with_the_neural_twin(tmp_path, monkeypatch):
    import httpx

    from plugins.shorts.studio import media

    spoken, sent = [], []
    _kokoro_env(monkeypatch, tmp_path, spoken, fail=RuntimeError("onnxruntime missing"))
    monkeypatch.setenv("SHORTS_AZURE_SPEECH_KEY", "az-key")
    monkeypatch.setenv("SHORTS_AZURE_SPEECH_REGION", "westeurope")
    monkeypatch.setattr(media, "duration", lambda p: 2.0)
    monkeypatch.setattr(media, "tempo", lambda src, dst, factor: dst.write_bytes(src.read_bytes()) or dst)
    monkeypatch.setattr(voice_mod.time, "sleep", lambda s: None)

    class Resp:
        status_code, content, text = 200, b"ID3 mp3", ""

    monkeypatch.setattr(httpx, "post", lambda url, content, timeout, headers: sent.append(content) or Resp())
    words = voice_mod.synthesize("Hola", "ef_dora", "+0%", tmp_path / "a.mp3")      # Lucía's Kokoro id → Elvira
    assert spoken == [] and b"es-ES-ElviraNeural" in sent[0] and len(words) == 1
    voice_mod.synthesize("Hola", pkg_mod.TIP_VOICES["es"], "+0%", tmp_path / "b.mp3")   # Martín keeps his HD voice
    assert b"Tristan:DragonHDLatestNeural" in sent[1]


# ---------------------------------------------------------------------------
# The brand look, the article's pictures, the promise and the critic (T3, T4)
# ---------------------------------------------------------------------------

HTML_WITH_IMAGES = (
    '<html><head><meta property="og:image" content="https://biglobster.top/assets/blog/covers/x.jpg"></head>'
    '<body><article class="article-body"><div class="article-cover"><img src="/assets/blog/covers/x.jpg" alt="c"></div>'
    '<p>Texto...</p><figure class="article-infographic"><img src="/assets/blog/infographics/x.jpg" '
    'alt="Infografía: el 23% de las pymes ganó clientes"><figcaption>Lo que declararon 533 pymes. '
    'Fuente: <b>PYMNTS</b>.</figcaption></figure></article></body></html>'
)


def test_the_brand_palette_is_the_look_and_never_rotates(tip, submit_env):
    assert tip["style"]["palette"] == "brand" == pkg_mod.BRAND_PALETTE and "brand" in pkg_mod.PALETTES
    ledger.add(_entry("es-x-1", palette="brand", motif="grid"))       # the look repeats; the motif still rotates
    ledger.add(_entry("es-x-2", palette="brand", motif="dots"))
    out = json.loads(st.handle_shorts_studio({"action": "submit", "package": tip}))
    assert out["success"], out


def test_the_written_promise_must_say_what_the_first_sentence_says(tip):
    pkg = pkg_mod.validate_package(tip)            # "Automatiza primero lo aburrido." / "...lo *aburrido*"
    assert pkg["beats"][0]["onscreen"] == "Automatiza primero lo *aburrido*"
    bad = copy.deepcopy(tip)
    bad["beats"][0]["onscreen"] = "Guía 2026 para pymes"
    with pytest.raises(pkg_mod.PackageError) as exc:
        pkg_mod.validate_package(bad)
    assert "written promise" in str(exc.value) and "Guía 2026" in str(exc.value)
    # A shared figure is enough, accents and asterisks aside.
    fig = copy.deepcopy(tip)
    fig["beats"][0]["vo"] = "El 73% ahorró tiempo en tres meses. " + fig["beats"][0]["vo"]
    fig["beats"][0]["onscreen"] = "*73%* menos tiempo, en tres meses"
    pkg_mod.validate_package(fig)
    acc = copy.deepcopy(tip)
    acc["beats"][0]["vo"] = "Recupera dos horas cada semana sin contratar. Y sin programar."
    acc["beats"][0]["onscreen"] = "Recupera *dos horas* a la semana"
    pkg_mod.validate_package(acc)
    assert pkg_mod._key_words("¿Cuántas facturas pagas dos veces?") == {"cuantas", "facturas", "pagas", "veces"}


def test_in_the_tip_format_lucia_has_ten_seconds_to_land_the_promise(tip):
    # The joke comes first by design (owner, 2026-10-17): no 8-word cap on the hook's first
    # sentence, and the written promise may echo any sentence of the hook.
    slow = copy.deepcopy(tip)
    slow["beats"][0]["vo"] = "Si repites facturas cada mes a mano: automatiza primero lo aburrido."
    assert pkg_mod.validate_package(slow)["beats"][0]["vo"].startswith("Si repites")
    # ...but opener + hook must still fit in her first 10 seconds.
    late = copy.deepcopy(tip)
    late["opener"]["line"] = "Si ya madrugo tanto para hacer el pan cada día, que al menos las facturas se vayan solas."
    late["opener"]["handoff"] = "Martín, explícalo."
    late["beats"][0]["vo"] = "Repites las mismas facturas cada mes a mano. Automatiza primero lo aburrido."
    with pytest.raises(pkg_mod.PackageError) as exc:
        pkg_mod.validate_package(late)
    assert "within her first 10 seconds" in str(exc.value) and "on camera" not in str(exc.value)
    # Her pace is the take's: the presenter's words a second at the take's tempo.
    from plugins.shorts.studio import presenter
    assert pkg_mod.TIP_TAKE_WORDS_PER_SECOND == {
        lang: round(wps * presenter.TAKE_TEMPO, 1) for lang, wps in presenter.WORDS_PER_SECOND.items()}


def test_the_infographic_goes_on_one_card_only(tip):
    pkg = pkg_mod.validate_package(tip)
    assert pkg["beats"][2]["visual"] == "infographic" and "visual" not in pkg["beats"][1]
    bad = copy.deepcopy(tip)
    bad["beats"][0]["visual"] = "infographic"
    with pytest.raises(pkg_mod.PackageError, match="not the hook"):
        pkg_mod.validate_package(bad)
    bad = copy.deepcopy(tip)
    bad["beats"][1]["visual"] = "infographic"
    with pytest.raises(pkg_mod.PackageError, match="one card only"):
        pkg_mod.validate_package(bad)
    bad = copy.deepcopy(tip)
    bad["beats"][2]["visual"] = "chart"
    with pytest.raises(pkg_mod.PackageError, match="visual: one of"):
        pkg_mod.validate_package(bad)


def test_article_images_find_the_cover_and_the_raster_infographic():
    found = article_mod.article_images(HTML_WITH_IMAGES, "https://biglobster.top/es/blog/x.html")
    assert found["cover"] == "https://biglobster.top/assets/blog/covers/x.jpg"
    assert found["infographic"]["url"] == "https://biglobster.top/assets/blog/infographics/x.jpg"
    assert found["infographic"]["alt"].startswith("Infografía")
    assert found["infographic"]["caption"] == "Lo que declararon 533 pymes. Fuente: PYMNTS."
    assert article_mod.article_images("<html><body><p>hi</p></body></html>", "https://biglobster.top/a") == {
        "cover": None, "infographic": None}


def test_the_article_action_reports_the_pictures_the_producer_may_use(home, monkeypatch):
    html = HTML_WITH_IMAGES.replace("<p>Texto...</p>", "<p>" + ARTICLE_TEXT + "</p>")
    monkeypatch.setattr("plugins.shorts.studio.article.fetch_html", lambda url: html)
    out = json.loads(st.handle_shorts_studio({"action": "article", "url": "https://biglobster.top/es/blog/x.html"}))
    assert out["success"], out
    assert out["cover"] is True and out["infographic"]["present"] is True
    assert out["infographic"]["alt"].startswith("Infografía") and "73" in " ".join(out["figures"])


def test_the_articles_pictures_reach_the_render(tip, tmp_path, monkeypatch):
    import httpx

    class Resp:
        status_code, text = 200, HTML_WITH_IMAGES
        content, encoding = HTML_WITH_IMAGES.encode("utf-8"), "utf-8"

        def raise_for_status(self):
            pass

    class Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def close(self):
            pass

        def get(self, url):
            return Resp()

    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr(build_mod.article, "url_is_safe", lambda url: True)     # the guard has its own test
    got = []
    monkeypatch.setattr(build_mod.sources, "download", lambda url, dest, **kw: got.append(url) or _touch(dest))
    pkg = dict(pkg_mod.validate_package(tip))
    pkg["beats"] = build_mod.tip_beats(pkg)
    public = tmp_path / "public"
    images = build_mod.gather_article_images(pkg, tmp_path, public, offline=False)
    assert images == {"cover": "article/cover.jpg", "infographic": "article/infographic.jpg",
                      "infographic_alt": "Infografía: el 23% de las pymes ganó clientes"}
    assert got == ["https://biglobster.top/assets/blog/covers/x.jpg",
                   "https://biglobster.top/assets/blog/infographics/x.jpg"]
    assert (public / "article" / "cover.jpg").exists()
    timeline = [{"from": i * 90, "duration": 90, "show": b["show"]} for i, b in enumerate(pkg["beats"])]
    props = build_mod.remotion_props(pkg, {"timeline": timeline, "words": [], "frames": 90 * len(timeline),
                                           "article_cover": images["cover"],
                                           "article_infographic": images["infographic"]})
    assert props["articleCover"] == "article/cover.jpg" and props["articleInfographic"] == "article/infographic.jpg"
    assert props["beats"][2]["visual"] == "infographic" and "visual" not in props["beats"][1]
    assert props["palette"] == "brand"
    # Offline (tests, dry runs): nothing fetched. A picture off the article's host is left alone.
    assert build_mod.gather_article_images(pkg, tmp_path, public, offline=True) == {
        "cover": None, "infographic": None, "infographic_alt": ""}
    Resp.content = HTML_WITH_IMAGES.replace("https://biglobster.top/assets/blog/covers/x.jpg", "https://cdn.example.com/x.jpg").encode("utf-8")
    got.clear()
    images = build_mod.gather_article_images(pkg, tmp_path, public, offline=False)
    assert images["cover"] is None and images["infographic"] == "article/infographic.jpg"


class _CriticAPI:
    def __init__(self, content, status=200):
        self.content, self.status, self.calls = content, status, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def close(self):
        pass

    def post(self, url, headers=None, json=None):
        self.calls.append((url, headers, json))
        if self.status != 200:
            return _ORResp(self.status, {"error": "boom"})
        return _ORResp(data={"choices": [{"message": {"content": self.content}}]})


def test_the_critic_returns_the_openings_problems_and_a_better_first_line(tip, monkeypatch):
    from plugins.shorts import critic

    monkeypatch.setattr(critic, "config", lambda: {"enabled": True, "model": "m", "min": 7, "key": "sk-crit"})
    pkg = pkg_mod.validate_package(tip)
    api = _CriticAPI(json.dumps({"score": 4, "problems": ["la primera frase es un tema, no una promesa", "la broma no lleva al consejo"],
                                 "rewrite": {"hook": "Recupera dos horas cada semana sin contratar.",
                                             "onscreen": "Recupera *dos horas* a la semana"}}))
    problems = critic.review(pkg, ARTICLE_TEXT, client=api)
    assert problems[0].startswith("guionista: the opening scores 4/10 (minimum 7): la primera frase es un tema")
    assert problems[1] == "guionista: la broma no lleva al consejo"
    assert "Recupera dos horas cada semana" in problems[2] and "on screen" in problems[2]
    url, headers, sent = api.calls[0]
    assert url == critic.CHAT_API and headers == {"Authorization": "Bearer sk-crit"}
    assert sent["model"] == "m" and sent["provider"] == {"data_collection": "deny"}
    assert sent["response_format"] == {"type": "json_object"}
    script = sent["messages"][1]["content"]
    assert "Automatiza primero lo aburrido" in script and "LUCIA (joke" in script and "MARTIN (card)" in script
    # The joke opens every short by design: the rubric judges the whole hook, within her first ten seconds.
    assert "before the hook by design" in script and "judge the HOOK as a whole" in critic.RUBRIC
    assert "first ten seconds" in critic.RUBRIC and "three seconds" not in critic.RUBRIC
    assert "ARTICLE (beginning)" in script and sent["messages"][0]["content"] == critic.RUBRIC
    # Good enough, fenced JSON, nonsense, an API error, or the critic switched off: no objection.
    assert critic.review(pkg, ARTICLE_TEXT, client=_CriticAPI("```json\n{\"score\": 9, \"problems\": []}\n```")) == []
    assert critic.review(pkg, ARTICLE_TEXT, client=_CriticAPI("not json at all")) == []
    assert critic.review(pkg, ARTICLE_TEXT, client=_CriticAPI("{}", status=500)) == []
    monkeypatch.setattr(critic, "config", lambda: {"enabled": False, "model": "m", "min": 7, "key": "sk-crit"})
    assert critic.review(pkg, ARTICLE_TEXT, client=api) == [] and len(api.calls) == 1
    monkeypatch.setattr(critic, "config", lambda: {"enabled": True, "model": "m", "min": 7, "key": ""})
    assert critic.review(pkg, ARTICLE_TEXT, client=api) == [] and len(api.calls) == 1


def test_submit_refuses_a_script_the_critic_rejects(tip, submit_env, monkeypatch):
    monkeypatch.setattr("plugins.shorts.critic.review",
                        lambda pkg, text, **kw: ["guionista: the opening scores 3/10 (minimum 7): a topic, not a promise"])
    out = json.loads(st.handle_shorts_studio({"action": "submit", "package": tip}))
    assert not out["success"] and "value proposition" in out["error"]
    assert out["problems"] == ["guionista: the opening scores 3/10 (minimum 7): a topic, not a promise"]
    assert submit_env == []


def test_the_render_farm_refuses_private_article_hosts_and_redirects(monkeypatch):
    """Without Hermes's tools/ (the Actions checkout), article.public_url is the guard."""
    import types

    monkeypatch.setattr(article_mod, "url_is_safe", lambda url: article_mod.public_url(url))
    resolves = {"biglobster.top": "93.184.216.34", "metadata.internal": "169.254.169.254", "home.test": "10.0.0.5"}
    monkeypatch.setattr(article_mod.socket, "getaddrinfo",
                        lambda host, *a, **kw: [(None, None, None, None, (resolves[host], 443))] if host in resolves else (_ for _ in ()).throw(article_mod.socket.gaierror()))
    assert article_mod.public_url("https://biglobster.top/es/blog/x.html")
    assert not article_mod.public_url("http://biglobster.top/x")                 # https only
    assert not article_mod.public_url("https://metadata.internal/latest")      # link-local
    assert not article_mod.public_url("https://home.test/x")                    # private
    assert not article_mod.public_url("https://127.0.0.1/x") and not article_mod.public_url("https://nowhere.invalid/x")
    with pytest.raises(ValueError, match="unsafe"):
        article_mod.fetch_html("https://home.test/x")
    hop = types.SimpleNamespace(is_redirect=True, next_request=types.SimpleNamespace(url="https://metadata.internal/"))
    with pytest.raises(ValueError, match="blocked redirect"):
        article_mod.guard_redirect(hop)
    article_mod.guard_redirect(types.SimpleNamespace(is_redirect=True, next_request=types.SimpleNamespace(url="https://biglobster.top/a")))
    article_mod.guard_redirect(types.SimpleNamespace(is_redirect=False, next_request=None))
    # The pictures go through the same guard before they are downloaded.
    got = []
    monkeypatch.setattr(build_mod.sources, "download", lambda url, dest, **kw: got.append(url) or _touch(dest))
    monkeypatch.setattr(build_mod.article, "fetch_html", lambda url, **kw: HTML_WITH_IMAGES.replace(
        "https://biglobster.top/assets/blog/covers/x.jpg", "https://biglobster.top/covers/x.jpg"))
    monkeypatch.setattr(build_mod.article, "url_is_safe", lambda url: "infographics" not in url)
    pkg = {"article": {"url": "https://biglobster.top/es/blog/x.html"}}
    images = build_mod.gather_article_images(pkg, Path("/tmp"), Path("/tmp/shorts-test-public"), offline=False)
    assert images["cover"] == "article/cover.jpg" and images["infographic"] is None and got == ["https://biglobster.top/covers/x.jpg"]


def test_a_mistyped_critic_bar_never_raises(monkeypatch):
    from plugins.shorts import critic

    values = {"SHORTS_CRITIC_MIN": "seven", "OPENROUTER_API_KEY": "sk"}
    monkeypatch.setattr(critic, "env", lambda name, default="": values.get(name, default))
    assert critic.config()["min"] == critic.DEFAULT_MIN
    values["SHORTS_CRITIC_MIN"] = "42"
    assert critic.config()["min"] == 10
    values["SHORTS_CRITIC_MIN"] = "5"
    assert critic.config()["min"] == 5 and critic.config()["enabled"] and critic.config()["key"] == "sk"


def test_going_live_picks_up_the_shorts_handed_over_in_shadow(home, tmp_path, monkeypatch):
    for rid, created in (("es-today-1", None), ("en-old-1", "2026-10-01T00:00:00Z")):
        d = tmp_path / rid
        _touch(d / "out" / "master.mp4")
        ledger.add(_entry(rid, dir=str(d)))
        fields = {"state": "handed_off", "x_handoff_at": "2026-10-15T08:20:00Z",
                  "qa": {"passed": True, "errors": [], "warnings": []}}
        if created:
            fields["created_at"] = created
        ledger.update(rid, **fields)
    # Shadow mode: nothing moves.
    monkeypatch.setattr(st, "publish_mode", lambda: "shadow")
    out = json.loads(st.handle_shorts_studio({"action": "status"}))
    assert out["shorts"] == [] and ledger.get("es-today-1")["state"] == "handed_off"
    # Live: today's shadow hand-off is ready for the publisher; the old one is left alone.
    monkeypatch.setattr(st, "publish_mode", lambda: "live")
    out = json.loads(st.handle_shorts_studio({"action": "status"}))
    assert [(s_["request_id"], s_["state"]) for s_ in out["shorts"]] == [("es-today-1", "ready")]
    assert out["shorts"][0]["files"]["master"].endswith("master.mp4")
    today = ledger.get("es-today-1")
    assert today["state"] == "ready" and not today.get("x_handoff_at") and "live now" in today["notes"][-1]
    assert ledger.get("en-old-1")["state"] == "handed_off"
    # A second status does not touch it again, and a shadow hand-off without files is not promoted.
    ledger.add(_entry("es-nofiles-1", dir=str(tmp_path / "nofiles")))
    ledger.update("es-nofiles-1", state="handed_off", x_handoff_at="2026-10-15T08:20:00Z")
    out = json.loads(st.handle_shorts_studio({"action": "status"}))
    assert [s_["request_id"] for s_ in out["shorts"]] == ["es-today-1"]
    assert ledger.get("es-nofiles-1")["state"] == "handed_off"


def test_the_hand_off_attaches_metadata_free_copies_for_the_relay(tmp_path, monkeypatch):
    from plugins.shorts import buzz_share

    def seg(marker, payload):
        return bytes([0xFF, marker]) + (2 + len(payload)).to_bytes(2, "big") + payload

    # A progressive-looking JPEG: SOI, APP0, COM, DQT, SOS + scan (with a stuffed FF00 and an RST),
    # a second DHT and SOS, then an APP1 after the scans, EOI, and a trailer after the EOI.
    scan1 = b"\x12\xff\x00\x34\xff\xd0\x56"
    scan2 = b"\x78\x9a"
    jpg_bytes = (b"\xff\xd8" + seg(0xE0, b"JFIF") + seg(0xFE, b"hello") + seg(0xDB, b"\x00\x01\x02")
                 + seg(0xDA, b"\x01\x00") + scan1 + seg(0xC4, b"\x10") + seg(0xDA, b"\x01\x01") + scan2
                 + seg(0xE1, b"Exif\x00\x00secret") + b"\xff\xd9" + b"TRAILER")
    jpg = tmp_path / "cover.jpg"
    jpg.write_bytes(jpg_bytes)
    (tmp_path / "cover.buzz.jpg").write_bytes(b"planted")          # a sibling from the artifact is never trusted
    clean = buzz_share.canonical(jpg)
    assert clean == tmp_path / "cover.buzz.jpg"
    assert clean.read_bytes() == (b"\xff\xd8" + seg(0xDB, b"\x00\x01\x02") + seg(0xDA, b"\x01\x00") + scan1
                                  + seg(0xC4, b"\x10") + seg(0xDA, b"\x01\x01") + scan2 + b"\xff\xd9")
    assert not (tmp_path / "cover.buzz.jpg.part").exists()
    for bad, why in ((b"x", "not a JPEG"), (b"\xff\xd8" + seg(0xDB, b"\x00"), "without an end"),
                     (b"\xff\xd8\xff\xdb\xff\xff", "malformed")):
        with pytest.raises(buzz_share.BuzzError, match=why):
            buzz_share.strip_jpeg(bad)
    # An MP4: streams copied, every tag, chapter and encoder stamp dropped, written to a temp file first.
    mp4 = _touch(tmp_path / "master.mp4")
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        Path(args[-1]).write_bytes(b"clean")
        return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(buzz_share.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(buzz_share.subprocess, "run", fake_run)
    out = buzz_share.canonical(mp4)
    assert out == tmp_path / "master.buzz.mp4" and out.read_bytes() == b"clean"
    args = calls[0]
    assert args[0] == "/usr/bin/ffmpeg" and args[-1].endswith("master.buzz.mp4.part")
    assert args[args.index("-map_metadata") + 1] == "-1" and args[args.index("-c") + 1] == "copy"
    assert args.count("+bitexact") == 3 and "-map_chapters" in args
    # Other files go as they are; a failed ffmpeg is the hand-off's error and leaves nothing behind.
    assert buzz_share.canonical(_touch(tmp_path / "captions.srt")) == tmp_path / "captions.srt"

    def failing_run(args, **kw):
        Path(args[-1]).write_bytes(b"half")
        return type("Done", (), {"returncode": 1, "stdout": "", "stderr": "moov atom not found"})()

    monkeypatch.setattr(buzz_share.subprocess, "run", failing_run)
    with pytest.raises(buzz_share.BuzzError, match="moov atom"):
        buzz_share.canonical(_touch(tmp_path / "story.mp4"))
    assert not (tmp_path / "story.buzz.mp4").exists() and not (tmp_path / "story.buzz.mp4.part").exists()


def test_a_published_short_whose_hand_off_failed_stays_listed_until_handed_off(home, tmp_path, monkeypatch):
    for rid, stamp in (("en-pub-1", None), ("en-done-1", "2026-10-15T12:00:00Z")):
        d = tmp_path / rid
        _touch(d / "out" / "master.mp4")
        ledger.add(_entry(rid, dir=str(d)))
        ledger.update(rid, state="published", published={"youtube": {"id": "y", "url": "https://youtube.com/shorts/y"}},
                      qa={"passed": True, "errors": [], "warnings": []}, **({"x_handoff_at": stamp} if stamp else {}))
    monkeypatch.setattr(st, "publish_mode", lambda: "live")
    out = json.loads(st.handle_shorts_studio({"action": "status"}))
    assert [(x["request_id"], x["state"], x.get("handoff_pending")) for x in out["shorts"]] == [("en-pub-1", "published", True)]
    assert out["shorts"][0]["files"]["master"].endswith("master.mp4")
    # Shadow mode never lists it (nothing is handed off there), and a handed-off one is done.
    monkeypatch.setattr(st, "publish_mode", lambda: "shadow")
    assert json.loads(st.handle_shorts_studio({"action": "status"}))["shorts"] == []

