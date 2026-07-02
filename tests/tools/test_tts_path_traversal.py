"""Regression: text_to_speech_tool output_path must reject '..' traversal.

The TTS surface accepts agent/user-supplied absolute paths (writing to a
chosen file is the whole point). What it must reject is paths that use
``..`` components to escape their declared base — those are almost
always either a bug or prompt-injection-controlled
(e.g. ``output_path="audio/../../etc/cron.d/x"``).
"""

import json

import pytest

from tools.tts_tool import text_to_speech_tool


@pytest.fixture
def no_tts_providers(monkeypatch):
    """Make provider generation fail locally instead of calling live edge-TTS.

    The pass-guard tests only assert the traversal rejection didn't fire;
    without this, the default edge provider performs a real synthesis call
    to Microsoft's endpoint."""
    async def _no_edge(text, output_path, tts_config):
        raise ValueError("edge TTS disabled in unit tests")

    monkeypatch.setattr("tools.tts_tool._generate_edge_tts", _no_edge)


def test_output_path_rejects_traversal_escape():
    """A path with '..' components must be rejected before any provider work."""
    result = json.loads(text_to_speech_tool(
        text="hello",
        output_path="audio/../../etc/cron.d/malicious",
    ))
    assert result["success"] is False
    assert "traversal" in result["error"].lower()


def test_output_path_rejects_bare_dotdot():
    """Bare '..' prefix must be rejected."""
    result = json.loads(text_to_speech_tool(
        text="hello",
        output_path="../escape.mp3",
    ))
    assert result["success"] is False
    assert "traversal" in result["error"].lower()


def test_output_path_absolute_path_passes_guard(tmp_path, monkeypatch, no_tts_providers):
    """Explicit absolute paths must pass the traversal guard.

    The agent legitimately writes audio to user-specified absolute paths;
    only ``..`` components are rejected. Any subsequent failure (no
    provider configured, etc.) is fine — the assertion is specifically
    that the 'traversal' rejection didn't fire.
    """
    inside = tmp_path / "clip.mp3"
    result = json.loads(text_to_speech_tool(
        text="hello",
        output_path=str(inside),
    ))
    error = result.get("error", "")
    assert "traversal" not in error.lower()


def test_output_path_relative_no_dotdot_passes_guard(tmp_path, monkeypatch, no_tts_providers):
    """Relative paths without '..' components must pass the guard."""
    monkeypatch.chdir(tmp_path)
    result = json.loads(text_to_speech_tool(
        text="hello",
        output_path="subdir/clip.mp3",
    ))
    error = result.get("error", "")
    assert "traversal" not in error.lower()
