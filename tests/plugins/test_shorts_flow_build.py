"""Tests for the Shorts Studio's ``flow`` format: the package contract, the
Fernet decryption of the owner's footage, the karaoke words built from what
faster-whisper hears, the editorial flags, the outro timing, the footage path
resolution, and the ``build`` dispatch.

No network, no ffmpeg, no Remotion: the pure helpers are proven here and the
render itself by the workflow's smoke render (the flow matrix entry).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from plugins.shorts.studio import build as build_mod
from plugins.shorts.studio import flow as flow_mod
from plugins.shorts.studio.build import BuildError

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "shorts" / "studio" / "samples" / "flow-sample.json"


@pytest.fixture
def sample():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

def test_flow_sample_is_valid(sample):
    pkg = flow_mod.validate(sample)
    assert pkg["format"] == "flow" and pkg["lang"] == "es"
    assert pkg["article_url"] == "https://biglobster.top/es/blog/seo-local-pymes-espana-2026.html"
    assert pkg["cover"] == {"title": "Sales en Google Maps o no existes", "kicker": "SEO local", "frame_s": 2.5}
    assert pkg["footage"] == {"path": "footage.mp4"}
    assert pkg["brand"] == "biglobster"
    assert pkg["expected_clips"] == 2
    assert pkg["overlays"] == [
        {"clip": 0, "position": "top", "delay_s": 0.0, "text": "¿Tu tienda vacía?"},
        {"clip": 0, "position": "bottom", "delay_s": 1.0, "text": "Atrae más clientes hoy"},
        {"clip": 1, "position": "top", "delay_s": 0.5, "text": "Perfil al 40 % = no sales"},
    ]


def test_validate_defaults_the_new_optional_fields(sample):
    sample.pop("overlays")
    sample.pop("expected_clips")
    pkg = flow_mod.validate(sample)
    assert pkg["overlays"] == [] and pkg["expected_clips"] is None


@pytest.mark.parametrize(
    "overlay, expected",
    [
        ({"clip": 0, "position": "middle", "delay_s": 0, "text": "x"}, "overlays[0].position"),
        ({"clip": 0, "position": "top", "delay_s": 0, "text": "x" * 41}, "overlays[0].text"),
        ({"clip": 0, "position": "top", "delay_s": 6, "text": "x"}, "overlays[0].delay_s"),
        ({"clip": -1, "position": "top", "delay_s": 0, "text": "x"}, "overlays[0].clip"),
    ],
)
def test_validate_rejects_a_bad_overlay(sample, overlay, expected):
    sample["overlays"] = [overlay]
    with pytest.raises(BuildError) as excinfo:
        flow_mod.validate(sample)
    assert expected in str(excinfo.value)


def test_validate_reports_every_bad_overlay_together(sample):
    sample["overlays"] = [
        {"clip": -1, "position": "middle", "delay_s": 6, "text": "x" * 41},
        *[{"clip": i, "position": "top", "delay_s": 0, "text": f"t{i}"} for i in range(12)],
    ]
    with pytest.raises(BuildError) as excinfo:
        flow_mod.validate(sample)
    message = str(excinfo.value)
    assert "overlays: max 12, got 13" in message
    assert "overlays[0].clip" in message and "overlays[0].position" in message
    assert "overlays[0].delay_s" in message and "overlays[0].text" in message


def test_validate_rejects_nine_expected_clips(sample):
    sample["expected_clips"] = 9
    with pytest.raises(BuildError) as excinfo:
        flow_mod.validate(sample)
    assert "expected_clips" in str(excinfo.value)


def test_validate_rejects_article_url_on_another_host(sample):
    sample["article_url"] = "https://evil.example/x"
    with pytest.raises(BuildError) as excinfo:
        flow_mod.validate(sample)
    assert "article_url" in str(excinfo.value)


def test_validate_rejects_article_url_over_http(sample):
    sample["article_url"] = "http://biglobster.top/x"
    with pytest.raises(BuildError) as excinfo:
        flow_mod.validate(sample)
    assert "article_url" in str(excinfo.value)


def test_validate_rejects_an_empty_cover_title(sample):
    sample["cover"]["title"] = "   "
    with pytest.raises(BuildError) as excinfo:
        flow_mod.validate(sample)
    assert "cover.title" in str(excinfo.value)


def test_validate_rejects_a_61_char_cover_title(sample):
    sample["cover"]["title"] = "x" * 61
    with pytest.raises(BuildError) as excinfo:
        flow_mod.validate(sample)
    assert "cover.title" in str(excinfo.value)


def test_validate_rejects_a_kicker_over_40_chars(sample):
    sample["cover"]["kicker"] = "k" * 41
    with pytest.raises(BuildError) as excinfo:
        flow_mod.validate(sample)
    assert "cover.kicker" in str(excinfo.value)


def test_validate_rejects_footage_with_both_path_and_url(sample):
    sample["footage"] = {"path": "footage.mp4", "url": "https://api.github.com/repos/x/y/releases/assets/1",
                         "encrypted": True}
    with pytest.raises(BuildError) as excinfo:
        flow_mod.validate(sample)
    assert "footage" in str(excinfo.value)


def test_validate_rejects_a_footage_url_off_api_github_com(sample):
    sample["footage"] = {"url": "https://github.com/x/y/releases/download/v1/f.enc", "encrypted": True}
    with pytest.raises(BuildError) as excinfo:
        flow_mod.validate(sample)
    assert "footage.url" in str(excinfo.value)


def test_validate_rejects_a_footage_url_without_encrypted_true(sample):
    sample["footage"] = {"url": "https://api.github.com/repos/x/y/releases/assets/1"}
    with pytest.raises(BuildError) as excinfo:
        flow_mod.validate(sample)
    assert "footage.encrypted" in str(excinfo.value)


def test_validate_rejects_a_footage_path_with_dotdot(sample):
    sample["footage"] = {"path": "../secret.mp4"}
    with pytest.raises(BuildError) as excinfo:
        flow_mod.validate(sample)
    assert "footage.path" in str(excinfo.value)


def test_validate_rejects_an_absolute_footage_path(sample):
    sample["footage"] = {"path": "/tmp/footage.mp4"}
    with pytest.raises(BuildError) as excinfo:
        flow_mod.validate(sample)
    assert "footage.path" in str(excinfo.value)


def test_validate_rejects_an_unknown_lang(sample):
    sample["lang"] = "fr"
    with pytest.raises(BuildError) as excinfo:
        flow_mod.validate(sample)
    assert "lang" in str(excinfo.value)


def test_validate_reports_every_problem_together(sample):
    sample["lang"] = "fr"
    sample["article_url"] = "http://evil.example/x"
    sample["cover"]["title"] = ""
    sample["footage"] = {"path": "../x.mp4", "url": "https://api.github.com/x", "encrypted": True}
    with pytest.raises(BuildError) as excinfo:
        flow_mod.validate(sample)
    message = str(excinfo.value)
    assert "lang" in message and "article_url" in message
    assert "cover.title" in message and "footage" in message
    assert len(message.strip().splitlines()) >= 4


# ---------------------------------------------------------------------------
# decrypt
# ---------------------------------------------------------------------------

def test_decrypt_round_trip():
    key = Fernet.generate_key().decode()
    data = b"the owner's vertical footage"
    assert flow_mod.decrypt(Fernet(key).encrypt(data), key) == data


def test_decrypt_with_the_wrong_key_raises_build_error():
    blob = Fernet(Fernet.generate_key()).encrypt(b"footage")
    with pytest.raises(BuildError, match="FLOW_ASSET_KEY missing or wrong"):
        flow_mod.decrypt(blob, Fernet.generate_key().decode())


def test_decrypt_with_an_empty_key_raises_build_error():
    with pytest.raises(BuildError, match="FLOW_ASSET_KEY missing or wrong"):
        flow_mod.decrypt(b"blob", "")


# ---------------------------------------------------------------------------
# words_from_heard
# ---------------------------------------------------------------------------

def test_words_from_heard_cleans_text_keeps_raw_and_paginates():
    heard = [
        {"text": "¡Hola,", "start": 0.0, "end": 0.4, "probability": 0.91},
        {"text": "mundo!", "start": 0.4, "end": 0.9, "probability": 0.83},
        {"text": "...", "start": 0.9, "end": 1.0, "probability": 0.99},
        {"text": "sí", "start": 1.234, "end": 1.679, "probability": 0.72},
        {"text": "cuatro", "start": 1.8, "end": 2.1, "probability": 0.66},
        {"text": "cinco", "start": 2.2, "end": 2.5, "probability": 0.95},
    ]
    words = flow_mod.words_from_heard(heard)
    assert [w["text"] for w in words] == ["Hola", "mundo", "sí", "cuatro", "cinco"]
    assert [w["raw"] for w in words] == ["¡Hola,", "mundo!", "sí", "cuatro", "cinco"]
    assert words[2]["startMs"] == 1234 and words[2]["endMs"] == 1679
    assert words[2]["probability"] == 0.72
    assert all(w["beat"] == 0 for w in words)
    assert [w["page"] for w in words] == [0, 0, 0, 1, 1]  # three words a page


# ---------------------------------------------------------------------------
# flags
# ---------------------------------------------------------------------------

def _word(text, start_s, probability=0.9):
    return {"text": text, "raw": text, "startMs": round(start_s * 1000),
            "endMs": round(start_s * 1000) + 300, "beat": 0, "page": 0,
            "probability": probability}


def test_flags_late_start():
    assert flow_mod.flags([_word("tarde", 0.8)], 10.0)["late_start"] is True
    assert flow_mod.flags([_word("pronto", 0.3)], 10.0)["late_start"] is False


def test_flags_lists_only_doubtful_words_with_rounded_fields():
    words = [_word("baja", 1.0, 0.4), _word("limite", 2.0, 0.59),
             _word("justo", 3.04, 0.6), _word("alta", 4.0, 0.9)]
    assert flow_mod.flags(words, 10.0)["doubtful"] == [
        {"word": "baja", "at_s": 1.0, "probability": 0.4},
        {"word": "limite", "at_s": 2.0, "probability": 0.59},
    ]


def test_flags_over_30s():
    assert flow_mod.flags([_word("hola", 0.2)], 31.0)["over_30s"] is True
    assert flow_mod.flags([_word("hola", 0.2)], 30.0)["over_30s"] is False


def test_flags_no_speech():
    out = flow_mod.flags([], 10.0)
    assert out["no_speech"] is True and out["first_word_s"] is None
    assert out["late_start"] is False and out["doubtful"] == []
    assert flow_mod.flags([_word("hola", 0.2)], 10.0)["no_speech"] is False


# ---------------------------------------------------------------------------
# timing
# ---------------------------------------------------------------------------

def test_timing_of_ten_seconds_of_footage():
    t = flow_mod.timing(10.0)
    assert t["footage_frames"] == 300
    assert t["outro_from"] == 285
    assert t["outro_frames"] == 150
    assert t["total_frames"] == 435
    assert t["total_s"] == 14.5


def test_footage_floor_keeps_the_outro_over_the_qa_minimum():
    assert flow_mod.FOOTAGE_MIN_S == 8.0
    assert flow_mod.timing(flow_mod.FOOTAGE_MIN_S)["total_s"] >= 12.0


# ---------------------------------------------------------------------------
# clip boundaries and overlays
# ---------------------------------------------------------------------------

SHOWINFO = """
[Parsed_showinfo_1 @ 0x7f8a] n:0 pts:121 pts_time:4.04 duration:1 pos:1 fmt:yuv420p
frame=   15 fps=0.0 q=-0.0 size=N/A time=00:00:04.04 bitrate=N/A speed=1.2x
[Parsed_showinfo_1 @ 0x7f8a] n:1 pts:126 pts_time:4.2 duration:1 pos:2 fmt:yuv420p
[Parsed_showinfo_1 @ 0x7f8a] n:2 pts:285 pts_time:9.5 duration:1 pos:3 fmt:yuv420p
[Parsed_showinfo_1 @ 0x7f8a] n:3 pts:N/A pts_time:N/A duration:N/A pos:4 fmt:yuv420p
"""


def test_parse_scene_cuts_drops_the_cut_half_a_second_apart():
    assert flow_mod.parse_scene_cuts(SHOWINFO) == [4.04, 9.5]


def test_parse_scene_cuts_without_cuts_is_empty():
    assert flow_mod.parse_scene_cuts("frame=  1 fps=0.0 q=0.0 size=N/A") == []


def test_segments_close_the_last_clip_at_the_footage_end():
    assert flow_mod.segments([4.0, 9.5], 15.0) == [(0, 4.0), (4.0, 9.5), (9.5, 15.0)]
    assert flow_mod.segments([], 12.0) == [(0, 12.0)]


def test_clip_segments_retries_until_the_expected_count(tmp_path, monkeypatch):
    calls = []
    cuts = {0.35: [1.0, 2.0], 0.25: [1.0, 2.0, 3.0]}

    def fake_detect(path, threshold):
        calls.append(threshold)
        return cuts[threshold]

    monkeypatch.setattr(flow_mod, "detect_cuts", fake_detect)
    segs, warning = flow_mod.clip_segments(tmp_path / "footage.mp4", 10.0, 4)
    assert calls == [0.35, 0.25]
    assert warning is None
    assert segs == [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 10.0)]


def test_clip_segments_without_expectation_keeps_the_default_threshold(tmp_path, monkeypatch):
    calls = []

    def fake_detect(path, threshold):
        calls.append(threshold)
        return [5.0]

    monkeypatch.setattr(flow_mod, "detect_cuts", fake_detect)
    segs, warning = flow_mod.clip_segments(tmp_path / "footage.mp4", 10.0, None)
    assert calls == [0.35]
    assert warning is None
    assert segs == [(0.0, 5.0), (5.0, 10.0)]


def test_clip_segments_warns_with_both_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(flow_mod, "detect_cuts", lambda path, threshold: [5.0])
    segs, warning = flow_mod.clip_segments(tmp_path / "footage.mp4", 10.0, 4)
    assert segs is None
    assert warning == "rótulos sin poner: el vídeo tiene 2 tomas y el pack 4 clips"


def test_overlay_frames_place_captions_over_their_clip_at_thirty_fps():
    segs = [(0.0, 4.0), (4.0, 9.5)]
    overlays = [
        {"clip": 0, "position": "top", "delay_s": 0.5, "text": "uno"},
        {"clip": 1, "position": "bottom", "delay_s": 0.0, "text": "dos"},
        {"clip": 5, "position": "top", "delay_s": 0.0, "text": "fuera"},
        {"clip": 0, "position": "bottom", "delay_s": 3.5, "text": "corta"},
    ]
    assert flow_mod.overlay_frames(overlays, segs, 30) == [
        {"text": "uno", "position": "top", "fromFrame": 15, "durationInFrames": 105},
        {"text": "dos", "position": "bottom", "fromFrame": 120, "durationInFrames": 165},
    ]


# ---------------------------------------------------------------------------
# footage_findings_to_warnings
# ---------------------------------------------------------------------------

def test_footage_findings_become_warnings_and_technical_errors_stay():
    report = {
        "passed": False,
        "errors": [
            "frozen picture for 3.0s at 1.0s",
            "black frames for 0.6s at 0.0s",
            "2.1s of silence at 4.0s",
            "loudness -20.0 LUFS, target -14±1.5",
        ],
        "warnings": [],
    }
    out = flow_mod.footage_findings_to_warnings(report)
    assert out["errors"] == ["loudness -20.0 LUFS, target -14±1.5"]
    assert out["warnings"] == [
        "footage: frozen picture for 3.0s at 1.0s",
        "footage: black frames for 0.6s at 0.0s",
        "footage: 2.1s of silence at 4.0s",
    ]
    assert out["passed"] is False


def test_only_footage_findings_pass_with_three_warnings():
    report = {
        "passed": False,
        "errors": [
            "frozen picture for 3.0s at 1.0s",
            "black frames for 0.6s at 0.0s",
            "2.1s of silence at 4.0s",
        ],
        "warnings": [],
    }
    out = flow_mod.footage_findings_to_warnings(report)
    assert out["errors"] == []
    assert out["passed"] is True
    assert [w for w in out["warnings"] if w.startswith("footage: ")] == [
        "footage: frozen picture for 3.0s at 1.0s",
        "footage: black frames for 0.6s at 0.0s",
        "footage: 2.1s of silence at 4.0s",
    ]
    assert len(out["warnings"]) == 3


# ---------------------------------------------------------------------------
# resolve_path
# ---------------------------------------------------------------------------

def test_resolve_path_inside_the_package_dir(tmp_path):
    package = tmp_path / "short" / "package.json"
    package.parent.mkdir()
    package.write_text("{}", encoding="utf-8")
    assert flow_mod.resolve_path(package, "footage.mp4") == (tmp_path / "short" / "footage.mp4").resolve()
    assert flow_mod.resolve_path(package, "clips/take.mp4") == (tmp_path / "short" / "clips" / "take.mp4").resolve()


def test_resolve_path_refuses_traversal(tmp_path):
    package = tmp_path / "short" / "package.json"
    package.parent.mkdir()
    package.write_text("{}", encoding="utf-8")
    with pytest.raises(BuildError):
        flow_mod.resolve_path(package, "../x.mp4")


# ---------------------------------------------------------------------------
# build dispatch
# ---------------------------------------------------------------------------

def test_build_dispatches_the_flow_format(tmp_path, monkeypatch):
    package = tmp_path / "package.json"
    package.write_text(json.dumps({"format": "flow", "lang": "es"}), encoding="utf-8")
    calls = {}

    def stub(package_path, raw, out, remotion_dir, *, offline=False):
        calls.update(package_path=package_path, raw=raw, out=out,
                     remotion_dir=remotion_dir, offline=offline)
        return 0

    monkeypatch.setattr(flow_mod, "build", stub)
    rc = build_mod.build(package, tmp_path / "out", tmp_path / "remotion")
    assert rc == 0
    assert calls["package_path"] == package
    assert calls["raw"] == {"format": "flow", "lang": "es"}
    assert calls["offline"] is False


def test_a_failed_scene_detection_skips_the_captions_not_the_render(monkeypatch):
    def broken(path, threshold):
        raise flow_mod.media.MediaError("scene cuts failed: boom")

    monkeypatch.setattr(flow_mod, "detect_cuts", broken)
    segs, warning = flow_mod.clip_segments(Path("x.mp4"), 20.0, 4)
    assert segs is None and warning.startswith("rótulos sin poner: no se pudieron detectar las tomas")
