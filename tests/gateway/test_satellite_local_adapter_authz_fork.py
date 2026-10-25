"""A shared-bot satellite's LOCAL session adapter does not cut it off from the primary's bot.

Since upstream v0.22.5 ``local_adapter_map`` (``gateway/session_local_recovery.py``) keeps a
served secondary's ``LocalSessionAdapter`` in ``runner._profile_adapters[<profile>]``.
``_adapters_for_profile`` read any non-empty map as "has its own bot", so a routed satellite
resolved no Telegram adapter for restored sources, restart notices, goal wakeups and completion
notices: they were skipped at DEBUG. Its own LOCAL must stay its own (``session_local.py``
looks the satellite's local sessions up through the same map).

Reuses the upstream ``mux`` fixture: ``ops`` is a routed shared-bot satellite, ``team_b`` owns
a Telegram bot.
"""
from gateway.config import Platform
from gateway.profile_routing import holds_messaging_adapter
from gateway.session import SessionSource
from tests.gateway.test_multiplex_routing_authz import _stub, mux


def _restored(profile="ops"):
    return SessionSource(platform=Platform.TELEGRAM, chat_id="72719239", chat_type="dm",
                         user_id="7", profile=profile)


def test_a_local_only_satellite_resolves_the_primary_bot_for_restored_sources(mux):
    ops_local = _stub(Platform.LOCAL, mux.runner, "OPS-LOCAL")
    mux.runner._profile_adapters["ops"] = {Platform.LOCAL: ops_local}
    runner = mux.runner
    assert runner._authorization_adapter(Platform.TELEGRAM, "ops") is mux.primary
    assert runner._delivery_adapter_for(_restored()) is mux.primary
    assert runner._resolve_injection_adapter("telegram", _restored()) is mux.primary


def test_the_satellites_own_local_adapter_stays_its_own(mux):
    ops_local = _stub(Platform.LOCAL, mux.runner, "OPS-LOCAL")
    mux.runner.adapters[Platform.LOCAL] = _stub(Platform.LOCAL, mux.runner, "PRIMARY-LOCAL")
    mux.runner._profile_adapters["ops"] = {Platform.LOCAL: ops_local}
    adapters = mux.runner._adapters_for_profile("ops")
    assert adapters[Platform.LOCAL] is ops_local
    assert adapters[Platform.TELEGRAM] is mux.primary
    assert mux.runner.adapters[Platform.LOCAL].label == "PRIMARY-LOCAL"   # primary map untouched


def test_an_empty_satellite_map_still_gets_the_primarys_live_map(mux):
    assert mux.runner._adapters_for_profile("ops") is mux.runner.adapters


def test_a_secondary_with_its_own_bot_keeps_its_own_map(mux):
    mux.runner._profile_adapters["team_b"][Platform.LOCAL] = _stub(Platform.LOCAL, mux.runner, "B-LOCAL")
    assert mux.runner._authorization_adapter(Platform.TELEGRAM, "team_b") is mux.team_b


def test_an_unrouted_local_only_secondary_never_borrows_the_primary(mux):
    mux.runner._profile_adapters["team_b"] = {Platform.LOCAL: _stub(Platform.LOCAL, mux.runner, "B-LOCAL")}
    assert mux.runner._authorization_adapter(Platform.TELEGRAM, "team_b") is None
    assert mux.runner._delivery_adapter_for(_restored("team_b")) is None


def test_holds_messaging_adapter_ignores_local_only():
    assert not holds_messaging_adapter({})
    assert not holds_messaging_adapter(None)
    assert not holds_messaging_adapter({Platform.LOCAL: object()})
    assert holds_messaging_adapter({Platform.LOCAL: object(), Platform.TELEGRAM: object()})
