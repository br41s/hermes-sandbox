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
    ts_palettes = re.findall(r"^\s+'([a-z]+-[a-z]+)': \{", theme, re.M)
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
    assert ledger.done_urls() == {"https://b.top/en-a-1", "https://b.top/es-a-1"}
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


def test_a_post_that_keeps_failing_stops_being_retried(home):
    url = "https://b.top/post"
    ledger.add(_entry("en-post-1", article_url=url))
    ledger.update("en-post-1", state="qa_failed")
    assert url not in ledger.done_urls()  # one failure: retry tomorrow
    ledger.add(_entry("en-post-2", article_url=url))
    ledger.update("en-post-2", state="render_failed")
    assert url in ledger.done_urls()  # two: leave it for a human


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

    def fake_upload(video, thumb, srt, social, lang, *, synthetic=False):
        seen["synthetic"] = synthetic
        return {"id": "yt1", "url": "u", "privacy": "public", "warnings": []}

    monkeypatch.setenv("SHORTS_PUBLISH_MODE", "live")
    monkeypatch.setattr(st, "_enabled_targets", lambda: ["youtube"])
    from plugins.shorts.social import youtube as youtube_mod

    monkeypatch.setattr(youtube_mod, "upload", fake_upload)
    out = json.loads(st.handle_shorts_studio({"action": "publish", "request_id": rid, "target": "youtube"}))
    assert out["success"] and seen["synthetic"] is True


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
    assert [b["show"] for b in pkg["beats"]] == ["presenter", "card", "card", "card", "card"]
    words = sum(len(b["vo"].split()) for b in pkg["beats"])
    assert pkg_mod.TIP_MIN_TOTAL_WORDS <= words <= pkg_mod.TIP_MAX_TOTAL_WORDS
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
    (lambda p: p["beats"][2].update(vo=p["beats"][2]["vo"] + " " + "muy " * 20), "one tip"),
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


def test_the_tip_short_runs_lucia_then_martins_nod_then_his_cards(tip):
    pkg = pkg_mod.validate_package(tip)
    beats = build_mod.tip_beats(pkg)
    assert [b["kind"] for b in beats[:4]] == ["opener", "hook", "handoff", "reaction"]
    assert beats[0]["vo"] == pkg["opener"]["line"] and beats[2]["vo"] == pkg["opener"]["handoff"]
    assert beats[3]["silent"] and not beats[3]["vo"]
    assert beats[-2]["kind"] == "cta" and beats[-1]["kind"] == "outro"
    assert "biglobster" in beats[-1]["vo"] and "síguenos" in beats[-1]["vo"].lower()
    assert "!" not in beats[-1]["vo"] and "¡" not in beats[-1]["vo"]      # calm, not overexcited
    assert build_mod.on_camera(beats) == [0, 1, 2]                    # only Lucía's lines are paid
    # Her lines fall back to her free voice; Martín's take the package's (his).
    assert {beats[i]["voice"] for i in (0, 1, 2)} == {presenter_mod.HOST_FALLBACK_VOICES["es"]}
    assert all("voice" not in b for b in beats[3:])


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


def test_lucia_is_paid_only_for_her_lines_and_martin_costs_nothing(tip, tmp_path, monkeypatch):
    fake = _FakeImageAPI()
    _tip_env(monkeypatch, tmp_path, fake)
    pkg = dict(pkg_mod.validate_package(tip))
    pkg["beats"] = build_mod.tip_beats(pkg)
    cast = build_mod.tip_cast(pkg, tmp_path, genvideo.open_budget(offline=False), offline=False)
    # The setting: one image request, both photos as references, the article's scene in the prompt.
    assert len(fake.chat) == 1
    content = fake.chat[0]["messages"][0]["content"]
    assert "bakery" in content[0]["text"] and "physics" in content[0]["text"] and len(content) == 3
    # Avatar IV once per line of hers, in her HeyGen voice, on the setting; nothing for Martín.
    assert len(fake.posts) == 3
    assert [t["prompt"] for t in fake.posts] == [pkg["beats"][i]["vo"] for i in (0, 1, 2)]
    assert {t["provider"]["options"]["heygen"]["voice_id"] for t in fake.posts} == {presenter_mod.HOST_VOICES["es"]}
    assert fake.posts[0]["input_references"][0]["image_url"]["url"].startswith("data:image/png")
    assert fake.posts[2]["provider"]["options"]["heygen"]["motion_prompt"] == presenter_mod.HANDOFF_MOTION
    assert sorted(cast["takes"]) == [0, 1, 2]
    # His loop is the owner's recording, committed: never generated, only looked up.
    assert cast["loop"] == presenter_mod.PHOTO_DIR / "assets" / "martin-loop.mp4"
    assert cast["report"]["loop"]["status"] == "reused"
    report = {}
    assert presenter_mod.bubble_loop(report, "nobody") is None and report["loop"]["status"] == "missing"


def test_lucias_takes_are_all_or_nothing_under_the_budget(tip, tmp_path, monkeypatch):
    fake = _FakeImageAPI()
    _tip_env(monkeypatch, tmp_path, fake)
    monkeypatch.setenv("SHORTS_AI_SHORT_BUDGET_USD", "0.30")
    pkg = dict(pkg_mod.validate_package(tip))
    pkg["beats"] = build_mod.tip_beats(pkg)
    report = {}
    image = _touch(tmp_path / "setting.png")
    lines = {i: pkg["beats"][i]["vo"] for i in (0, 1, 2)}
    with fake() as client:
        takes = presenter_mod.host_takes(client, genvideo.open_budget(offline=False), image, lines, "es", 2,
                                         tmp_path, report)
    assert takes == {} and not fake.posts
    assert {t["status"] for t in report["takes"]} == {"skipped"} and "short budget" in report["takes"][0]["reason"]


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


def test_a_tip_short_without_lucia_is_not_published():
    report = {"takes": [{"beat": 0, "status": "skipped", "reason": "daily cap $2.00 reached"},
                        {"beat": 1, "status": "skipped", "reason": "daily cap $2.00 reached"}]}
    error = build_mod.presenter_missing_error(True, [0, 1], True, report)
    assert error and "Lucía's take missing" in error and error.count("daily cap") == 1
    assert build_mod.presenter_missing_error(True, [], True, report) is None        # every take was made
    assert build_mod.presenter_missing_error(True, [0], False, report) is None      # no key: smoke test
    assert build_mod.presenter_missing_error(False, [0], True, report) is None      # classic format
    assert "not made" in build_mod.presenter_missing_error(True, [0], True, {})


def test_tip_props_carry_her_takes_his_nod_his_loop_and_the_cards(tip):
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
    assert props["beats"][0]["presenter"] == "presenter/p00.mp4" and props["beats"][0]["presenterName"] == "Lucía"
    assert props["beats"][3]["kind"] == "reaction" and props["beats"][3]["show"] == "reaction"
    assert [b["number"] for b in props["beats"][:4]] == [0, 0, 0, 0]
    assert props["beats"][-1]["kind"] == "outro" and props["beats"][-1]["url"]
    assert props["cover"]["kicker"] == pkg["beats"][1].get("kicker", "")


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
    # Every line of Martín's (cards, CTA, outro) in his voice; none of Lucía's, nothing for the silent turn.
    assert sorted(voiced) == list(range(4, len(pkg["beats"])))
    assert {v for _, v in said} == {pkg["voice"]}


def test_edge_is_retried_for_over_a_minute(monkeypatch, tmp_path):
    from plugins.shorts.studio import voice as voice_mod

    calls, waits = [], []

    async def refuse(*a, **kw):
        calls.append(1)
        raise RuntimeError("403, message='Invalid response status'")

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

    monkeypatch.setenv("SHORTS_AZURE_SPEECH_KEY", "az-key")
    monkeypatch.setenv("SHORTS_AZURE_SPEECH_REGION", "westeurope")
    monkeypatch.setattr(media, "duration", lambda p: 2.0)
    monkeypatch.setattr(httpx, "post", lambda url, content, timeout, headers: sent.append((url, content, headers)) or Resp(200))
    words = voice_mod.synthesize("Elige una tarea & mide", "es-ES-AlvaroNeural", "+5%", tmp_path / "a.mp3")
    url, ssml, headers = sent[0]
    assert url == "https://westeurope.tts.speech.microsoft.com/cognitiveservices/v1"
    assert b"es-ES-AlvaroNeural" in ssml and b"&amp; mide" in ssml and b"rate='+5%'" in ssml
    voice_mod.synthesize("Hola", "es-ES-Tristan:DragonHDLatestNeural", "+5%", tmp_path / "hd.mp3")
    assert b"name='es-ES-Tristan:DragonHDLatestNeural'" in sent[1][1] and b"prosody" not in sent[1][1]
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
