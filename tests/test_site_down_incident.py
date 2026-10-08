"""Regression lock for the products-down signal.

FinView's API was down for a day on 2026-09-29 (a billing budget disabled the GCP
project) and no signal said so: Ops Sentinel is retired and the watcher only looked
at Hermes. These tests pin the probe: two failures 20s apart are a brief, a blip is
not, a site that stays down is one brief a day, and FinView's API is probed every
3 hours to spare Cloud Run's free tier.

Hermetic: probes and the clock are injected; the network is never touched.
"""
from datetime import datetime, timedelta, timezone

import pytest

import incidents.sweep as sw
from incidents.sweep import probe_verdict, site_down_incidents

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
SITES = (("Alpha", "https://alpha.example/health", "Zeabur alpha"),
         ("Beta", "https://beta.example/", "Vercel beta"))


@pytest.fixture
def down_when_500(monkeypatch):
    """Site logic is tested apart from the down/up policy: a 500 or no answer is down."""
    monkeypatch.setattr(sw, "probe_verdict",
                        lambda status, seconds, error: None if status == 200 else f"HTTP {status}")


def _scripted(answers):
    """A probe that answers per URL from a list, one answer per call."""
    calls = {url: list(seq) for url, seq in answers.items()}

    def probe(url):
        return calls[url].pop(0), 0.1, ""
    return probe


def test_two_failures_in_a_row_are_one_brief_and_a_blip_is_none(down_when_500):
    sleeps = []
    probe = _scripted({SITES[0][1]: [500, 500], SITES[1][1]: [500, 200]})
    out = site_down_incidents(probes=SITES, probe=probe, now=NOW, sleep=sleeps.append)
    assert [i.title for i in out] == ["Alpha is down"]
    assert out[0].kind == "site_down"
    assert "Zeabur alpha" in out[0].handoff and SITES[0][1] in out[0].detail
    assert len(sleeps) == 2, "each failing site gets exactly one second look"


def test_a_site_that_stays_down_is_one_brief_a_day(down_when_500):
    def always_down(url):
        return 500, 0.1, ""

    first = site_down_incidents(probes=SITES[:1], probe=always_down, now=NOW, sleep=lambda s: None)
    later = site_down_incidents(probes=SITES[:1], probe=always_down,
                                now=NOW + timedelta(hours=5), sleep=lambda s: None)
    tomorrow = site_down_incidents(probes=SITES[:1], probe=always_down,
                                   now=NOW + timedelta(days=1), sleep=lambda s: None)
    assert first[0].id == later[0].id != tomorrow[0].id


def test_healthy_sites_never_wait(down_when_500):
    def up(url):
        return 200, 0.1, ""

    def no_sleep(seconds):
        raise AssertionError("slept with every site up")

    assert site_down_incidents(probes=SITES, probe=up, now=NOW, sleep=no_sleep) == []


def test_outside_the_deployment_no_site_is_probed(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("network touched outside the deployment")

    monkeypatch.setattr(sw, "_probe_site", _boom)
    assert site_down_incidents(in_deployment=False, now=NOW) == []


def test_every_probe_is_https_and_named_once():
    names = [name for name, _, _ in sw.SITE_PROBES]
    assert len(names) == len(set(names))
    assert all(url.startswith("https://") for _, url, _ in sw.SITE_PROBES)


@pytest.mark.parametrize("status,error,expected", [
    (200, "", None),
    (204, "", None),
    (404, "", "HTTP 404 after 0.3s"),
    (503, "", "HTTP 503 after 0.3s"),
    (None, "URLError: <urlopen error [Errno -2] Name does not resolve>", "no answer after 0s (URLError"),
])
def test_down_is_no_answer_or_any_4xx_5xx(status, error, expected):
    verdict = probe_verdict(status, 0.3, error)
    assert verdict is None if expected is None else verdict.startswith(expected)


def test_slow_but_answering_is_up():
    assert probe_verdict(200, 45.0, "") is None, "a Cloud Run cold start is not an outage"


def test_finview_api_is_probed_every_third_hour_only():
    probed = []

    def up(url):
        probed.append(url)
        return 200, 0.1, ""

    sites = (("FinView API", "https://api.example/api/health", "Cloud Run"),) + SITES
    site_down_incidents(probes=sites, probe=up, now=NOW.replace(hour=13), sleep=lambda s: None)
    assert "https://api.example/api/health" not in probed and len(probed) == 2
    probed.clear()
    site_down_incidents(probes=sites, probe=up, now=NOW.replace(hour=15), sleep=lambda s: None)
    assert len(probed) == 3


def test_the_three_stack_signals_reach_the_brief_and_are_deduped(tmp_path):
    def inc(kind, title):
        return sw.Incident(id=f"{kind}:x", kind=kind, title=title, detail="d", handoff="h")

    kwargs = dict(jobs=[], langfuse=[], judge_liveness=[],
                  site_down=[inc("site_down", "Alpha is down")],
                  volume_backup=[inc("volume_backup", "Host volume backup has not run")],
                  openrouter_budget=[inc("openrouter_budget", "OpenRouter key held by main is low")],
                  state_path=tmp_path / "s.json")
    first = sw.sweep(**kwargs)
    for title in ("Alpha is down", "Host volume backup has not run",
                  "OpenRouter key held by main is low"):
        assert title in first
    assert sw.sweep(**kwargs) == "", "each incident is one brief"
