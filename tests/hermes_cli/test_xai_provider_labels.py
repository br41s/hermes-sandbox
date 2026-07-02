"""Regression tests for xAI provider label disambiguation."""

import time

import pytest

from hermes_cli.models import provider_label
from hermes_cli.providers import get_label


@pytest.fixture(autouse=True)
def _pinned_models_dev_cache(monkeypatch):
    """get_label("xai") resolves the display name via the models.dev
    catalog; pin the one entry needed so the test never fetches the live
    registry (blocked by the network guard, and non-hermetic anyway)."""
    import agent.models_dev as md
    monkeypatch.setattr(md, "_models_dev_cache", {
        "xai": {"id": "xai", "name": "xAI", "models": {}},
    })
    monkeypatch.setattr(md, "_models_dev_cache_time", time.time())


def test_xai_oauth_provider_label_is_not_collapsed_to_api_key_label():
    """The model picker must distinguish xAI API-key and OAuth providers."""
    assert get_label("xai") == "xAI"
    assert get_label("xai-oauth") == "xAI Grok OAuth (SuperGrok / Premium+)"
    assert get_label("grok-oauth") == "xAI Grok OAuth (SuperGrok / Premium+)"


def test_xai_oauth_provider_labels_match_canonical_model_labels():
    """Provider helpers should agree on the OAuth display label."""
    assert get_label("xai-oauth") == provider_label("xai-oauth")
