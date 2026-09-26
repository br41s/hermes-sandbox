"""Fork's own tests for the bundled Nous dashboard-auth plugin, kept out of upstream's file so upstream merges do not conflict."""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import plugins.dashboard_auth.nous as nous_plugin


@pytest.fixture(scope="module")
def rsa_keypair() -> Dict[str, Any]:
    """Generate an RS256 keypair + matching JWK for verify_session tests."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_numbers = key.public_key().public_numbers()

    def _b64url_uint(n: int) -> str:
        length = (n.bit_length() + 7) // 8
        return (
            base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()
        )

    jwk = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": "test-key-1",
        "n": _b64url_uint(public_numbers.n),
        "e": _b64url_uint(public_numbers.e),
    }
    return {"private_pem": private_pem, "jwk": jwk, "kid": jwk["kid"]}


# ---------------------------------------------------------------------------
# Token-mint helper
# ---------------------------------------------------------------------------


def _mint_token(
    rsa_keypair: Dict[str, Any],
    *,
    iss: str = "https://portal.example.com",
    aud: str = "agent:inst123",
    sub: str = "usr_abc",
    agent_instance_id: str | None = "inst123",
    oauth_contract_version: Any = 1,
    org_id: str | None = "org_xyz",
    scope: str = "agent_dashboard:access",
    ttl_seconds: int = 900,
    extra_claims: Dict[str, Any] | None = None,
) -> str:
    now = int(time.time())
    claims = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "iat": now,
        "exp": now + ttl_seconds,
        "scope": scope,
    }
    if agent_instance_id is not None:
        claims["agent_instance_id"] = agent_instance_id
    if oauth_contract_version is not None:
        claims["oauth_contract_version"] = oauth_contract_version
    if org_id is not None:
        claims["org_id"] = org_id
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(
        claims,
        rsa_keypair["private_pem"],
        algorithm="RS256",
        headers={"kid": rsa_keypair["kid"]},
    )


def _patched_jwks(provider: nous_plugin.NousDashboardAuthProvider, rsa_keypair):
    """Patch the provider's JWKS client to return our fixture key."""
    fake_key = MagicMock()
    fake_key.key = serialization.load_pem_private_key(
        rsa_keypair["private_pem"].encode(), password=None
    ).public_key()
    fake_client = MagicMock()
    fake_client.get_signing_key_from_jwt.return_value = fake_key
    provider._jwks_client = fake_client


class TestPortalPostsSendExplicitUserAgent:
    """Token-endpoint and refresh POSTs must not use httpx's default UA."""

    @pytest.fixture
    def provider(self, rsa_keypair):
        p = nous_plugin.NousDashboardAuthProvider(
            client_id="agent:inst123", portal_url="https://portal.example.com"
        )
        _patched_jwks(p, rsa_keypair)
        return p

    def _mock_post(self, status_code: int, body: Any, *, ctype: str = "application/json"):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = status_code
        if isinstance(body, dict):
            resp.text = json.dumps(body)
            resp.json = MagicMock(return_value=body)
        else:
            resp.text = body
            # _parse_json_body bails on non-application/json before .json()
            # is called, but be safe for callers that pass a non-dict body
            # with ctype=application/json.
            resp.json = MagicMock(side_effect=ValueError("not json"))
        resp.headers = {"content-type": ctype}
        return resp

    def test_auth_code_post_sends_explicit_user_agent(self, provider, rsa_keypair):
        """Regression: the token-endpoint POST must send an explicit
        User-Agent. httpx's default (``python-httpx/x.y``) is blocked by the
        Portal WAF with an HTML 403, which surfaces as a ProviderError and a
        503 to the browser. Same fix as the JWKS client (see
        ``test_jwks_client_sends_explicit_http_headers``)."""
        mock_resp = self._mock_post(
            200,
            {"access_token": _mint_token(rsa_keypair), "token_type": "Bearer"},
        )
        with patch(
            "plugins.dashboard_auth.nous.httpx.post", return_value=mock_resp
        ) as mock_post:
            provider.complete_login(
                code="abc",
                state="state-val",
                code_verifier="vfy",
                redirect_uri="https://hermes.fly.dev/auth/callback",
            )
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["User-Agent"] == "HermesAgent/1.0"
        assert kwargs["headers"]["Accept"] == "application/json"

    def test_refresh_post_sends_explicit_user_agent(self, provider, rsa_keypair):
        """Regression: the refresh POST must send an explicit User-Agent, and
        must not lose the ``x-nous-refresh-token`` header while doing so. The
        refresh path is the one that hits the Portal WAF repeatedly, so a
        blocked UA here kills a live session seconds after login."""
        mock_resp = self._mock_post(
            200,
            {
                "access_token": _mint_token(rsa_keypair),
                "token_type": "Bearer",
                "refresh_token": "rt_rotated_value",
            },
        )
        with patch(
            "plugins.dashboard_auth.nous.httpx.post", return_value=mock_resp
        ) as mock_post:
            provider.refresh_session(refresh_token="rt_old_value")
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["User-Agent"] == "HermesAgent/1.0"
        assert kwargs["headers"]["Accept"] == "application/json"
        assert kwargs["headers"]["x-nous-refresh-token"] == "rt_old_value"
