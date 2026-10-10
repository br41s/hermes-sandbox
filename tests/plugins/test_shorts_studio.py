"""Tests for the Shorts Studio: package contract, grounding, ledger, the
``shorts_studio`` tool, the render-side helpers and the publishers.

Network (GitHub, YouTube, Meta, article fetches) is mocked throughout. The
ffmpeg-backed tests run only where ffmpeg is installed; the Remotion render
itself is proven by the workflow's smoke render, not here.
"""

from __future__ import annotations

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


def test_submit_dispatches_and_records(sample, submit_env):
    out = json.loads(st.handle_shorts_studio({"action": "submit", "package": sample}))
    assert out["success"], out
    rid, sent = submit_env[0]
    assert rid.startswith("en-when-to-adopt-ai-sme-2026-") and sent["request_id"] == rid
    entry = ledger.get(rid)
    assert entry["state"] == "rendering" and entry["run_id"] == 7
    again = json.loads(st.handle_shorts_studio({"action": "submit", "package": sample}))
    assert not again["success"] and "already has a short" in again["error"]


def test_submit_rejects_invented_figures_and_a_repeated_look(sample, submit_env):
    ledger.add(_entry("es-x-1", palette="indigo-coral", motif="grid"))
    sample["beats"][3]["value"] = "91%"
    out = json.loads(st.handle_shorts_studio({"action": "submit", "package": sample}))
    assert not out["success"]
    joined = " ".join(out["problems"])
    assert "style.palette" in joined and "'91'" in joined
    assert submit_env == []


def test_submit_requires_avatar_clips_from_the_library(sample, submit_env):
    sample["beats"].insert(1, {"kind": "avatar", "avatar": "martin", "clip_url": "https://x/clip.mp4",
                               "vo": "Hi, I am Martin."})
    out = json.loads(st.handle_shorts_studio({"action": "submit", "package": sample}))
    assert not out["success"] and "not in the avatar library" in " ".join(out["problems"])
    ledger.add_avatar({"avatar": "martin", "lang": "en", "line": "Hi, I am Martin.",
                       "clip_url": "https://x/clip.mp4", "seconds": 2.0})
    out = json.loads(st.handle_shorts_studio({"action": "submit", "package": sample}))
    assert out["success"], out


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


def test_status_collects_a_finished_render(sample, submit_env, monkeypatch):
    st.handle_shorts_studio({"action": "submit", "package": sample})
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


def test_publish_is_refused_in_shadow_mode_and_handoff_delivers_files(sample, submit_env, monkeypatch):
    monkeypatch.delenv("SHORTS_PUBLISH_MODE", raising=False)
    st.handle_shorts_studio({"action": "submit", "package": sample})
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


def test_publish_refuses_a_short_that_failed_qa(sample, submit_env, monkeypatch):
    monkeypatch.setenv("SHORTS_PUBLISH_MODE", "live")
    st.handle_shorts_studio({"action": "submit", "package": sample})
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


def test_shadow_handoff_does_not_relabel_a_published_short(sample, submit_env, monkeypatch):
    monkeypatch.delenv("SHORTS_PUBLISH_MODE", raising=False)
    st.handle_shorts_studio({"action": "submit", "package": sample})
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


def test_submit_refuses_a_colliding_request_id_before_dispatching(sample, submit_env, monkeypatch):
    monkeypatch.setattr(st, "_request_id", lambda pkg: "en-fixed-id-20260925000000")
    first = json.loads(st.handle_shorts_studio({"action": "submit", "package": sample}))
    assert first["success"], first
    other = copy.deepcopy(sample)
    other["article"]["url"] = "https://biglobster.top/blog/another-post"
    other["beats"][-1]["url"] = other["article"]["url"]
    other["style"] = {"palette": "violet-lime", "motif": "grid"}
    second = json.loads(st.handle_shorts_studio({"action": "submit", "package": other}))
    assert not second["success"] and "submitted this second" in second["error"]
    assert len(submit_env) == 1  # no second render started


def test_publish_story_without_a_story_cut_is_a_clear_error(sample, submit_env, monkeypatch):
    monkeypatch.setenv("SHORTS_PUBLISH_MODE", "live")
    for k, v in {"META_PAGE_ID": "1", "META_PAGE_ACCESS_TOKEN": "t", "META_IG_USER_ID": "2"}.items():
        monkeypatch.setenv(k, v)
    st.handle_shorts_studio({"action": "submit", "package": sample})
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

    jobs, skipped = genvideo.plan(pkg, timeline, cfg, daily_used=0.0, fps=30)
    assert [j["beat"] for j in jobs] == [0, 1]           # 8 s x $0.03 = $0.24 each; the third breaks $0.50
    assert skipped[0]["beat"] == 2 and "short budget" in skipped[0]["reason"]

    jobs, skipped = genvideo.plan(pkg, timeline, cfg, daily_used=0.80, fps=30)
    assert jobs == [] and all("daily cap" in s["reason"] for s in skipped)

    jobs, _ = genvideo.plan(pkg, timeline, cfg, daily_used=None, fps=30)
    assert len(jobs) == 2                                # unknown spend: the per-short budget still holds
    assert "pink" not in jobs[0]["prompt"] and "No text" in jobs[0]["prompt"]


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


def test_stock_footage_never_overwrites_a_generated_scene(sample, tmp_path):
    pkg = pkg_mod.validate_package(sample)
    timeline = [{"from": 0, "duration": 90} for _ in pkg["beats"]]
    timeline[0]["broll"] = "ai/s00.mp4"
    build_mod.gather_broll(pkg, timeline, tmp_path, tmp_path / "public", "seed", offline=True)
    assert timeline[0]["broll"] == "ai/s00.mp4" and timeline[1]["broll"] is None


def test_a_short_with_generated_scenes_is_published_as_synthetic_media(sample, submit_env, monkeypatch):
    st.handle_shorts_studio({"action": "submit", "package": sample})
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


def test_a_live_short_is_handed_to_socialbot_once(sample, submit_env, buzz_env, monkeypatch):
    from plugins.shorts import buzz_share

    rid = _ready_short(sample, submit_env, state="published", synthetic=True,
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


def test_a_failed_buzz_post_is_retried_next_run(sample, submit_env, buzz_env, monkeypatch):
    from plugins.shorts import buzz_share

    rid = _ready_short(sample, submit_env, state="published")
    monkeypatch.setenv("SHORTS_PUBLISH_MODE", "live")
    monkeypatch.setattr(buzz_share, "cli", lambda: "/usr/local/bin/buzz")
    monkeypatch.setattr(buzz_share.subprocess, "run", lambda args, **kw: type(
        "Done", (), {"returncode": 1, "stdout": "", "stderr": "relay said no"})())
    out = json.loads(st.handle_shorts_studio({"action": "handoff", "request_id": rid}))
    assert not out["success"] and "relay said no" in out["error"]
    assert not ledger.get(rid).get("x_handoff_at")


def test_shadow_mode_never_posts_to_buzz(sample, submit_env, buzz_env, monkeypatch):
    from plugins.shorts import buzz_share

    rid = _ready_short(sample, submit_env, state="ready")
    monkeypatch.delenv("SHORTS_PUBLISH_MODE", raising=False)

    def boom(*a, **kw):
        raise AssertionError("shadow mode must not post to Buzz")

    monkeypatch.setattr(buzz_share, "post", boom)
    out = json.loads(st.handle_shorts_studio({"action": "handoff", "request_id": rid}))
    assert out["success"] and "[SHADOW]" in out["message"]
