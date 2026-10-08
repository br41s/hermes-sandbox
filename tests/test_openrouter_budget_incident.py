"""Regression lock for the OpenRouter key-budget signal.

Content jobs share one capped key (it 402'd the auditor for a day on 2026-09-01) and
a rental's key can sit near its cap with nobody watching. These tests pin the check:
low and exhausted keys are one brief per cap window, a refused key alerts, and
neither a key nor its OpenRouter label ever reaches a brief.

Hermetic: key records and .env files are injected; OpenRouter is never called.
"""
from datetime import datetime, timedelta, timezone

import pytest

import incidents.sweep as sw
from incidents.sweep import OPENROUTER_LOW_FRACTION, openrouter_budget_incidents

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)


def _key(limit, remaining, reset="weekly"):
    return {"limit": limit, "limit_remaining": remaining, "limit_reset": reset,
            "usage_daily": 0.4, "usage_weekly": 3.1, "label": "sk-or-v1-abc...xyz"}


def test_uncapped_and_healthy_keys_are_silent():
    records = [(["main"], _key(None, None, None)),
               (["grow-shop"], _key(10, 10 * OPENROUTER_LOW_FRACTION + 0.01))]
    assert openrouter_budget_incidents(records=records, now=NOW) == []


def test_a_low_key_is_one_brief_per_window_and_exhausted_is_its_own():
    low = openrouter_budget_incidents(records=[(["bl-shoroban"], _key(5, 0.31, None))], now=NOW)
    assert len(low) == 1 and low[0].title == "OpenRouter key held by bl-shoroban is low"
    assert "$0.31 left of a $5.00 cap (never resets)" in low[0].detail
    again = openrouter_budget_incidents(records=[(["bl-shoroban"], _key(5, 0.10, None))],
                                        now=NOW + timedelta(days=9))
    assert again[0].id == low[0].id, "a cap that never resets is one brief until it changes"

    out = openrouter_budget_incidents(records=[(["main"], _key(40, 0))], now=NOW)
    assert "exhausted" in out[0].id
    next_week = openrouter_budget_incidents(records=[(["main"], _key(40, 0))],
                                            now=NOW + timedelta(days=7))
    assert next_week[0].id != out[0].id, "a weekly cap gets one brief per week"


def test_a_refused_key_alerts_and_no_key_or_label_reaches_the_brief(monkeypatch):
    secret = "sk-or-v1-0000secret0000"
    monkeypatch.setattr(sw, "_openrouter_keys",
                        lambda: {secret: ["main", "grow-shop", "hermes-seo", "auditor"]})

    def refused(key):
        raise sw.DependencyAlertBlind(401, "OpenRouter refused the key")

    monkeypatch.setattr(sw, "_fetch_openrouter_key", refused)
    out = openrouter_budget_incidents(in_deployment=True, now=NOW)
    assert len(out) == 1 and "refuses" in out[0].title and "and 1 more" in out[0].title
    for inc in out:
        assert secret not in f"{inc.id} {inc.title} {inc.detail} {inc.handoff}"

    monkeypatch.setattr(sw, "_fetch_openrouter_key", lambda key: _key(5, 0.5))
    brief = openrouter_budget_incidents(in_deployment=True, now=NOW)[0]
    assert "abc...xyz" not in brief.detail, "the key's label is a masked key: keep it out"


def test_keys_come_from_main_and_real_profiles_deduplicated(tmp_path):
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=shared\n"
                                   "HERMES_AUDITOR_OPENROUTER_API_KEY=audit\n")
    for name, body in [("grow-shop", "OPENROUTER_API_KEY=shared\n"),
                       ("bl-shoroban", "OPENROUTER_API_KEY=own\n"),
                       ("not-a-profile", "OPENROUTER_API_KEY=ignored\n")]:
        prof = tmp_path / "profiles" / name
        prof.mkdir(parents=True)
        (prof / ".env").write_text(body)
        if name != "not-a-profile":
            (prof / "SOUL.md").write_text("x")
    keys = sw._openrouter_keys(home=tmp_path)
    assert keys == {"shared": ["main", "grow-shop"], "audit": ["main (auditor key)"],
                    "own": ["bl-shoroban"]}


def test_outside_the_deployment_openrouter_is_never_asked(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("network touched outside the deployment")

    monkeypatch.setattr(sw, "_openrouter_keys", _boom)
    assert openrouter_budget_incidents(in_deployment=False, now=NOW) == []


class _Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body.encode()


@pytest.mark.parametrize("body,expected", [
    ('{"data": "x"}', None),
    ('[1, 2]', None),
    ('{"data": 5}', None),
    ('{"error": "nope"}', None),
    ('{"data": {"limit": 7, "limit_remaining": 4}}', {"limit": 7, "limit_remaining": 4}),
])
def test_only_a_dict_answer_is_a_key_record(monkeypatch, body, expected):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: _Resp(body))
    assert sw._fetch_openrouter_key("k") == expected


def test_odd_usage_figures_do_not_crash_the_brief():
    record = dict(_key(5, 0.5), usage_daily="lots", usage_weekly=None)
    out = openrouter_budget_incidents(records=[(["bl-shoroban"], record)], now=NOW)
    assert "used today $0.00, this week $0.00" in out[0].detail
