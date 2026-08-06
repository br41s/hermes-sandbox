"""End-to-end test of the deterministic template application.

Runs apply_site_template against a real HTTP server that mimics
bl-site-package's contract (``/api/setup/status``, ``/api/setup/complete``,
``/api/auth/login``, ``/api/site/texts``, ``/api/site/logo``). Mocks would hide
exactly the bugs that matter here — the multipart logo encoding, the
already-configured branch, and the order of the calls.
"""

import json
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from scripts.bl_site_setup import SiteSetupError, apply_site_template

ANSWERS = {
    "company_name": "Fontanería García",
    "sector": "Instalaciones",
    "notify_email": "hola@garcia.example",
    "legal_name": "García e Hijos SL",
    "biz_city": "Vigo",
}


def _tiny_png() -> bytes:
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
        + chunk(b"IEND", b"")
    )


class _FakeSite(BaseHTTPRequestHandler):
    state: dict = {}

    def log_message(self, *args):  # silence the test output
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/setup/status":
            self._json(200, {"configured": self.state.get("configured", False)})
        elif self.path == "/logo.png":
            data = _tiny_png()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        self.state.setdefault("calls", []).append(self.path)

        if self.path == "/api/setup/complete":
            if self.state.get("configured"):
                self._json(409, {"error": "already configured"})
                return
            self.state["setup"] = json.loads(raw)
            self.state["configured"] = True
            self._json(200, {"success": True})
        elif self.path == "/api/auth/login":
            if json.loads(raw).get("password") != self.state.get("setup", {}).get("panelPassword"):
                self._json(401, {"error": "bad password"})
                return
            self._json(200, {"token": "jwt-token"})
        elif self.path == "/api/site/texts":
            assert self.headers.get("Authorization") == "Bearer jwt-token"
            self.state["texts"] = json.loads(raw)
            self._json(200, {"success": True})
        elif self.path == "/api/site/logo":
            assert self.headers.get("Authorization") == "Bearer jwt-token"
            assert b'name="logo"; filename="logo.png"' in raw
            assert _tiny_png() in raw
            self.state["logo"] = True
            self._json(200, {"success": True, "path": "/uploads/logo.png"})
        else:
            self._json(404, {"error": "not found"})


@pytest.fixture
def site():
    _FakeSite.state = {}
    server = HTTPServer(("127.0.0.1", 0), _FakeSite)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", _FakeSite.state
    server.shutdown()
    server.server_close()


def test_applies_the_whole_template_on_a_blank_instance(site):
    url, state = site
    report = apply_site_template(
        url, "panel-pw-123", {**ANSWERS, "logo_url": f"{url}/logo.png"},
        "sk-or-v1-test", image_model="fal-ai/flux-2/klein/9b",
    )

    assert report["setup_completed"] is True
    assert state["setup"] == {
        "companyName": "Fontanería García",
        "sector": "Instalaciones",
        "panelPassword": "panel-pw-123",
        "openrouterApiKey": "sk-or-v1-test",
    }
    # Verbatim copy — the buyer's own words, no model in the path.
    assert state["texts"]["legal_name"] == "García e Hijos SL"
    assert state["texts"]["biz_city"] == "Vigo"
    assert state["texts"]["image_model"] == "fal-ai/flux-2/klein/9b"
    assert report["logo"] == "/uploads/logo.png"
    # The copywriting fields are NOT touched here — that's the agent's job.
    assert not any(k.startswith("page_") for k in state["texts"])


def test_reapplying_on_an_already_configured_instance_converges(site):
    url, state = site
    apply_site_template(url, "panel-pw-123", ANSWERS, "sk-or-v1-test")
    report = apply_site_template(url, "panel-pw-123", {**ANSWERS, "biz_city": "Pontevedra"}, "sk-or-v1-test")

    # A retried provision must not fail on the self-sealing setup endpoint.
    assert report["setup_completed"] is False
    assert state["texts"]["biz_city"] == "Pontevedra"


def test_instance_claimed_under_another_password_is_reported_not_overwritten(site):
    url, _ = site
    apply_site_template(url, "panel-pw-123", ANSWERS, "sk-or-v1-test")
    with pytest.raises(SiteSetupError) as exc:
        apply_site_template(url, "a-different-password", ANSWERS, "sk-or-v1-test")
    assert exc.value.code == "site_already_claimed"


def test_a_broken_logo_never_fails_a_paid_order(site):
    url, state = site
    report = apply_site_template(
        url, "panel-pw-123", {**ANSWERS, "logo_url": f"{url}/does-not-exist.png"}, "sk-or-v1-test"
    )
    assert report["logo"].startswith("skipped:")
    assert state["texts"]["legal_name"] == "García e Hijos SL"


def test_unreachable_instance_is_a_distinct_code():
    with pytest.raises(SiteSetupError) as exc:
        apply_site_template("http://127.0.0.1:1", "pw", ANSWERS, "sk-or-v1-test")
    assert exc.value.code == "site_unreachable"
