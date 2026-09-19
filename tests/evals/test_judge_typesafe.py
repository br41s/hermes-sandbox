"""TypeSafe System One judge: verdict bands, degradation, and request shape."""
from __future__ import annotations

import io
import json
from contextlib import contextmanager
from unittest import mock

import pytest

from evals import judge as judge_mod
from evals.judge import NOUL_FAIL_BELOW, NOUL_PASS_ABOVE, judge

ASSERTION = {"text": "the reply names the fallback model", "check": {"must_contain": ["gpt"]}}


@contextmanager
def _api(noul=None, *, error=None, body=None):
    """Patch urlopen to serve one System One response (or raise)."""
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data.decode("utf-8"))
        if error is not None:
            raise error
        payload = body if body is not None else {
            "model": "jev-latest",
            "answers": {"satisfied": {"type": "noul", "noul": noul}},
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    with mock.patch.dict("os.environ", {"TYPESAFE_API_KEY": "sk-test"}), \
            mock.patch("urllib.request.urlopen", fake_urlopen):
        yield captured


def test_high_probability_passes():
    with _api(0.97):
        result = judge("...", ASSERTION, use_typesafe=True)
    assert result.passed is True
    assert result.mode == "typesafe"
    assert "0.97" in result.reason


def test_low_probability_fails():
    with _api(0.03):
        result = judge("...", ASSERTION, use_typesafe=True)
    assert result.passed is False
    assert result.mode == "typesafe"
    assert "not satisfied" in result.reason


@pytest.mark.parametrize("noul", [NOUL_FAIL_BELOW + 0.01, 0.5, NOUL_PASS_ABOVE - 0.01])
def test_undecided_band_fails_and_says_it_is_uncertain(noul):
    """The band is the point of using a Noul: it must not round to a green."""
    with _api(noul):
        result = judge("...", ASSERTION, use_typesafe=True)
    assert result.passed is False
    assert "uncertain" in result.reason
    # An ambiguous verdict must not read as "the behaviour is broken".
    assert "not the same as the behaviour being wrong" in result.reason


def test_thresholds_are_inclusive_at_the_edges():
    with _api(NOUL_PASS_ABOVE):
        assert judge("...", ASSERTION, use_typesafe=True).passed is True
    with _api(NOUL_FAIL_BELOW):
        result = judge("...", ASSERTION, use_typesafe=True)
    assert result.passed is False
    assert "not satisfied" in result.reason


def test_request_matches_the_documented_system_one_shape():
    with _api(0.9) as captured:
        judge("the output", ASSERTION, use_typesafe=True)
    assert captured["url"] == "https://api.typesafe.ai/v1/systemone"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    body = captured["body"]
    assert body["model"] == "jev-latest"
    assert body["state"] == {"agent_output": "the output", "assertion": ASSERTION["text"]}
    question = body["questions"]["satisfied"]
    assert question["type"] == "noul"
    assert set(question["criteria"]) == {"true", "false"}


def test_missing_key_degrades_to_deterministic_and_names_the_cause():
    with mock.patch.dict("os.environ", {}, clear=True):
        result = judge("contains gpt-4", ASSERTION, use_typesafe=True)
    assert result.mode == "typesafe->deterministic"
    assert "TYPESAFE_API_KEY is not set" in result.reason
    assert result.passed is True  # the deterministic check still ran


def test_transport_error_degrades_and_names_the_cause():
    with _api(error=TimeoutError("timed out")):
        result = judge("contains gpt-4", ASSERTION, use_typesafe=True)
    assert result.mode == "typesafe->deterministic"
    assert "TimeoutError" in result.reason


def test_malformed_response_degrades_rather_than_crashing():
    with _api(body={"answers": {}}):
        result = judge("contains gpt-4", ASSERTION, use_typesafe=True)
    assert result.mode == "typesafe->deterministic"
    assert "KeyError" in result.reason


def test_typesafe_is_not_used_unless_requested():
    with mock.patch.object(judge_mod, "_typesafe_judge") as called:
        result = judge("contains gpt-4", ASSERTION)
    called.assert_not_called()
    assert result.mode == "deterministic"
