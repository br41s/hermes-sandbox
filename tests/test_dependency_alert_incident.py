"""Regression lock for the dependency-advisory signal (incidents/sweep.py).

Hermetic: injects synthetic code-scanning alert payloads, so no network and no
GitHub token is touched.

The behaviours under test are the ones that decide whether this signal stays
useful or gets muted:
  * the standing backlog is adopted as a baseline and NEVER delivered,
  * an unchanged queue stays silent forever,
  * a package that gains a new advisory reports exactly once,
  * a large batch collapses into one brief instead of dozens,
  * medium/low never reach Telegram at all.
"""
import json

from datetime import datetime, timedelta, timezone

import pytest

from incidents.sweep import (DEP_ROLLUP_THRESHOLD, DependencyAlertBlind,
                             _blind_incident, _fetch_dependency_alerts,
                             dependency_alert_incidents, sweep)


def _alert(number, cve, severity, pkg, version, lockfile="uv.lock"):
    """A code-scanning alert shaped like osv-scanner's real SARIF upload.

    The lockfile and package only exist inside the rendered markdown of
    `rule.help` — there is no structured field for either — so the fixture
    reproduces that table rather than inventing a cleaner shape the parser
    would never see in production.
    """
    return {
        "number": number,
        "state": "open",
        "rule": {
            "id": cve,
            "security_severity_level": severity,
            "description": f"{cve}: {pkg} issue",
            "help": (
                f"**Your dependency is vulnerable to [{cve}]**\n\n"
                "### Affected Packages\n\n"
                "| Source | Package Name | Package Version |\n"
                "| --- | --- | --- |\n"
                f"| lockfile:/github/workspace/{lockfile} | {pkg} | {version} |\n\n"
                "## Remediation\n"
            ),
        },
        "tool": {"name": "osv-scanner"},
    }


def _sweep(incidents, state_path):
    """Run a sweep with every other signal silenced."""
    return sweep(jobs=[], langfuse=[], blocked=[], checkout_drift=[],
                 judge_liveness=[], dependency_alerts=incidents,
                 state_path=state_path)


def test_parses_package_and_lockfile_out_of_the_help_markdown():
    inc = dependency_alert_incidents(alerts=[
        _alert(1, "CVE-1", "high", "pillow", "12.2.0", "uv.lock")])
    assert len(inc) == 1
    assert "pillow" in inc[0].title
    assert "uv.lock" in inc[0].detail
    assert "reachability: runtime (production image)" in inc[0].detail


def test_groups_many_advisories_of_one_package_into_one_incident():
    """Pillow alone produced 13 of the 242 open alerts. One pin, one brief."""
    alerts = [_alert(n, f"CVE-{n}", "high", "pillow", "12.2.0") for n in range(1, 14)]
    inc = dependency_alert_incidents(alerts=alerts)
    assert len(inc) == 1
    assert "advisories (13)" in inc[0].detail


def test_medium_and_low_never_produce_an_incident():
    alerts = [_alert(1, "CVE-1", "medium", "joi", "18.2.3"),
              _alert(2, "CVE-2", "low", "dompurify", "3.4.2"),
              _alert(3, "CVE-3", "warning", "svgo", "3.3.3")]
    assert dependency_alert_incidents(alerts=alerts) == []


def test_missing_token_degrades_to_silence_not_an_exception(monkeypatch):
    """Same contract as the Langfuse signal: never take the sweep down."""
    for var in ("HERMES_DEP_ALERT_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    assert dependency_alert_incidents() == []


def test_existing_backlog_is_baselined_and_never_delivered(tmp_path):
    """The 242-alert backlog is not news. First run adopts it, silently."""
    state = tmp_path / "state.json"
    backlog = dependency_alert_incidents(alerts=[
        _alert(n, f"CVE-{n}", "high", f"pkg{n}", "1.0") for n in range(1, 31)])

    out = _sweep(backlog, state)

    assert "dependency advisory" not in out
    assert json.loads(state.read_text())["dep_alerts_baselined"] is True
    # Persisted despite there being no incident output — otherwise the next
    # sweep re-derives all 30 as new.
    assert len(json.loads(state.read_text())["seen"]) == 30


def test_unchanged_queue_stays_silent(tmp_path):
    state = tmp_path / "state.json"
    backlog = dependency_alert_incidents(alerts=[
        _alert(1, "CVE-1", "high", "pillow", "12.2.0")])
    _sweep(backlog, state)
    assert _sweep(backlog, state) == ""


def test_new_advisory_on_a_known_package_reports_once(tmp_path):
    state = tmp_path / "state.json"
    before = dependency_alert_incidents(alerts=[
        _alert(1, "CVE-1", "high", "pillow", "12.2.0")])
    _sweep(before, state)

    after = dependency_alert_incidents(alerts=[
        _alert(1, "CVE-1", "high", "pillow", "12.2.0"),
        _alert(2, "CVE-2", "critical", "pillow", "12.2.0")])
    first = _sweep(after, state)
    assert "pillow" in first
    assert "CRITICAL" in first
    # ...and does not nag about it on the next run.
    assert _sweep(after, state) == ""


def test_large_new_batch_collapses_into_one_rollup(tmp_path):
    """A lockfile-wide shift is one decision, not N Telegram messages."""
    state = tmp_path / "state.json"
    _sweep(dependency_alert_incidents(alerts=[
        _alert(0, "CVE-0", "high", "seed", "1.0")]), state)

    batch = dependency_alert_incidents(alerts=[
        _alert(0, "CVE-0", "high", "seed", "1.0"),
        *[_alert(n, f"CVE-{n}", "high", f"pkg{n}", "1.0")
          for n in range(1, DEP_ROLLUP_THRESHOLD + 5)],
    ])
    out = _sweep(batch, state)

    assert out.count("🔴 *Incident*") == 1
    assert "packages with new critical/high dependency advisories" in out
    # The rollup retires the ids it speaks for, so the batch does not re-roll.
    assert _sweep(batch, state) == ""


def test_build_only_lockfile_is_labelled_as_such(tmp_path):
    """The docs site never enters the production image; the brief says so."""
    inc = dependency_alert_incidents(alerts=[
        _alert(1, "CVE-1", "critical", "websocket-driver", "0.7.4",
               "website/package-lock.json")])
    assert "build-only" in inc[0].detail


# --- fail-closed: a refused API must not look like an empty queue -------------
# Production ran this signal blind: the fine-grained PAT had no
# `Code scanning alerts: Read`, so every fetch 403'd, the detector swallowed it
# and reported "nothing new" every hour while 37 high alerts sat open. A
# transient blip still deserves silence; a refusal never fixes itself and has
# to speak up. Same shape as the auditor judge that ran for twelve weeks
# without executing — an error detector cannot catch an absence.


class _FakeHTTPError(Exception):
    """Stands in for urllib.error.HTTPError (code + readable body)."""

    def __init__(self, code, message):
        self.code = code
        self._body = json.dumps({"message": message}).encode()
        super().__init__(f"HTTP {code}")

    def read(self):
        return self._body


def _patch_urlopen(monkeypatch, exc):
    import urllib.error
    import urllib.request
    # _FakeHTTPError must be caught by the `except urllib.error.HTTPError`
    # branch, so make it one for the duration of the test.
    monkeypatch.setattr(urllib.error, "HTTPError", _FakeHTTPError, raising=False)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(exc))


@pytest.mark.parametrize("status", [401, 403, 404])
def test_refused_api_raises_blind_rather_than_returning_empty(monkeypatch, status):
    _patch_urlopen(monkeypatch, _FakeHTTPError(status, "nope"))
    with pytest.raises(DependencyAlertBlind) as caught:
        _fetch_dependency_alerts("br41s/hermes-sandbox", "tok")
    assert caught.value.status == status


def test_transient_failure_still_degrades_quietly(monkeypatch):
    """A 500 or a dropped connection resolves itself — do not page for it."""
    _patch_urlopen(monkeypatch, _FakeHTTPError(500, "server error"))
    assert _fetch_dependency_alerts("br41s/hermes-sandbox", "tok") is None

    _patch_urlopen(monkeypatch, TimeoutError("connection timed out"))
    assert _fetch_dependency_alerts("br41s/hermes-sandbox", "tok") is None


def test_403_surfaces_as_an_incident_naming_the_permission(monkeypatch):
    """The real production failure, verbatim."""
    _patch_urlopen(monkeypatch, _FakeHTTPError(
        403, "Resource not accessible by personal access token"))
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    inc = dependency_alert_incidents()
    assert len(inc) == 1
    assert "BLIND" in inc[0].title
    assert "Code scanning alerts: Read" in inc[0].detail
    # It must say the silence is not reassurance.
    assert "reports nothing new" in inc[0].detail


def test_404_gets_its_own_explanation(monkeypatch):
    """404 is scanning switched off, not a missing permission."""
    _patch_urlopen(monkeypatch, _FakeHTTPError(404, "Not Found"))
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    inc = dependency_alert_incidents()
    assert "code scanning may be disabled" in inc[0].detail
    assert "Code scanning alerts: Read" not in inc[0].detail


def test_blind_repeats_daily_not_once_ever():
    """One alert months ago is not a working signal; nagging hourly is noise."""
    blind = DependencyAlertBlind(403, "nope")
    t0 = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)
    same_day = _blind_incident(blind, "r", now=t0 + timedelta(hours=6)).id
    next_day = _blind_incident(blind, "r", now=t0 + timedelta(days=1)).id
    assert _blind_incident(blind, "r", now=t0).id == same_day
    assert next_day != same_day


def test_blind_incident_breaks_the_all_clean_heartbeat(tmp_path, monkeypatch):
    """The heartbeat must not say 'no new incidents' while the signal is blind."""
    _patch_urlopen(monkeypatch, _FakeHTTPError(403, "Resource not accessible"))
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    state = tmp_path / "state.json"
    out = sweep(jobs=[], langfuse=[], blocked=[], checkout_drift=[],
                judge_liveness=[], state_path=state)
    assert "BLIND" in out
    assert "no new incidents" not in out
