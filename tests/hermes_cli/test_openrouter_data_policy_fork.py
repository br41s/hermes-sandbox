"""Fork: ``provider_routing.data_collection`` on the OpenRouter paths the main turn did not cover.

Auxiliary calls (vision, titles, summaries, compression) and the image_gen/video_gen plugins build
their own request bodies; each must carry the operator's policy, and only when the request really
goes to OpenRouter. Video must never carry ``zdr`` (OpenRouter offers no ZDR video endpoint).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli.fork_ext.openrouter_data_policy import (
    openrouter_provider_policy,
    with_openrouter_data_policy,
)

_RUNTIME = "hermes_cli.runtime_provider.resolve_runtime_provider"
_PNG_DATA_URI = "data:image/png;base64,dGVzdC1pbWFnZS1kYXRh"  # "test-image-data"


def _config(monkeypatch, value="deny"):
    """Patch the cached config reader the helper imports lazily."""
    cfg = {} if value is ... else {"provider_routing": {"data_collection": value}}
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: cfg)


def _runtime_ok(**over):
    base = {
        "provider": "openrouter",
        "api_mode": "chat_completions",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "sk-or-test",
        "source": "env",
    }
    base.update(over)
    return base


def _mock_chat_response(images):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "",
                                 "images": [{"type": "image_url", "image_url": {"url": u}} for u in images]}}]
    }
    return resp


def _mock_image_api_response():
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"created": 0, "data": [{"b64_json": "dGVzdA==", "media_type": "image/png"}]}
    return resp


def _openrouter_provider():
    from plugins.image_gen.openrouter import OpenRouterCompatImageProvider

    return OpenRouterCompatImageProvider(
        provider_name="openrouter",
        display_name="OpenRouter",
        runtime_name="openrouter",
        config_key="openrouter",
        model_env_var="OPENROUTER_IMAGE_MODEL",
        setup_schema={"name": "OpenRouter (image)", "badge": "paid", "env_vars": []},
    )


def _openrouter_image_api_provider():
    from plugins.image_gen.openrouter import _build_providers

    return {p.name: p for p in _build_providers()}["openrouter"]


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


class TestPolicy:
    def test_unset_config_returns_empty(self, monkeypatch):
        _config(monkeypatch, ...)
        assert openrouter_provider_policy() == {}

    def test_deny_is_returned(self, monkeypatch):
        _config(monkeypatch, "deny")
        assert openrouter_provider_policy() == {"data_collection": "deny"}

    @pytest.mark.parametrize("value", ["", "   ", None, 42, ["deny"], {"data_collection": "deny"}])
    def test_blank_non_string_values_return_empty(self, monkeypatch, value):
        _config(monkeypatch, value)
        assert openrouter_provider_policy() == {}

    def test_value_is_stripped_but_not_normalized(self, monkeypatch):
        _config(monkeypatch, "  allow  ")
        assert openrouter_provider_policy() == {"data_collection": "allow"}

    def test_config_read_failure_returns_empty(self, monkeypatch):
        def boom():
            raise RuntimeError("unreadable")

        monkeypatch.setattr("hermes_cli.config.load_config_readonly", boom)
        assert openrouter_provider_policy() == {}

    def test_body_is_not_mutated(self, monkeypatch):
        _config(monkeypatch, "deny")
        body = {"model": "m", "provider": {"only": ["x"]}}
        result = with_openrouter_data_policy(body)
        assert result == {"model": "m", "provider": {"only": ["x"], "data_collection": "deny"}}
        assert body == {"model": "m", "provider": {"only": ["x"]}}
        assert result is not body

    def test_empty_policy_returns_a_copy(self, monkeypatch):
        _config(monkeypatch, ...)
        body = {"model": "m"}
        result = with_openrouter_data_policy(body)
        assert result == body and result is not body
        assert with_openrouter_data_policy(None) == {}

    def test_config_wins_over_caller_data_collection(self, monkeypatch):
        _config(monkeypatch, "deny")
        result = with_openrouter_data_policy({"provider": {"only": ["x"], "data_collection": "allow"}})
        assert result["provider"] == {"only": ["x"], "data_collection": "deny"}

    def test_non_dict_provider_is_replaced(self, monkeypatch):
        _config(monkeypatch, "deny")
        assert with_openrouter_data_policy({"provider": "auto"})["provider"] == {"data_collection": "deny"}

    def test_other_provider_keys_survive(self, monkeypatch):
        _config(monkeypatch, "deny")
        provider = {"order": ["a", "b"], "sort": "price", "zdr": True, "ignore": ["c"]}
        assert with_openrouter_data_policy({"provider": provider})["provider"] == {
            **provider, "data_collection": "deny"}


# ---------------------------------------------------------------------------
# Auxiliary calls
# ---------------------------------------------------------------------------


def _aux_kwargs(provider, **kwargs):
    from agent.auxiliary_client import _build_call_kwargs

    return _build_call_kwargs(
        provider=provider, model="m", messages=[{"role": "user", "content": "hi"}], **kwargs)


class TestAuxiliary:
    def test_openrouter_by_name_carries_the_policy(self, monkeypatch):
        _config(monkeypatch, "deny")
        kwargs = _aux_kwargs("openrouter")
        assert kwargs["extra_body"]["provider"]["data_collection"] == "deny"

    def test_openrouter_by_base_url_carries_the_policy(self, monkeypatch):
        _config(monkeypatch, "deny")
        kwargs = _aux_kwargs("custom", base_url="https://openrouter.ai/api/v1")
        assert kwargs["extra_body"]["provider"]["data_collection"] == "deny"

    def test_other_providers_get_no_provider_key(self, monkeypatch):
        _config(monkeypatch, "deny")
        for provider, base_url in (("nous", None), ("anthropic", None),
                                   ("custom", "https://example.com/v1")):
            kwargs = _aux_kwargs(provider, base_url=base_url)
            assert "provider" not in kwargs.get("extra_body", {}), provider

    def test_config_without_policy_adds_no_extra_body(self, monkeypatch):
        _config(monkeypatch, ...)
        kwargs = _aux_kwargs("openrouter")
        assert "provider" not in kwargs.get("extra_body", {})

    def test_config_read_failure_still_builds_the_call(self, monkeypatch):
        def boom():
            raise RuntimeError("unreadable")

        monkeypatch.setattr("hermes_cli.config.load_config_readonly", boom)
        kwargs = _aux_kwargs("openrouter")
        assert "provider" not in kwargs.get("extra_body", {})


# ---------------------------------------------------------------------------
# image_gen plugin
# ---------------------------------------------------------------------------


class TestImagePlugin:
    def test_image_api_payload_carries_the_policy(self, monkeypatch):
        _config(monkeypatch, "deny")
        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=_mock_image_api_response()) as mock_post, \
             patch("plugins.image_gen.openrouter.save_b64_image", return_value=Path("/tmp/i.png")):
            result = _openrouter_image_api_provider().generate(
                prompt="a red square", model="openai/gpt-image-2")
        assert result["success"] is True, result
        assert mock_post.call_args[0][0] == "https://openrouter.ai/api/v1/images/generations"
        assert mock_post.call_args.kwargs["json"]["provider"] == {"data_collection": "deny"}

    def test_chat_image_payload_carries_the_policy(self, monkeypatch):
        _config(monkeypatch, "deny")
        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=_mock_chat_response([_PNG_DATA_URI])) as mock_post, \
             patch("plugins.image_gen.openrouter.save_b64_image", return_value=Path("/tmp/x.png")):
            result = _openrouter_provider().generate(prompt="a pet")
        assert result["success"] is True, result
        payload = mock_post.call_args.kwargs["json"]
        assert payload["provider"] == {"data_collection": "deny"}

    def test_non_openrouter_base_url_gets_no_provider_key(self, monkeypatch):
        _config(monkeypatch, "deny")
        elsewhere = _runtime_ok(base_url="https://example.com/v1")
        with patch(_RUNTIME, return_value=elsewhere), \
             patch("requests.post", return_value=_mock_chat_response([_PNG_DATA_URI])) as mock_post, \
             patch("plugins.image_gen.openrouter.save_b64_image", return_value=Path("/tmp/x.png")):
            result = _openrouter_provider().generate(prompt="a pet")
        assert result["success"] is True, result
        assert mock_post.call_args[0][0] == "https://example.com/v1/chat/completions"
        assert "provider" not in mock_post.call_args.kwargs["json"]

    def test_config_without_policy_sends_the_old_payload(self, monkeypatch):
        _config(monkeypatch, ...)
        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=_mock_chat_response([_PNG_DATA_URI])) as mock_post, \
             patch("plugins.image_gen.openrouter.save_b64_image", return_value=Path("/tmp/x.png")):
            _openrouter_provider().generate(prompt="a pet")
        assert "provider" not in mock_post.call_args.kwargs["json"]


# ---------------------------------------------------------------------------
# video_gen plugin
# ---------------------------------------------------------------------------

_VEO = {"id": "google/veo-3.1", "name": "Google: Veo 3.1", "supported_durations": [4, 6, 8],
        "supported_resolutions": ["720p", "1080p", "4K"], "supported_aspect_ratios": ["16:9", "9:16"],
        "supported_frame_images": ["first_frame", "last_frame"], "generate_audio": True, "seed": True,
        "pricing_skus": {"duration_seconds_with_audio": "0.40", "duration_seconds_without_audio": "0.20"}}


@dataclass
class _Response:
    payload: dict
    status_code: int = 200

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@dataclass
class _Session:
    posts: list = field(default_factory=list)
    gets: list = field(default_factory=list)
    polls: list = field(default_factory=lambda: [
        _Response({"id": "job-1", "status": "in_progress"}),
        _Response({"id": "job-1", "status": "completed", "usage": {"cost": 0.4}})])

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return _Response({"id": "job-1", "status": "pending"}, status_code=202)

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return self.polls.pop(0)

    def close(self):
        pass


def _video_provider(monkeypatch, base_url="https://openrouter.ai/api/v1"):
    from plugins.video_gen.openrouter import OpenRouterVideoGenProvider

    provider = OpenRouterVideoGenProvider()
    monkeypatch.setattr(provider, "_catalog", lambda: [_VEO])
    monkeypatch.setattr(provider, "_configured_model", lambda: "google/veo-3.1")
    monkeypatch.setattr(provider, "_credentials", lambda: ("sk-or-test", base_url))
    return provider


def _generate_video(monkeypatch, tmp_path, base_url):
    provider = _video_provider(monkeypatch, base_url)
    session = _Session()
    monkeypatch.setattr(provider, "_session", lambda: session)
    monkeypatch.setattr("plugins.video_gen.openrouter.time.sleep", lambda s: None)
    monkeypatch.setattr("plugins.video_gen.openrouter.save_url_video",
                        lambda url, **kwargs: tmp_path / "clip.mp4")
    result = provider.generate("a fox", duration=6, resolution="1080p", aspect_ratio="16:9")
    return result, session


class TestVideoPlugin:
    def test_video_payload_carries_the_policy_and_no_zdr(self, monkeypatch, tmp_path):
        _config(monkeypatch, "deny")
        result, session = _generate_video(monkeypatch, tmp_path, "https://openrouter.ai/api/v1")
        assert result["success"] is True, result
        assert session.posts[0][0] == "https://openrouter.ai/api/v1/videos"
        payload = session.posts[0][1]["json"]
        assert payload["provider"] == {"data_collection": "deny"}
        assert "zdr" not in payload

    def test_non_openrouter_base_url_gets_no_provider_key(self, monkeypatch, tmp_path):
        _config(monkeypatch, "deny")
        result, session = _generate_video(monkeypatch, tmp_path, "https://example.com/api/v1")
        assert result["success"] is True, result
        assert "provider" not in session.posts[0][1]["json"]

    def test_config_without_policy_sends_the_old_payload(self, monkeypatch, tmp_path):
        _config(monkeypatch, ...)
        result, session = _generate_video(monkeypatch, tmp_path, "https://openrouter.ai/api/v1")
        assert result["success"] is True, result
        assert "provider" not in session.posts[0][1]["json"]
