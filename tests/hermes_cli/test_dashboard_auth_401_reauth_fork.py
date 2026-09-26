"""Fork's own tests for dashboard-auth 401 re-auth handling, kept out of upstream's file so upstream merges do not conflict."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth.base import ProviderError
from hermes_cli.dashboard_auth.cookies import (
    SESSION_PROVIDER_COOKIE,
    SESSION_RT_COOKIE,
)
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider


@pytest.fixture
def gated_app():
    clear_providers()
    register_provider(StubAuthProvider())
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    client = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    yield client
    clear_providers()
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


class TestRefreshOutageOnDocumentNavigation:
    """A provider outage during transparent refresh answers 503 in the
    shape the requester can use: HTML for a page load, JSON for /api/*."""

    def _outage_client(self, gated_app):
        """Gated client whose only provider fails refresh with an outage."""
        class UnreachableProvider(StubAuthProvider):
            name = "unreachable"

            def refresh_session(self, *, refresh_token: str):
                raise ProviderError("simulated provider outage")

        clear_providers()
        register_provider(UnreachableProvider())
        gated_app.cookies.clear()
        gated_app.cookies.set(SESSION_RT_COOKIE, "opaque-refresh-token")
        gated_app.cookies.set(SESSION_PROVIDER_COOKIE, "unreachable")
        return gated_app

    def test_refresh_outage_on_document_nav_returns_html_not_json(self, gated_app):
        """A document navigation must not render a raw JSON blob.

        Regression: both 503 sites returned JSONResponse unconditionally,
        so a plain browser navigation during a provider outage painted the
        error envelope as the page instead of the dashboard.
        """
        client = self._outage_client(gated_app)

        response = client.get("/", follow_redirects=False)

        assert response.status_code == 503
        assert response.headers["content-type"].startswith("text/html")
        assert "Provider unavailable" in response.text
        assert not response.text.lstrip().startswith("{")

    def test_refresh_outage_on_document_nav_does_not_redirect_to_login(
        self, gated_app
    ):
        """The session is still valid — an outage must not start a re-login.

        Bouncing to /login here would discard a good session to work around
        a transient upstream failure, which is what the 503 path exists to
        avoid.
        """
        client = self._outage_client(gated_app)

        response = client.get("/", follow_redirects=False)

        assert response.status_code == 503
        assert "location" not in response.headers
        assert client.cookies.get(SESSION_RT_COOKIE) == "opaque-refresh-token"

    def test_refresh_outage_api_json_envelope_is_unchanged(self, gated_app):
        """The /api/* contract the SPA reads must not shift."""
        client = self._outage_client(gated_app)

        response = client.get("/api/sessions", follow_redirects=False)

        assert response.status_code == 503
        assert response.headers["content-type"].startswith("application/json")
        assert "unreachable" in response.json()["detail"]
