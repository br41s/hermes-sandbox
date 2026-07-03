"""Tests for the webhook adapter's ``trigger_cron_job_id`` route mode.

``trigger_cron_job_id`` forces an EXISTING cron job due, with zero agent/LLM
involvement — no model ever sees this route's payload. Added after the
auditor's PR-trigger route (docker/cont-init.d/03-biglobster-config §6e)
shipped with an agent-turn + fixed-prompt design that silently did nothing:
_HERMES_WEBHOOK_SAFE_TOOLS (toolsets.py) intentionally excludes the terminal
tool from every webhook-triggered session, so a prompt telling the model to
run a shell command has no tool that could ever execute it.

Covers:
- Agent is NOT invoked (``handle_message`` never called)
- ``tools.cronjob_tools.cronjob`` is called with action="run" and the
  configured job_id
- HTTP returns 200 on success, 502 on a rejected/failed trigger or exception
- Startup validation rejects combining trigger_cron_job_id with deliver_only
- HMAC auth, rate limiting, and idempotency still apply
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH


def _make_adapter(routes, **extra_kw) -> WebhookAdapter:
    extra = {"host": "127.0.0.1", "port": 0, "routes": routes}
    extra.update(extra_kw)
    config = PlatformConfig(enabled=True, extra=extra)
    return WebhookAdapter(config)


def _create_app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_get("/health", adapter._handle_health)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


def _patch_cronjob_tool(return_value):
    return patch(
        "tools.cronjob_tools.cronjob",
        MagicMock(return_value=json.dumps(return_value)),
    )


# ===================================================================
# Core behaviour: agent bypass + correct dispatch
# ===================================================================

class TestTriggerCronJobBypassesAgent:

    @pytest.mark.asyncio
    async def test_post_triggers_job_without_agent(self):
        routes = {
            "auditor-pr-trigger": {
                "secret": _INSECURE_NO_AUTH,
                "events": ["pull_request"],
                "trigger_cron_job_id": "job_abc123",
            }
        }
        adapter = _make_adapter(routes)

        handle_message_calls: list[MessageEvent] = []

        async def _capture(event):
            handle_message_calls.append(event)

        adapter.handle_message = _capture

        app = _create_app(adapter)
        with _patch_cronjob_tool({"success": True, "job": {"job_id": "job_abc123"}}) as mock_cronjob:
            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post(
                    "/webhooks/auditor-pr-trigger",
                    json={"action": "opened", "number": 7, "repository": {"full_name": "org/repo"}},
                    headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-1"},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["status"] == "triggered"
                assert data["job_id"] == "job_abc123"

        mock_cronjob.assert_called_once_with(action="run", job_id="job_abc123")
        assert handle_message_calls == []

    @pytest.mark.asyncio
    async def test_rejected_trigger_returns_502(self):
        """cronjob() reporting success=False (e.g. stale/unknown job id) -> 502."""
        routes = {
            "r": {
                "secret": _INSECURE_NO_AUTH,
                "trigger_cron_job_id": "job_stale",
            }
        }
        adapter = _make_adapter(routes)
        app = _create_app(adapter)
        with _patch_cronjob_tool({"success": False, "error": "job not found"}):
            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post(
                    "/webhooks/r", json={}, headers={"X-GitHub-Delivery": "d-2"}
                )
                assert resp.status == 502
                data = await resp.json()
                assert data["error"] == "job not found"

    @pytest.mark.asyncio
    async def test_exception_returns_502(self):
        """If the cronjob tool import/call raises, we return 502 (not 500)."""
        routes = {
            "r": {
                "secret": _INSECURE_NO_AUTH,
                "trigger_cron_job_id": "job_x",
            }
        }
        adapter = _make_adapter(routes)
        app = _create_app(adapter)
        with patch("tools.cronjob_tools.cronjob", side_effect=RuntimeError("boom")):
            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post(
                    "/webhooks/r", json={}, headers={"X-GitHub-Delivery": "d-3"}
                )
                assert resp.status == 502
                data = await resp.json()
                assert data["error"] == "Cron trigger failed"
                assert "boom" not in json.dumps(data)


# ===================================================================
# Startup validation
# ===================================================================

class TestTriggerCronJobStartupValidation:

    @pytest.mark.asyncio
    async def test_combining_with_deliver_only_rejected(self):
        routes = {
            "bad": {
                "secret": _INSECURE_NO_AUTH,
                "trigger_cron_job_id": "job_x",
                "deliver_only": True,
                "deliver": "telegram",
            }
        }
        adapter = _make_adapter(routes)
        with pytest.raises(ValueError, match="separate dispatch modes"):
            await adapter.connect()

    @pytest.mark.asyncio
    async def test_valid_trigger_route_accepted(self):
        routes = {
            "good": {
                "secret": _INSECURE_NO_AUTH,
                "trigger_cron_job_id": "job_x",
            }
        }
        adapter = _make_adapter(routes)
        try:
            started = await adapter.connect()
            if started:
                await adapter.disconnect()
        except ValueError:
            pytest.fail("valid trigger_cron_job_id config should not raise ValueError")


# ===================================================================
# Security + reliability invariants still hold
# ===================================================================

class TestTriggerCronJobSecurityInvariants:

    @pytest.mark.asyncio
    async def test_hmac_still_enforced(self):
        routes = {
            "r": {
                "secret": "real-secret",
                "trigger_cron_job_id": "job_x",
            }
        }
        adapter = _make_adapter(routes)
        app = _create_app(adapter)
        with _patch_cronjob_tool({"success": True, "job": {}}) as mock_cronjob:
            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post(
                    "/webhooks/r", json={}, headers={"X-GitHub-Delivery": "d-noauth"}
                )
                assert resp.status == 401
            mock_cronjob.assert_not_called()

    @pytest.mark.asyncio
    async def test_idempotency_still_applies(self):
        routes = {
            "r": {
                "secret": _INSECURE_NO_AUTH,
                "trigger_cron_job_id": "job_x",
            }
        }
        adapter = _make_adapter(routes)
        app = _create_app(adapter)
        with _patch_cronjob_tool({"success": True, "job": {}}) as mock_cronjob:
            async with TestClient(TestServer(app)) as cli:
                r1 = await cli.post(
                    "/webhooks/r", json={}, headers={"X-GitHub-Delivery": "dup-1"}
                )
                assert r1.status == 200
                r2 = await cli.post(
                    "/webhooks/r", json={}, headers={"X-GitHub-Delivery": "dup-1"}
                )
                assert r2.status == 200
                data = await r2.json()
                assert data["status"] == "duplicate"
            assert mock_cronjob.call_count == 1

    @pytest.mark.asyncio
    async def test_rate_limit_still_applies(self):
        routes = {
            "r": {
                "secret": _INSECURE_NO_AUTH,
                "trigger_cron_job_id": "job_x",
            }
        }
        adapter = _make_adapter(routes, rate_limit=2)
        app = _create_app(adapter)
        with _patch_cronjob_tool({"success": True, "job": {}}):
            async with TestClient(TestServer(app)) as cli:
                for i in range(2):
                    r = await cli.post(
                        "/webhooks/r", json={}, headers={"X-GitHub-Delivery": f"rl-{i}"}
                    )
                    assert r.status == 200
                r3 = await cli.post(
                    "/webhooks/r", json={}, headers={"X-GitHub-Delivery": "rl-3"}
                )
                assert r3.status == 429
