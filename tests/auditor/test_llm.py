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
    # A single-backend model has no backend-local cache to keep warm, so there
    # is nothing to pin to. (Fixture was openrouter/owl-alpha until that model
    # was retired from OpenRouter — see CONTENT_MODEL_DEFAULT.)
    req = llm._build_request("openai/gpt-5.6-luna", [{"role": "user", "content": "hi"}],
                             "sk-test", session_id="hermes-auditor-content")
    assert "provider" not in _body(req)


def test_no_session_id_omits_field():
    # Backwards-compatible: absent session_id => no key in the body.
    req = llm._build_request("vendor/strong-1", [{"role": "user", "content": "hi"}], "sk-test")
    assert "session_id" not in _body(req)


# ── judge timing (report_judge_elapsed) ─────────────────────────────────────
# The gate failed on 2 of 4 PRs on 2026-09-18 and left only `exit 4` behind.
# This module is raw urllib with no Langfuse instrumentation and runs as a
# subprocess, so neither the trace nor agent.log held the duration — deciding
# whether the 300s bound was too tight meant re-running the judge by hand.
# These lock the timing line onto every exit path, and lock stdout clean:
# stdout is the VERDICT the orchestrator parses.

def _run_main(monkeypatch, review_impl):
    """Drive main() past arg parsing and the PR fetch, with review() stubbed."""
    monkeypatch.setattr(llm, "review", review_impl)
    monkeypatch.setattr(llm, "fetch_pr_content", lambda repo, number: ("diff", None))
    return llm.main(["--tier", "system", "--repo", "o/r", "--number", "1"])


def test_elapsed_logged_on_success_and_stdout_is_only_the_verdict(monkeypatch, capsys):
    rc = _run_main(monkeypatch, lambda tier, content: "VERDICT TEXT")
    captured = capsys.readouterr()
    assert rc == 0
    # The orchestrator parses stdout — timing must never land there.
    assert captured.out.strip() == "VERDICT TEXT"
    assert "judge call OK in" in captured.err
    assert "deadline" in captured.err


def test_elapsed_logged_on_timeout(monkeypatch, capsys):
    def _timeout(tier, content):
        raise TimeoutError("judge call exceeded its 300s deadline")

    rc = _run_main(monkeypatch, _timeout)
    captured = capsys.readouterr()
    assert rc == 4
    assert captured.out == ""
    assert "judge call TIMED OUT in" in captured.err


def test_elapsed_logged_on_judge_failure(monkeypatch, capsys):
    def _fail(tier, content):
        raise RuntimeError("HTTP 502 from model")

    rc = _run_main(monkeypatch, _fail)
    captured = capsys.readouterr()
    assert rc == 4
    assert captured.out == ""
    assert "judge call FAILED in" in captured.err


def test_elapsed_reports_percentage_of_the_deadline(monkeypatch, capsys):
    """A call landing near the bound is the only warning the next will exceed it."""
    monkeypatch.setenv("HERMES_AUDITOR_JUDGE_DEADLINE_SECONDS", "10")
    _run_main(monkeypatch, lambda tier, content: "ok")
    assert "deadline 10s" in capsys.readouterr().err


# ── generation bounds (the biglobster#550 gate outage) ──────────────────────
# The judge model is a REASONING model with a 384k-token completion budget. The
# request sent no max_tokens, no reasoning_effort and did not stream, so
# time-to-first-byte was time-to-last-byte and latency had no ceiling: the gate
# timed out on 2 of 4 PRs (2026-09-18) and then on biglobster#550 (2026-09-20),
# a 3-file, +195/-3 prose diff that still burned 100% of a 420s bound. Raising
# the bound 300 -> 420 did not fix it and could not: the tail was unbounded.
# These lock the three properties that bound it.

def test_completion_is_capped_by_default():
    body = _body(llm._build_request("vendor/strong-1", [{"role": "user", "content": "hi"}],
                                    "sk-test"))
    assert body["max_tokens"] == llm.JUDGE_MAX_TOKENS_DEFAULT


def test_reasoning_effort_is_low_by_default():
    body = _body(llm._build_request("vendor/strong-1", [{"role": "user", "content": "hi"}],
                                    "sk-test"))
    assert body["reasoning_effort"] == "low"


def test_request_streams():
    # Not for display — so each token is a socket read the per-read timeout can
    # actually see. Non-streamed, the whole generation was one silent wait.
    body = _body(llm._build_request("vendor/strong-1", [{"role": "user", "content": "hi"}],
                                    "sk-test"))
    assert body["stream"] is True


def test_max_tokens_env_override_and_floor(monkeypatch):
    monkeypatch.setenv("HERMES_AUDITOR_JUDGE_MAX_TOKENS", "1234")
    assert llm.judge_max_tokens() == 1234
    # A typo must not cap every verdict into truncation.
    monkeypatch.setenv("HERMES_AUDITOR_JUDGE_MAX_TOKENS", "3")
    assert llm.judge_max_tokens() == 256
    monkeypatch.setenv("HERMES_AUDITOR_JUDGE_MAX_TOKENS", "banana")
    assert llm.judge_max_tokens() == llm.JUDGE_MAX_TOKENS_DEFAULT
    monkeypatch.setenv("HERMES_AUDITOR_JUDGE_MAX_TOKENS", "  ")
    assert llm.judge_max_tokens() == llm.JUDGE_MAX_TOKENS_DEFAULT


def test_reasoning_effort_override_and_escape_hatch(monkeypatch):
    monkeypatch.setenv("HERMES_AUDITOR_JUDGE_REASONING_EFFORT", "high")
    assert llm.judge_reasoning_effort() == "high"
    # "off" (or anything unrecognised) omits the field for a model that would
    # reject it. _env_value strips, so "" cannot carry that meaning itself.
    monkeypatch.setenv("HERMES_AUDITOR_JUDGE_REASONING_EFFORT", "off")
    assert llm.judge_reasoning_effort() == ""
    body = _body(llm._build_request("vendor/strong-1", [{"role": "user", "content": "hi"}],
                                    "sk-test"))
    assert "reasoning_effort" not in body
    monkeypatch.setenv("HERMES_AUDITOR_JUDGE_REASONING_EFFORT", "   ")
    assert llm.judge_reasoning_effort() == llm.JUDGE_REASONING_EFFORT_DEFAULT


# ── SSE parsing ─────────────────────────────────────────────────────────────

class _FakeResp:
    """Minimal stand-in for the urlopen response: a context manager over lines."""

    def __init__(self, lines):
        self._lines = [l.encode("utf-8") for l in lines]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._lines)


def _sse(*events):
    return [f"data: {json.dumps(e)}\n" for e in events]


def _chunk(content=None, finish=None):
    delta = {"content": content} if content is not None else {}
    return {"choices": [{"delta": delta, "finish_reason": finish}]}


def test_stream_accumulates_content_and_finish_reason():
    resp = _FakeResp([
        ": OPENROUTER PROCESSING\n",          # keepalive padding — ignored
        "\n",
        *_sse(_chunk("APPR"), _chunk("OVE", finish="stop")),
        "data: [DONE]\n",
    ])
    text, finish = llm._read_stream(resp)
    assert text == "APPROVE"
    assert finish == "stop"


def test_stream_drops_reasoning_tokens():
    # We grade on the conclusion, not the thinking.
    resp = _FakeResp([
        *_sse({"choices": [{"delta": {"reasoning": "hmm..."}, "finish_reason": None}]}),
        *_sse(_chunk("BLOCK", finish="stop")),
    ])
    assert llm._read_stream(resp)[0] == "BLOCK"


def test_stream_error_after_200_raises():
    # A provider can fail mid-stream after the headers said 200. A partial
    # verdict must never reach the orchestrator as a whole one.
    resp = _FakeResp([
        *_sse(_chunk("APPR")),
        *_sse({"error": {"message": "upstream timed out"}}),
    ])
    try:
        llm._read_stream(resp)
    except RuntimeError as e:
        assert "upstream timed out" in str(e)
    else:
        raise AssertionError("expected RuntimeError on a mid-stream error")


# ── fail-closed on a verdict that is not whole ──────────────────────────────
# Both are newly reachable now the completion is capped. Returning either would
# hand the orchestrator text it parses as a review — and a review with no BLOCK
# in it reads as approval.

def _review_over(monkeypatch, lines):
    monkeypatch.setattr(llm, "_env_value", lambda name: "sk-test")
    monkeypatch.setattr(llm.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp(lines))
    monkeypatch.setattr(llm, "record_judge_success", lambda: None)
    return llm.review("system", "diff")


def test_truncated_verdict_fails_closed(monkeypatch):
    lines = _sse(_chunk("APPROVE, but I was cut off mid-", finish="length"))
    try:
        _review_over(monkeypatch, lines)
    except RuntimeError as e:
        assert "PARTIAL" in str(e)
        assert "HERMES_AUDITOR_JUDGE_MAX_TOKENS" in str(e)
    else:
        raise AssertionError("a length-truncated verdict must not be returned")


def test_empty_verdict_fails_closed(monkeypatch):
    try:
        _review_over(monkeypatch, _sse(_chunk("", finish="stop")))
    except RuntimeError as e:
        assert "empty verdict" in str(e)
    else:
        raise AssertionError("an empty verdict must not be returned")


def test_whole_verdict_is_returned(monkeypatch):
    out = _review_over(monkeypatch, _sse(_chunk("APPROVE — sound change.", finish="stop")))
    assert out == "APPROVE — sound change."


def test_stream_cut_mid_verdict_fails_closed():
    # No finish_reason, no [DONE] — a dropped connection. The text left behind
    # looks like an ordinary short review, which is exactly why this must raise
    # rather than return: a review with no BLOCK in it reads as approval.
    resp = _FakeResp(_sse(_chunk("APPROVE, the change is so")))
    try:
        llm._read_stream(resp)
    except RuntimeError as e:
        assert "PARTIAL" in str(e)
    else:
        raise AssertionError("a cut stream must not return its partial text")


def test_finish_reason_alone_is_a_valid_terminator():
    # A provider may end cleanly without emitting [DONE]; that is not a cut.
    text, finish = llm._read_stream(_FakeResp(_sse(_chunk("APPROVE", finish="stop"))))
    assert (text, finish) == ("APPROVE", "stop")


def test_done_alone_is_a_valid_terminator():
    # ...and the mirror case: [DONE] with no finish_reason on any chunk.
    resp = _FakeResp([*_sse(_chunk("APPROVE")), "data: [DONE]\n"])
    text, finish = llm._read_stream(resp)
    assert (text, finish) == ("APPROVE", None)
