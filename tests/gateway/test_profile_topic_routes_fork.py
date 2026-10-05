"""Fork: multiplex stage 2b — profile-bound Telegram topics routed in-process.

hermes_cli/fork_ext/boot_reconcile.py turns every group_topics topic bound to a profile
into a gateway.profile_routes entry, and upstream's multiplex path runs the turn in that
profile. These pin the generation, that upstream's own loader and matcher accept what it
writes, and the rollback. A bound topic no route matched is dropped
(test_telegram_unrouted_bound_topic_fork.py).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.config_loader import bridge_toplevel_keys
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent, MessageType
from gateway.profile_routing import match_profile_route, parse_profile_routes
from gateway.session import SessionSource, build_session_key
from hermes_cli.fork_ext import boot_reconcile as br

CHAT = -1004224848555
SERVED = {"default", "biglobster", "grow-shop", "finview"}


def _cfg(*topics, chat=CHAT) -> dict:
    return {"telegram": {"extra": {"group_topics": [{"chat_id": chat, "topics": list(topics)}]}}}


def _topic(profile, thread, name=None):
    return {"name": name or profile, "profile": profile, "thread_id": thread}


# ── generation ─────────────────────────────────────────────────────────────────


def test_one_route_per_bound_topic_of_a_served_profile():
    cfg = _cfg(_topic("grow-shop", 3), _topic("biglobster", 2), _topic("ghost", 9),
               {"name": "general", "thread_id": 1}, {"name": "no-thread", "profile": "finview"})

    routes = br.topic_routes(cfg, SERVED)

    assert routes == [
        {"name": "fork-topic:biglobster:2", "platform": "telegram",
         "chat_id": str(CHAT), "thread_id": "2", "profile": "biglobster"},
        {"name": "fork-topic:grow-shop:3", "platform": "telegram",
         "chat_id": str(CHAT), "thread_id": "3", "profile": "grow-shop"},
    ]


def test_generated_routes_are_what_upstream_matches():
    cfg = _cfg(_topic("grow-shop", 3), _topic("biglobster", 2))
    routes = parse_profile_routes(br.topic_routes(cfg, SERVED))

    def match(thread):
        route = match_profile_route(routes, platform="telegram", chat_id=str(CHAT), thread_id=thread)
        return route.profile if route else None

    assert match("3") == "grow-shop"
    assert match("2") == "biglobster"
    assert match("61") is None  # an unbound topic stays on the default profile
    assert match(None) is None  # the group's general chat too


@pytest.mark.parametrize("nested", [True, False])
def test_upstream_loader_reads_the_routes_where_they_are_written(nested):
    cfg = _cfg(_topic("grow-shop", 3))
    if not nested:
        cfg["profile_routes"] = []
    assert br.reconcile_profile_routes(cfg, SERVED)

    gw_data: dict = {}
    bridge_toplevel_keys(cfg, cfg.get("gateway"), gw_data)
    loaded = GatewayConfig.from_dict({**gw_data, "multiplex_profiles": True})

    assert [(r.profile, r.chat_id, r.thread_id) for r in loaded.profile_routes] == [
        ("grow-shop", str(CHAT), "3")]


def test_a_top_level_key_is_written_not_shadowed():
    """A top-level profile_routes (even []) wins upstream's bridge over gateway.profile_routes."""
    cfg = _cfg(_topic("grow-shop", 3))
    cfg["profile_routes"] = []
    br.reconcile_profile_routes(cfg, SERVED)
    assert [r["profile"] for r in cfg["profile_routes"]] == ["grow-shop"]
    assert "profile_routes" not in cfg.get("gateway", {})


# ── reconcile ──────────────────────────────────────────────────────────────────


def test_human_routes_are_kept_and_ours_replaced():
    human = {"name": "ops-dm", "platform": "telegram", "user_id": "42", "profile": "finview"}
    stale = {"name": "fork-topic:finview:61", "platform": "telegram",
             "chat_id": str(CHAT), "thread_id": "61", "profile": "finview"}
    cfg = _cfg(_topic("grow-shop", 3))
    cfg["gateway"] = {"multiplex_profiles": True, "profile_routes": [human, stale]}

    assert br.reconcile_profile_routes(cfg, SERVED) is True
    assert [r["name"] for r in cfg["gateway"]["profile_routes"]] == ["ops-dm", "fork-topic:grow-shop:3"]
    assert cfg["gateway"]["multiplex_profiles"] is True
    assert br.reconcile_profile_routes(cfg, SERVED) is False  # second boot: no rewrite


def test_rollback_removes_only_ours():
    human = {"name": "ops-dm", "platform": "telegram", "user_id": "42", "profile": "finview"}
    cfg = _cfg(_topic("grow-shop", 3))
    cfg["gateway"] = {"profile_routes": [human]}
    br.reconcile_profile_routes(cfg, SERVED)

    assert br.reconcile_profile_routes(cfg, SERVED, enabled=False) is True
    assert cfg["gateway"]["profile_routes"] == [human]


def test_an_unreadable_served_set_leaves_the_routes_alone():
    cfg = _cfg(_topic("grow-shop", 3))
    cfg["gateway"] = {"profile_routes": [{"name": "fork-topic:x:1", "platform": "telegram",
                                           "profile": "x", "chat_id": "1", "thread_id": "1"}]}
    before = [dict(r) for r in cfg["gateway"]["profile_routes"]]
    assert br.reconcile_profile_routes(cfg, None) is False
    assert cfg["gateway"]["profile_routes"] == before


def test_main_config_gets_routes_and_profiles_do_not():
    main, profile = _cfg(_topic("grow-shop", 3)), _cfg(_topic("grow-shop", 3))
    br.reconcile_cfg(main, "main", {}, profiles_src=br.Path("/nonexistent"), served=SERVED)
    br.reconcile_cfg(profile, "grow-shop", {}, served=SERVED)
    assert [r["profile"] for r in main["gateway"]["profile_routes"]] == ["grow-shop"]
    assert "profile_routes" not in profile.get("gateway", {})


def test_routes_and_multiplex_move_together():
    """Routes only take effect under multiplex (see OVERRIDES); a rollback flips both.
    test_rollback_removes_only_ours covers what the next boot does with the routes."""
    assert br.ROUTE_BOUND_TOPICS is br.OVERRIDES[("gateway", "multiplex_profiles")] is True
