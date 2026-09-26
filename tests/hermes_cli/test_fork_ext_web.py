"""The fork's dashboard routes live on ``hermes_cli.fork_ext.web.router``.

They were moved out of upstream's ``web_server.py`` so upstream merges stop
conflicting. These tests pin what the move must not change: the routes are
still served by the dashboard app, ahead of the SPA catch-all, and the cron
copy route still reaches web_server's cron helpers (looked up at call time,
so patching them on ``web_server`` still takes effect).
"""

import asyncio

from starlette.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.fork_ext import web as fork_web

FORK_ROUTES = {
    ("/health", "GET"),
    ("/api/cron/jobs/{job_id}/copy", "POST"),
    ("/api/delegate", "POST"),
    ("/api/bl/rental/provision", "POST"),
}


def _route_index():
    index = {}
    for i, route in enumerate(web_server.app.routes):
        for method in getattr(route, "methods", None) or ():
            index.setdefault((route.path, method), i)
    return index


def test_fork_routes_mounted_before_any_catch_all():
    index = _route_index()
    missing = FORK_ROUTES - index.keys()
    assert not missing, f"fork routes not mounted on the app: {missing}"
    catch_all = [
        i for i, r in enumerate(web_server.app.routes)
        if "{full_path" in getattr(r, "path", "")
    ]
    last_fork = max(index[key] for key in FORK_ROUTES)
    assert all(last_fork < i for i in catch_all)


def test_health_is_ok():
    with TestClient(web_server.app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_copy_cron_job_uses_web_server_helpers(monkeypatch):
    source = {
        "id": "abc",
        "prompt": "p",
        "name": "n",
        "schedule": {"kind": "interval", "minutes": 30},
        "deliver": "telegram",
        "workdir": "/w",
    }
    calls = []

    def fake_call(profile, func_name, *args, **kwargs):
        calls.append((profile, func_name, args, kwargs))
        if func_name == "get_job":
            return source
        return {"id": "new", **kwargs}

    monkeypatch.setattr(web_server, "_find_cron_job_profile", lambda job_id: "grow-shop")
    monkeypatch.setattr(web_server, "_call_cron_for_profile", fake_call)

    result = asyncio.run(fork_web.copy_cron_job("abc", to_profile="default"))

    assert calls[0][:3] == ("grow-shop", "get_job", ("abc",))
    profile, func_name, _, kwargs = calls[1]
    assert (profile, func_name) == ("default", "create_job")
    assert kwargs["schedule"] == "every 30m"
    assert kwargs["deliver"] == "telegram"
    assert kwargs["workdir"] == "/w"
    assert result["id"] == "new"


def test_cron_schedule_expr():
    expr = fork_web._cron_schedule_expr
    assert expr({"schedule": {"kind": "cron", "expr": "0 9 * * *"}}) == "0 9 * * *"
    assert expr({"schedule": {"kind": "interval", "minutes": 15}}) == "every 15m"
    assert expr({"schedule": {"kind": "once"}, "schedule_display": "in 1h"}) == "in 1h"
