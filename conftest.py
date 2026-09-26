"""Fork's own pytest guards for the test suite, kept out of upstream's tests/conftest.py so upstream merges do not conflict."""

# pytest loads this root conftest before tests/conftest.py. The guards below
# used to live in that file; two adjustments keep them behaving exactly as
# they did there:
#
# * Scope. They only act on tests under tests/. A root conftest also covers
#   test files elsewhere in the repo (skills/*/tests, plugins/*/tests,
#   onsite-seo/), which never had these guards.
# * Order. Autouse fixtures from an outer conftest set up before those of an
#   inner one, so on their own these would now run before tests/conftest.py's
#   _hermetic_environment. Each guard requests that fixture first, so
#   credentials are scrubbed and HERMES_HOME is isolated before the guard
#   imports anything, as before.

from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent / "tests"

_NETWORK_GUARD_BYPASS_MARK = "network_guard_bypass"
_DEP_INSTALL_GUARD_BYPASS_MARK = "dep_install_guard_bypass"


def pytest_configure(config):  # noqa: D401 — pytest hook
    config.addinivalue_line(
        "markers",
        f"{_NETWORK_GUARD_BYPASS_MARK}: bypass the outbound-network guard "
        "(only for tests that genuinely need a real remote connection; "
        "prefer @pytest.mark.integration for anything hitting live services).",
    )
    config.addinivalue_line(
        "markers",
        f"{_DEP_INSTALL_GUARD_BYPASS_MARK}: bypass the lazy-installer guard "
        "(only for tests of dep_ensure's install-script resolution itself).",
    )


def _guarded_suite_test(request) -> bool:
    """True for a test under tests/, after tests/conftest's hermetic env is set up."""
    try:
        path = Path(str(request.node.path)).resolve()
    except Exception:
        return False
    if not path.is_relative_to(_TESTS_DIR):
        return False
    request.getfixturevalue("_hermetic_environment")
    return True


# ── Outbound-network guard ─────────────────────────────────────────────────
#
# Unit tests must never touch the real network. When one does (an unmocked
# fetch_models_dev(), a GitHub API poll, a skills-hub download), two bad
# things happen: the suite's result depends on network weather, and on a
# slow link the call can block a whole ``pytest tests/`` run indefinitely —
# observed 2026-07-01 hanging >20 min inside an established TLS connection
# that neither ``--timeout`` (signal can't interrupt the blocking C read)
# nor ``-m 'not integration'`` caught.
#
# This fixture patches ``socket.socket.connect``/``connect_ex`` to reject
# any non-loopback destination with a loud RuntimeError naming the test,
# so network leaks fail fast and identify themselves instead of hanging.
# Loopback and AF_UNIX are allowed — plenty of tests spin up local servers.
# Real integration tests opt out with ``@pytest.mark.integration`` (already
# deselected by default) or ``@pytest.mark.network_guard_bypass``.

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "::", ""}


def _is_local_address(address) -> bool:
    # AF_UNIX sockets pass a str/bytes path, not a (host, port) tuple.
    if isinstance(address, (str, bytes)):
        return True
    if isinstance(address, tuple) and address:
        host = address[0]
        if isinstance(host, bytes):
            try:
                host = host.decode("ascii", "replace")
            except Exception:
                return False
        if not isinstance(host, str):
            return False
        return host in _LOOPBACK_HOSTS or host.startswith("127.")
    return False


@pytest.fixture(autouse=True)
def _network_guard(request, monkeypatch):
    """Fail fast on any outbound (non-loopback) socket connection."""
    if not _guarded_suite_test(request):
        yield
        return
    if request.node.get_closest_marker(_NETWORK_GUARD_BYPASS_MARK) or \
            request.node.get_closest_marker("integration"):
        yield
        return

    import socket as _socket_mod

    real_connect = _socket_mod.socket.connect
    real_connect_ex = _socket_mod.socket.connect_ex
    test_id = request.node.nodeid

    def _reject(address):
        raise RuntimeError(
            f"Outbound network connection to {address!r} blocked in unit "
            f"test {test_id}. Mock the call, or mark the test with "
            f"@pytest.mark.integration / @pytest.mark.{_NETWORK_GUARD_BYPASS_MARK}."
        )

    def guarded_connect(self, address):
        if _is_local_address(address):
            return real_connect(self, address)
        _reject(address)

    def guarded_connect_ex(self, address):
        if _is_local_address(address):
            return real_connect_ex(self, address)
        _reject(address)

    monkeypatch.setattr(_socket_mod.socket, "connect", guarded_connect)
    monkeypatch.setattr(_socket_mod.socket, "connect_ex", guarded_connect_ex)
    yield


# ── Lazy-installer guard ───────────────────────────────────────────────────
#
# ``hermes_cli.dep_ensure.ensure_dependency`` shells out to install.sh /
# install.ps1, which runs REAL ``npm install`` (global into $HERMES_HOME and,
# in git checkouts, at the repo root — mutating package-lock.json and
# node_modules/). Production code calls it lazily when a dependency is
# missing, e.g. ``_find_agent_browser()`` when agent-browser isn't on PATH —
# so any test that reaches such a path on a dev machine without the dep
# silently kicks off a multi-minute live installer (observed 2026-07-01:
# a 20+ minute npm install inside ``pytest tests/``, plus an unwanted
# package-lock.json rewrite). npm runs in a subprocess, so the socket-level
# network guard above cannot catch it.
#
# Stub the install-script lookup so ensure_dependency degrades to its
# graceful "no install script found" → False path. Tests of dep_ensure
# itself patch ``_find_install_script`` explicitly and are unaffected.


@pytest.fixture(autouse=True)
def _lazy_install_guard(request, monkeypatch):
    """Prevent ensure_dependency() from running real installers in tests."""
    if not _guarded_suite_test(request):
        yield
        return
    if request.node.get_closest_marker(_DEP_INSTALL_GUARD_BYPASS_MARK):
        yield
        return
    try:
        import hermes_cli.dep_ensure as _dep_ensure
    except Exception:
        yield
        return
    monkeypatch.setattr(
        _dep_ensure, "_find_install_script", lambda *a, **k: (None, None)
    )
    yield
