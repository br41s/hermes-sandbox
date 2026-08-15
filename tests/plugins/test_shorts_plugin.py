"""Tests for the `shorts` plugin (vertical short-form video assembly).

Network and ffmpeg are mocked throughout: these assert on the argv the
renderer builds and on the timing arithmetic, not on encoded pixels. The
real render is verified by hand against ffprobe — see AGENT_RENTAL_SETUP.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugins.shorts import (
    _validate_output_path,
    check_shorts_available,
    handle_shorts_render,
)
from plugins.shorts import captions as captions_mod
from plugins.shorts import render as render_mod
from plugins.shorts import stock as stock_mod


# ---------------------------------------------------------------------------
# Output path bounding
# ---------------------------------------------------------------------------

def test_relative_output_path_resolves_into_the_profile_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hermes_constants.get_hermes_home", lambda: tmp_path, raising=False
    )
    resolved = _validate_output_path("shorts/my-post/01-mistakes.mp4")
    assert resolved == tmp_path / "workspace" / "shorts" / "my-post" / "01-mistakes.mp4"


@pytest.mark.parametrize(
    "bad, expected",
    [
        ("shorts/../../etc/cron.d/x.mp4", "'..' component"),
        ("/etc/passwd.mp4", "must stay inside"),
        ("shorts/notes.txt", "must end in .mp4"),
        ("", "required"),
    ],
)
def test_output_path_rejects_escapes_and_wrong_types(bad, expected, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hermes_constants.get_hermes_home", lambda: tmp_path, raising=False
    )
    with pytest.raises(ValueError) as excinfo:
        _validate_output_path(bad)
    assert expected in str(excinfo.value)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def test_unknown_action_is_a_json_error_not_an_exception():
    payload = json.loads(handle_shorts_render({"action": "publish"}))
    assert payload["success"] is False
    assert "publish" in payload["error"]


def test_stock_search_without_a_key_explains_the_fallback(monkeypatch):
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    payload = json.loads(handle_shorts_render({"action": "stock_search", "query": "plants"}))
    assert payload["success"] is False
    assert "PEXELS_API_KEY" in payload["error"]
    # The agent needs to know it can still render without stock footage.
    assert "clip_url" in payload["error"]


def test_check_available_gates_on_ffmpeg(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert check_shorts_available() is False
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    assert check_shorts_available() is True


# ---------------------------------------------------------------------------
# Pexels response parsing
# ---------------------------------------------------------------------------

def _pexels_payload():
    return {
        "videos": [
            {
                "id": 1,
                "duration": 12,
                "image": "https://example.com/thumb1.jpg",
                "video_files": [
                    # Landscape rendition — must be skipped at 9:16.
                    {"file_type": "video/mp4", "width": 1920, "height": 1080,
                     "link": "https://example.com/wide.mp4"},
                    # Undersized portrait, and the one we want.
                    {"file_type": "video/mp4", "width": 720, "height": 1280,
                     "link": "https://example.com/small.mp4"},
                    {"file_type": "video/mp4", "width": 1080, "height": 1920,
                     "link": "https://example.com/good.mp4"},
                    # Oversized — skipped so we don't pull a 4K file.
                    {"file_type": "video/mp4", "width": 2880, "height": 5120,
                     "link": "https://example.com/huge.mp4"},
                ],
            },
            # Too short for the requested minimum.
            {"id": 2, "duration": 1, "video_files": [
                {"file_type": "video/mp4", "width": 1080, "height": 1920,
                 "link": "https://example.com/brief.mp4"}]},
        ]
    }


def test_search_clips_picks_the_smallest_rendition_at_or_above_target(monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return _pexels_payload()

    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return _Response()

    monkeypatch.setattr("httpx.get", fake_get)

    clips = stock_mod.search_clips("watering plants", per_page=5, min_duration=3)

    assert captured["params"]["orientation"] == "portrait"
    assert captured["headers"]["Authorization"] == "test-key"
    # The short clip is filtered out; only one usable video remains.
    assert len(clips) == 1
    assert clips[0]["url"] == "https://example.com/good.mp4"
    assert clips[0]["height"] > clips[0]["width"]


def test_search_clips_reports_no_results_with_actionable_advice(monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")

    class _Empty:
        def raise_for_status(self):
            return None

        def json(self):
            return {"videos": []}

    monkeypatch.setattr("httpx.get", lambda *a, **k: _Empty())
    with pytest.raises(stock_mod.StockError) as excinfo:
        stock_mod.search_clips("abstract concept")
    assert "concrete visual noun" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Captions
# ---------------------------------------------------------------------------

def test_format_time_uses_ass_centisecond_form():
    assert captions_mod.format_time(0) == "0:00:00.00"
    assert captions_mod.format_time(3.456) == "0:00:03.46"
    assert captions_mod.format_time(65.5) == "0:01:05.50"
    assert captions_mod.format_time(-1) == "0:00:00.00"


def test_long_caption_splits_into_two_line_cues():
    cues = captions_mod.split_into_cues(
        "Wet leaves after dark are exactly how fungus gets into your beds"
    )
    assert len(cues) > 1
    for cue in cues:
        assert cue.count("\\N") <= captions_mod.MAX_LINES_PER_CUE - 1


def test_cue_durations_partition_the_scene_exactly():
    ass = captions_mod.build_ass([(0.0, 6.0, "one two three four five six seven eight nine ten")])
    lines = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert len(lines) >= 2
    # The last cue must end exactly at the scene boundary — a drift here
    # would accumulate across scenes and desync every caption after it.
    assert lines[-1].split(",")[2] == "0:00:06.00"
    # And cues must be contiguous: each starts where the previous ended.
    for previous, current in zip(lines, lines[1:]):
        assert previous.split(",")[2] == current.split(",")[1]


def test_captions_are_lifted_clear_of_the_platform_ui():
    ass = captions_mod.build_ass([(0.0, 2.0, "hello")])
    style = next(line for line in ass.splitlines() if line.startswith("Style:"))
    margin_v = int(style.split(",")[-2])
    # Bottom 20% of a 1920-tall frame is platform chrome.
    assert margin_v > captions_mod.PLAY_RES_Y * 0.20


def test_caption_text_cannot_inject_ass_override_blocks():
    ass = captions_mod.build_ass([(0.0, 2.0, "{\\an8}drop \\N shadow")])
    dialogue = next(line for line in ass.splitlines() if line.startswith("Dialogue:"))
    text = dialogue.split(",,", 1)[1]
    assert "{" not in text and "}" not in text
    assert "\\an8" not in text
    # The only backslash sequences left are the \N line breaks we inserted.
    assert all(
        segment.startswith("N") or segment == ""
        for segment in text.split("\\")[1:]
    )


# ---------------------------------------------------------------------------
# Render pipeline
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_render(monkeypatch):
    """Neutralise ffmpeg/TTS and capture every command the renderer builds."""
    commands: list[list[str]] = []

    monkeypatch.setattr(render_mod, "require_ffmpeg", lambda: ("ffmpeg", "ffprobe"))

    def fake_synthesize(text, output_path):
        path = Path(str(output_path))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")
        return str(path)

    monkeypatch.setattr(render_mod, "synthesize", fake_synthesize)
    monkeypatch.setattr(render_mod, "probe_duration", lambda path: 3.0)

    def fake_run(cmd, *, cwd, what):
        commands.append(cmd)
        # The final encode is the only step that writes outside the workdir.
        if what == "final encode":
            Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(cmd[-1]).write_bytes(b"mp4")

    monkeypatch.setattr(render_mod, "_run", fake_run)
    return commands


def test_render_produces_canvas_correct_output(stub_render, tmp_path):
    out = tmp_path / "01.mp4"
    result = render_mod.render_short(
        {"scenes": [{"vo": "one"}, {"vo": "two"}]}, out
    )

    assert result["width"] == 1080 and result["height"] == 1920
    assert result["fps"] == 30
    assert result["scene_count"] == 2
    # Two scenes of 3.0s speech plus the tail pad on each.
    assert result["duration_seconds"] == pytest.approx(
        2 * (3.0 + render_mod.SCENE_TAIL_PAD), abs=0.01
    )
    assert result["has_music"] is False
    assert out.exists()


def test_scene_without_a_clip_falls_back_to_a_solid_background(stub_render, tmp_path):
    render_mod.render_short({"scenes": [{"vo": "one"}]}, tmp_path / "a.mp4")
    scene_cmd = next(c for c in stub_render if "scene_00.mp4" in c)
    assert "lavfi" in scene_cmd
    assert render_mod.FALLBACK_COLOR in " ".join(scene_cmd)


def test_scene_with_a_clip_crops_to_fill_and_loops_short_sources(stub_render, tmp_path):
    source = tmp_path / "broll.mp4"
    source.write_bytes(b"clip")
    render_mod.render_short(
        {"scenes": [{"vo": "one", "clip_path": str(source)}]}, tmp_path / "a.mp4"
    )
    scene_cmd = next(c for c in stub_render if "scene_00.mp4" in c)
    joined = " ".join(scene_cmd)
    # Loop so a 2s clip can fill a 3.35s scene...
    assert "-stream_loop" in scene_cmd
    # ...and crop rather than letterbox, so the frame is always filled.
    assert "force_original_aspect_ratio=increase" in joined
    assert "crop=1080:1920" in joined


def test_final_encode_burns_captions_and_normalises_loudness(stub_render, tmp_path):
    render_mod.render_short({"scenes": [{"vo": "one"}]}, tmp_path / "a.mp4")
    final = stub_render[-1]
    joined = " ".join(final)
    assert "ass=captions.ass" in joined
    assert "loudnorm=I=-14" in joined
    assert "+faststart" in final


def test_music_is_ducked_under_the_voiceover(stub_render, tmp_path):
    music = tmp_path / "bed.wav"
    music.write_bytes(b"music")
    result = render_mod.render_short(
        {"scenes": [{"vo": "one"}], "music_path": str(music)}, tmp_path / "a.mp4"
    )
    assert result["has_music"] is True
    mix = next(c for c in stub_render if "mixed.wav" in c)
    assert "sidechaincompress" in " ".join(mix)


def test_render_refuses_to_exceed_the_duration_cap(stub_render, tmp_path, monkeypatch):
    # Four 20s lines is 80s of speech — the cap must bite on scene 3, before
    # any of it is encoded, rather than silently shipping a truncated video.
    monkeypatch.setattr(render_mod, "probe_duration", lambda path: 20.0)
    with pytest.raises(render_mod.RenderError) as excinfo:
        render_mod.render_short(
            {"scenes": [{"vo": "a"}, {"vo": "b"}, {"vo": "c"}, {"vo": "d"}]},
            tmp_path / "a.mp4",
        )
    assert "past the 60s limit" in str(excinfo.value)
    assert "at scene 3" in str(excinfo.value)
    assert not (tmp_path / "a.mp4").exists()


def test_render_refuses_too_many_scenes(stub_render, tmp_path):
    scenes = [{"vo": f"line {n}"} for n in range(render_mod.MAX_SCENES + 1)]
    with pytest.raises(render_mod.RenderError) as excinfo:
        render_mod.render_short({"scenes": scenes}, tmp_path / "a.mp4")
    assert f"{render_mod.MAX_SCENES}-scene limit" in str(excinfo.value)
    # Rejected before any synthesis or encoding happens.
    assert stub_render == []


def test_render_rejects_an_empty_storyboard(stub_render, tmp_path):
    with pytest.raises(render_mod.RenderError):
        render_mod.render_short({"scenes": []}, tmp_path / "a.mp4")


def test_render_rejects_a_scene_with_nothing_to_say(stub_render, tmp_path):
    with pytest.raises(render_mod.RenderError) as excinfo:
        render_mod.render_short({"scenes": [{"caption": "silent"}]}, tmp_path / "a.mp4")
    assert "no 'vo'" in str(excinfo.value)


def test_unsafe_clip_urls_are_refused(stub_render, tmp_path, monkeypatch):
    monkeypatch.setattr("tools.url_safety.is_safe_url", lambda url: False)
    with pytest.raises(render_mod.RenderError) as excinfo:
        render_mod.render_short(
            {"scenes": [{"vo": "one", "clip_url": "http://169.254.169.254/meta.mp4"}]},
            tmp_path / "a.mp4",
        )
    assert "unsafe" in str(excinfo.value)


def test_temp_workdir_is_cleaned_up_even_when_rendering_fails(stub_render, tmp_path, monkeypatch):
    created: list[str] = []
    real_mkdtemp = render_mod.tempfile.mkdtemp

    def tracking_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    monkeypatch.setattr(render_mod.tempfile, "mkdtemp", tracking_mkdtemp)
    monkeypatch.setattr(
        render_mod, "synthesize", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    with pytest.raises(RuntimeError):
        render_mod.render_short({"scenes": [{"vo": "one"}]}, tmp_path / "a.mp4")

    assert created and not Path(created[0]).exists()
