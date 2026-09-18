"""Tests for the HuggingFace image_gen plugin (seedream-4.5 via HF Inference API)."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest

import plugins.image_gen.huggingface as hf_plugin


@pytest.fixture(autouse=True)
def _tmp_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


# ── Metadata ────────────────────────────────────────────────────────────────


class TestMetadata:
    def test_name(self):
        provider = hf_plugin.HuggingFaceImageGenProvider()
        assert provider.name == "huggingface"

    def test_display_name(self):
        provider = hf_plugin.HuggingFaceImageGenProvider()
        assert provider.display_name == "HuggingFace"

    def test_default_model(self):
        """The default is pinned deliberately, so a silent change is visible.

        622b98095 switched it to seedream-4.5 and 44342ada3 revisited the
        provider, but this literal stayed on the old FLUX default. Asserting
        membership in _MODELS instead would have let both changes through
        unnoticed -- the point of this test is to be a change-detector, so the
        literal stays and gets updated with intent.
        """
        provider = hf_plugin.HuggingFaceImageGenProvider()
        assert provider.default_model() == "bytedance-seed/seedream-4.5"
        # ...and the default must actually be offered by the provider.
        assert provider.default_model() in {e["id"] for e in provider.list_models()}

    def test_list_models_has_required_fields(self):
        provider = hf_plugin.HuggingFaceImageGenProvider()
        for entry in provider.list_models():
            assert "id" in entry
            assert "display" in entry
            assert "price" in entry

    def test_setup_schema(self):
        provider = hf_plugin.HuggingFaceImageGenProvider()
        schema = provider.get_setup_schema()
        assert schema["name"] == "HuggingFace"
        assert schema["badge"] == "free"
        keys = [e["key"] for e in schema["env_vars"]]
        assert "HUGGINGFACE_API_KEY" in keys


# ── Availability ────────────────────────────────────────────────────────────


class TestAvailability:
    def test_no_key_unavailable(self, monkeypatch):
        monkeypatch.delenv("HUGGINGFACE_API_KEY", raising=False)
        assert hf_plugin.HuggingFaceImageGenProvider().is_available() is False

    def test_key_present_available(self, monkeypatch):
        monkeypatch.setenv("HUGGINGFACE_API_KEY", "hf_test")
        assert hf_plugin.HuggingFaceImageGenProvider().is_available() is True


# ── Model resolution ────────────────────────────────────────────────────────


class TestModelResolution:
    def test_default_model(self):
        model_id, meta = hf_plugin._resolve_model()
        assert model_id == hf_plugin.DEFAULT_MODEL
        assert "display" in meta

    def test_env_var_override(self, monkeypatch):
        monkeypatch.setenv(
            "HUGGINGFACE_IMAGE_MODEL",
            "stabilityai/stable-diffusion-xl-base-1.0",
        )
        model_id, _ = hf_plugin._resolve_model()
        assert model_id == "stabilityai/stable-diffusion-xl-base-1.0"

    def test_env_var_unknown_falls_back(self, monkeypatch):
        monkeypatch.setenv("HUGGINGFACE_IMAGE_MODEL", "unknown/model")
        model_id, _ = hf_plugin._resolve_model()
        assert model_id == hf_plugin.DEFAULT_MODEL


# ── Generate ────────────────────────────────────────────────────────────────


class TestGenerate:
    @pytest.fixture(autouse=True)
    def _api_key(self, monkeypatch):
        monkeypatch.setenv("HUGGINGFACE_API_KEY", "hf_test")

    def _fake_image_response(self, content: bytes, status: int = 200):
        mock = MagicMock()
        mock.status_code = status
        mock.content = content
        mock.text = ""
        return mock

    def test_empty_prompt_rejected(self):
        provider = hf_plugin.HuggingFaceImageGenProvider()
        result = provider.generate("")
        assert result["success"] is False
        assert result["error_type"] == "invalid_argument"

    def test_missing_api_key(self, monkeypatch):
        monkeypatch.delenv("HUGGINGFACE_API_KEY", raising=False)
        provider = hf_plugin.HuggingFaceImageGenProvider()
        result = provider.generate("a cat")
        assert result["success"] is False
        assert result["error_type"] == "auth_required"

    def test_successful_generation_saves_to_cache(self, tmp_path):
        fake_png = b"fake-png-data"

        with patch(
            "requests.post",
            return_value=self._fake_image_response(fake_png),
        ):
            with patch(
                "plugins.image_gen.huggingface.save_b64_image",
                return_value=tmp_path / "hf_img.png",
            ) as mock_save:
                provider = hf_plugin.HuggingFaceImageGenProvider()
                result = provider.generate("a dragon")

        assert result["success"] is True
        assert "hf_img.png" in result["image"]
        assert result["provider"] == "huggingface"
        assert result["model"] == hf_plugin.DEFAULT_MODEL
        mock_save.assert_called_once()

    def test_model_loading_503(self):
        mock_resp = self._fake_image_response(b"", status=503)
        mock_resp.text = "Service Unavailable"

        with patch("requests.post", return_value=mock_resp):
            provider = hf_plugin.HuggingFaceImageGenProvider()
            result = provider.generate("a cat")

        assert result["success"] is False
        assert result["error_type"] == "model_loading"
        assert "cold start" in result["error"]

    def test_api_error_surfaces_status(self):
        mock_resp = self._fake_image_response(b"bad auth", status=401)
        mock_resp.text = "Unauthorized"

        with patch("requests.post", return_value=mock_resp):
            provider = hf_plugin.HuggingFaceImageGenProvider()
            result = provider.generate("a cat")

        assert result["success"] is False
        assert result["error_type"] == "api_error"
        assert "401" in result["error"]

    def test_network_error(self):
        with patch(
            "requests.post",
            side_effect=ConnectionError("refused"),
        ):
            provider = hf_plugin.HuggingFaceImageGenProvider()
            result = provider.generate("a cat")

        assert result["success"] is False
        assert result["error_type"] == "network_error"

    def test_request_uses_hf_router_url(self):
        fake_png = b"fake-png"

        with patch(
            "requests.post",
            return_value=self._fake_image_response(fake_png),
        ) as mock_post:
            with patch("plugins.image_gen.huggingface.save_b64_image", return_value="/tmp/x.png"):
                provider = hf_plugin.HuggingFaceImageGenProvider()
                provider.generate("a robot")

        called_url = mock_post.call_args.args[0] if mock_post.call_args.args else mock_post.call_args[0][0]
        assert "router.huggingface.co" in called_url
        assert hf_plugin.DEFAULT_MODEL in called_url

    def test_auth_header_sent(self):
        fake_png = b"fake-png"

        with patch(
            "requests.post",
            return_value=self._fake_image_response(fake_png),
        ) as mock_post:
            with patch("plugins.image_gen.huggingface.save_b64_image", return_value="/tmp/x.png"):
                provider = hf_plugin.HuggingFaceImageGenProvider()
                provider.generate("a robot")

        headers = mock_post.call_args.kwargs.get("headers") or mock_post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer hf_test"

    @pytest.mark.parametrize("aspect", ["landscape", "square", "portrait"])
    def test_aspect_ratio_accepted(self, aspect, tmp_path):
        fake_png = b"fake-png"

        with patch(
            "requests.post",
            return_value=self._fake_image_response(fake_png),
        ):
            with patch("plugins.image_gen.huggingface.save_b64_image", return_value=tmp_path / "x.png"):
                provider = hf_plugin.HuggingFaceImageGenProvider()
                result = provider.generate("a cat", aspect_ratio=aspect)

        assert result["success"] is True
        assert result["aspect_ratio"] == aspect


# ── Registration ─────────────────────────────────────────────────────────────


class TestRegistration:
    def test_register(self):
        mock_ctx = MagicMock()
        hf_plugin.register(mock_ctx)
        mock_ctx.register_image_gen_provider.assert_called_once()
        provider = mock_ctx.register_image_gen_provider.call_args[0][0]
        assert isinstance(provider, hf_plugin.HuggingFaceImageGenProvider)
        assert provider.name == "huggingface"
