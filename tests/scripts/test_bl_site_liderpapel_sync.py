"""Outcome tests for the Liderpapel sync cron script.

The script exists because a sync that silently never runs is indistinguishable
from one that runs fine — the client's shop keeps serving, just from a frozen
catalogue. So the behaviour worth pinning down is not "does it POST", it is
which situations it stays silent for and which it reports. Silence is the
success signal (Hermes delivers a no_agent job's stdout, and empty stdout is no
delivery), so a false silence is the expensive failure.
"""

import urllib.error

import pytest

from scripts import bl_site_liderpapel_sync as sync

SITE = "https://cliente.example"
BEFORE = "2026-08-19T13:27:41.000Z"
AFTER = "2026-08-20T06:00:12.000Z"

CONFIGURED = {
    "hasPassword": True,
    "supplierCode": "CSP",
    "lastSyncStatus": "ok",
    "lastSyncAt": BEFORE,
}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(sync.time, "sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def _credentials(monkeypatch):
    monkeypatch.setattr(sync, "_get_site_credentials", lambda: (SITE, "panel-pw"))
    monkeypatch.setattr(sync, "_get_jwt", lambda _url, _pw: "jwt-token")


def wire(monkeypatch, statuses, run=None):
    """Feed the script a scripted sequence of status responses.

    ``statuses`` is consumed one per GET; the last entry repeats. ``run`` is
    called for the POST and may raise to simulate a timeout or an HTTP error.
    """
    remaining = list(statuses)
    calls = {"run": 0}

    def _request(method, url, _token, _timeout):
        if method == "POST":
            calls["run"] += 1
            if run is not None:
                return run()
            return {"success": True}
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    monkeypatch.setattr(sync, "_request", _request)
    return calls


def test_silent_when_the_sync_completes_and_advances(monkeypatch, capsys):
    wire(monkeypatch, [CONFIGURED, {**CONFIGURED, "lastSyncStatus": "running"},
                       {**CONFIGURED, "lastSyncAt": AFTER}])

    assert sync.main() == 0
    assert capsys.readouterr().out == "", "a healthy sync must deliver nothing"


def test_reports_when_the_sync_reports_an_error(monkeypatch, capsys):
    wire(monkeypatch, [CONFIGURED, {
        **CONFIGURED,
        "lastSyncStatus": "error",
        "lastSyncMessage": "Credenciales sFTP de Liderpapel incompletas",
    }])

    assert sync.main() == 1
    assert "Credenciales sFTP" in capsys.readouterr().out


def test_reports_a_success_that_never_advanced(monkeypatch, capsys):
    # The dangerous case: status says "ok", but it is the previous run's "ok".
    # Nothing about the site looks broken and the catalogue is stale anyway.
    wire(monkeypatch, [CONFIGURED, CONFIGURED])

    assert sync.main() == 1
    out = capsys.readouterr().out
    assert BEFORE in out
    assert "no se ha actualizado" in out


def test_a_post_timeout_is_not_an_outcome(monkeypatch, capsys):
    # The endpoint is synchronous and the sync outlives the socket. Timing out
    # says nothing — the sync carries on server-side, so the script has to keep
    # polling rather than report a failure it has not observed.
    def timeout():
        raise TimeoutError("socket timed out")

    wire(
        monkeypatch,
        [CONFIGURED, {**CONFIGURED, "lastSyncStatus": "running"}, {**CONFIGURED, "lastSyncAt": AFTER}],
        run=timeout,
    )

    assert sync.main() == 0
    assert capsys.readouterr().out == ""


def test_an_http_error_from_the_run_endpoint_is_reported(monkeypatch, capsys):
    def http_error():
        raise urllib.error.HTTPError(
            f"{SITE}/api/sync/liderpapel/run", 500, "Server Error", {},
            __import__("io").BytesIO(b'{"error":"El feed no devolvio ningun producto VAL"}'),
        )

    wire(monkeypatch, [CONFIGURED], run=http_error)

    assert sync.main() == 1
    assert "ningun producto VAL" in capsys.readouterr().out


def test_reports_a_sync_stuck_running(monkeypatch, capsys):
    monkeypatch.setattr(sync, "POLL_DEADLINE", 0)
    wire(monkeypatch, [CONFIGURED, {**CONFIGURED, "lastSyncStatus": "running"}])

    assert sync.main() == 1
    assert "running" in capsys.readouterr().out


def test_reports_a_profile_without_site_credentials(monkeypatch, capsys):
    monkeypatch.setattr(sync, "_get_site_credentials", lambda: (None, None))

    assert sync.main() == 1
    assert "BL_SITE_URL" in capsys.readouterr().out


def test_reports_a_site_with_no_liderpapel_connection(monkeypatch, capsys):
    # A client on bl-site-package without a distributor feed is a normal
    # deployment, not a broken one — but a sync job pointed at it is a
    # misconfiguration worth naming rather than retrying nightly in silence.
    wire(monkeypatch, [{"hasPassword": False, "supplierCode": None}])

    assert sync.main() == 1
    assert "conexión con Liderpapel" in capsys.readouterr().out


def test_never_posts_when_the_site_is_not_configured(monkeypatch):
    calls = wire(monkeypatch, [{"hasPassword": False, "supplierCode": None}])

    sync.main()
    assert calls["run"] == 0
