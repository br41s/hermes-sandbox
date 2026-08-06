"""Tests for the payment-confirmed rental provisioning webhook.

Covers the two things that make this endpoint safe to expose to a paid
checkout flow: it authenticates every request itself, and a Stripe retry can
never provision the same order twice.
"""

import hashlib
import hmac
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import bl_rental_webhook as whook
from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS

SECRET = "test-shared-secret"

VALID_ANSWERS = {
    "company_name": "Fontanería García",
    "sector": "Instalaciones",
    "notify_email": "hola@garcia.example",
}


def _order(**overrides) -> dict:
    body = {
        "order_id": "ord_123",
        "slug": "bl-cliente-garcia",
        "client_name": "Fontanería García",
        "openrouter_key": "sk-or-v1-test",
        "agents": ["site-setup"],
        "questionnaire": VALID_ANSWERS,
    }
    body.update(overrides)
    return body


def _post(client: TestClient, body: dict, *, secret=SECRET, timestamp=None, tamper=False):
    raw = json.dumps(body).encode()
    ts = str(int(time.time())) if timestamp is None else str(timestamp)
    sig = hmac.new(secret.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    if tamper:
        sig = "0" * 64
    return client.post(
        "/api/bl/rental/provision",
        content=raw,
        headers={
            "content-type": "application/json",
            whook.SIGNATURE_HEADER: f"sha256={sig}",
            whook.TIMESTAMP_HEADER: ts,
        },
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv(whook.SECRET_ENV_VAR, SECRET)
    monkeypatch.setattr(whook, "_notify_ceo", lambda text: None)
    app = FastAPI()
    app.include_router(whook.router)
    return TestClient(app)


@pytest.fixture
def pool(tmp_path):
    """A pool with one blank instance to claim."""
    (tmp_path / whook.INSTANCES_FILENAME).write_text(
        json.dumps({"instances": [{"site_url": "https://bl-blank-01.example", "status": "free"}]}),
        encoding="utf-8",
    )
    return tmp_path / whook.INSTANCES_FILENAME


def _stub_provision(monkeypatch, calls: list, outcome=None):
    """Replace the blocking provisioning call. outcome=None means success."""

    def fake(order, site_url, panel_password):
        calls.append({"order_id": order.order_id, "site_url": site_url, "password": panel_password})
        if outcome is not None:
            return outcome
        return None, "", {"profile": order.slug, "jobs": [{"job_id": "job_1"}], "site_setup": {"ok": True}}

    monkeypatch.setattr(whook, "_run_provision", fake)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def test_route_is_on_the_dashboard_auth_allowlist():
    # The BigLobster server-to-server call has no OAuth cookie; if this drifts
    # out of the allowlist every paid order 401s at the gate before the HMAC
    # check ever runs.
    assert "/api/bl/rental/provision" in PUBLIC_API_PATHS


def test_unsigned_request_is_rejected(client):
    resp = client.post("/api/bl/rental/provision", json=_order())
    assert resp.status_code == 401


def test_bad_signature_is_rejected(client, pool):
    resp = _post(client, _order(), tamper=True)
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


def test_wrong_secret_is_rejected(client, pool):
    resp = _post(client, _order(), secret="not-the-secret")
    assert resp.status_code == 401


def test_stale_timestamp_is_rejected(client, pool):
    stale = int(time.time()) - whook.MAX_TIMESTAMP_SKEW_SECONDS - 60
    resp = _post(client, _order(), timestamp=stale)
    assert resp.status_code == 401
    assert "replay window" in resp.json()["detail"]


def test_route_fails_closed_without_a_configured_secret(client, monkeypatch):
    monkeypatch.delenv(whook.SECRET_ENV_VAR)
    resp = _post(client, _order())
    assert resp.status_code == 503
    assert resp.json()["code"] == "not_configured"


# ---------------------------------------------------------------------------
# Happy path + idempotency
# ---------------------------------------------------------------------------

def test_provisions_and_returns_panel_credentials(client, pool, monkeypatch, tmp_path):
    calls: list = []
    _stub_provision(monkeypatch, calls)

    resp = _post(client, _order())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "provisioned"
    assert body["profile"] == "bl-cliente-garcia"
    assert body["site_url"] == "https://bl-blank-01.example"
    assert body["panel_url"] == "https://bl-blank-01.example/panel"
    # Generated for the buyer when the order doesn't carry one.
    assert body["panel_password"]
    assert len(calls) == 1

    # The claimed instance is no longer free.
    instances = json.loads((tmp_path / whook.INSTANCES_FILENAME).read_text())["instances"]
    assert instances[0]["status"] == "claimed"
    assert instances[0]["order_id"] == "ord_123"


def test_retried_webhook_does_not_double_provision(client, pool, monkeypatch):
    calls: list = []
    _stub_provision(monkeypatch, calls)

    first = _post(client, _order())
    second = _post(client, _order())

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True
    # Same profile and, crucially, the same panel password — a fresh one would
    # lock the buyer out of the panel they were already emailed.
    assert second.json()["panel_password"] == first.json()["panel_password"]
    assert len(calls) == 1, "a Stripe retry must not provision a second time"


def test_explicit_site_url_bypasses_the_pool(client, pool, monkeypatch, tmp_path):
    calls: list = []
    _stub_provision(monkeypatch, calls)

    resp = _post(client, _order(site_url="https://already-mine.example"))
    assert resp.status_code == 200
    assert calls[0]["site_url"] == "https://already-mine.example"
    instances = json.loads((tmp_path / whook.INSTANCES_FILENAME).read_text())["instances"]
    assert instances[0]["status"] == "free"


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

def test_empty_pool_returns_503_and_provisions_nothing(client, monkeypatch):
    calls: list = []
    _stub_provision(monkeypatch, calls)

    resp = _post(client, _order())
    assert resp.status_code == 503
    assert resp.json()["code"] == "no_instance_available"
    assert calls == []


@pytest.mark.parametrize(
    "code,status",
    [
        ("invalid_api_key", 400),
        ("invalid_questionnaire", 400),
        ("slug_collision", 409),
        ("site_already_claimed", 409),
        ("site_unreachable", 502),
        ("internal", 500),
    ],
)
def test_failure_codes_map_to_retryability(client, pool, monkeypatch, code, status):
    _stub_provision(monkeypatch, [], outcome=(code, "boom", None))
    resp = _post(client, _order())
    assert resp.status_code == status
    assert resp.json()["code"] == code


def test_failed_order_releases_its_pooled_instance_and_can_retry(client, pool, monkeypatch, tmp_path):
    _stub_provision(monkeypatch, [], outcome=("site_unreachable", "down", None))
    assert _post(client, _order()).status_code == 502

    instances = json.loads((tmp_path / whook.INSTANCES_FILENAME).read_text())["instances"]
    assert instances[0]["status"] == "free", "a failed order must not burn a blank instance"

    calls: list = []
    _stub_provision(monkeypatch, calls)
    assert _post(client, _order()).status_code == 200
    assert len(calls) == 1


def test_order_stuck_in_progress_by_a_crash_can_be_retaken(client, pool, monkeypatch, tmp_path):
    (tmp_path / whook.ORDERS_FILENAME).write_text(
        json.dumps({"ord_123": {"status": "in_progress",
                                "started_at": int(time.time()) - whook.STALE_IN_PROGRESS_SECONDS - 1}}),
        encoding="utf-8",
    )
    calls: list = []
    _stub_provision(monkeypatch, calls)
    assert _post(client, _order()).status_code == 200
    assert len(calls) == 1


def test_concurrent_duplicate_is_held_off_with_409(client, pool, tmp_path):
    (tmp_path / whook.ORDERS_FILENAME).write_text(
        json.dumps({"ord_123": {"status": "in_progress", "started_at": int(time.time())}}),
        encoding="utf-8",
    )
    resp = _post(client, _order())
    assert resp.status_code == 409
    assert resp.json()["status"] == "in_progress"


def test_malformed_payload_is_a_400_not_a_crash(client, pool):
    resp = _post(client, {"order_id": "ord_9"})  # missing required fields
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_order"
