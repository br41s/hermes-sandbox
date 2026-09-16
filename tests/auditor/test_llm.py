"""Regression lock for auditor model resolution (auditor/llm.py).

The load-bearing properties: env vars win (the CEO's fast-change knobs work),
and an unknown/missing tier falls to the SYSTEM model, never the cheap one —
the real gate must not silently downgrade. The HTTP call itself is not exercised
here (no network); request construction is checked separately.
"""
import json

import auditor.llm as llm


def test_env_vars_win(monkeypatch):
    monkeypatch.setenv("HERMES_AUDITOR_SYSTEM_MODEL", "vendor/strong-1")
    monkeypatch.setenv("HERMES_AUDITOR_CONTENT_MODEL", "vendor/cheap-1")
    assert llm.resolve_model("system") == "vendor/strong-1"
    assert llm.resolve_model("content") == "vendor/cheap-1"


def test_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("HERMES_AUDITOR_SYSTEM_MODEL", raising=False)
    monkeypatch.delenv("HERMES_AUDITOR_CONTENT_MODEL", raising=False)
    assert llm.resolve_model("system") == llm.SYSTEM_MODEL_DEFAULT
    assert llm.resolve_model("content") == llm.CONTENT_MODEL_DEFAULT


def test_blank_env_falls_back(monkeypatch):
    monkeypatch.setenv("HERMES_AUDITOR_SYSTEM_MODEL", "   ")
    assert llm.resolve_model("system") == llm.SYSTEM_MODEL_DEFAULT


def test_unknown_tier_uses_system(monkeypatch):
    monkeypatch.setenv("HERMES_AUDITOR_SYSTEM_MODEL", "vendor/strong-1")
    # Anything that isn't "content" must resolve to the system model (fail-safe).
    assert llm.resolve_model("banana") == "vendor/strong-1"
    assert llm.resolve_model("") == "vendor/strong-1"


def test_missing_api_key_raises(monkeypatch):
    # Patch the resolver, not just os.environ: the key now also resolves from
    # $HERMES_HOME/.env, so a delenv alone would false-pass on any machine that
    # happens to have one.
    monkeypatch.setattr(llm, "_env_value", lambda _name: "")
    try:
        llm.review("system", "review this diff")
    except RuntimeError as e:
        assert "OPENROUTER_API_KEY" in str(e)
        assert "did NOT run" in str(e)
    else:
        raise AssertionError("expected RuntimeError when API key is absent")


def test_request_is_well_formed(monkeypatch):
    req = llm._build_request("vendor/strong-1", [{"role": "user", "content": "hi"}], "sk-test")
    assert req.full_url == llm._OPENROUTER_URL
    assert req.get_header("Authorization") == "Bearer sk-test"
    body = json.loads(req.data.decode("utf-8"))
    assert body["model"] == "vendor/strong-1"
    assert body["temperature"] == 0
    assert body["messages"][0]["role"] == "user"


def _body(req):
    return json.loads(req.data.decode("utf-8"))


def test_session_id_emitted_for_sticky_routing():
    # session_id is the OpenRouter sticky-routing key that keeps the cache warm.
    req = llm._build_request("deepseek/deepseek-v4-pro", [{"role": "user", "content": "hi"}],
                             "sk-test", session_id="hermes-auditor-system")
    assert _body(req)["session_id"] == "hermes-auditor-system"


def test_session_id_truncated_to_256():
    req = llm._build_request("deepseek/deepseek-v4-pro", [{"role": "user", "content": "hi"}],
                             "sk-test", session_id="x" * 500)
    assert len(_body(req)["session_id"]) == 256


def test_deepseek_is_provider_pinned_with_fallbacks_on():
    # DeepSeek cache is backend-local → pin to the deepseek upstream, but keep
    # fallbacks ON so an outage doesn't break the review gate.
    req = llm._build_request("deepseek/deepseek-v4-flash", [{"role": "user", "content": "hi"}],
                             "sk-test", session_id="hermes-auditor-system")
    prov = _body(req)["provider"]
    assert prov == {"order": ["deepseek"]}
    assert "allow_fallbacks" not in prov  # fallbacks stay default-on


def test_non_deepseek_is_not_pinned():
    # owl-alpha is a single OpenRouter-native backend — no pinning, no harm.
    req = llm._build_request("openrouter/owl-alpha", [{"role": "user", "content": "hi"}],
                             "sk-test", session_id="hermes-auditor-content")
    assert "provider" not in _body(req)


def test_no_session_id_omits_field():
    # Backwards-compatible: absent session_id => no key in the body.
    req = llm._build_request("vendor/strong-1", [{"role": "user", "content": "hi"}], "sk-test")
    assert "session_id" not in _body(req)
