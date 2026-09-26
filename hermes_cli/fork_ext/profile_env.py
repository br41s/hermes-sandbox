"""A routed profile's ``.env`` for the child processes it spawns (fork-owned).

Cron profile jobs no longer load the profile's ``.env`` into ``os.environ``
(``cron/fork_ext/profile_scope.py`` installs it as the run's secret scope
instead), so a child process built from ``os.environ`` would otherwise lose
keys it used to inherit — ``BL_SITE_URL`` for the infographic prompt's
``printenv``, ``PEXELS_API_KEY`` for a rental's scripts, and so on.

``child_env_overlay()`` returns the bound scope when the current task runs
for a profile other than the process's own, and ``{}`` everywhere else, so a
default-profile job, a gateway turn or the CLI build exactly the environment
they did before. The caller still applies its usual stripping on top: the
overlay only restores what ``os.environ`` would have held, it never widens
what reaches a child.

Upstream v2026.9.24 solves the same problem with ``served_profile_child_env``;
this is the minimal fork-side bridge until that merge.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict

_fal_clients: Dict[str, Any] = {}
_fal_clients_lock = threading.Lock()


def child_env_overlay() -> Dict[str, str]:
    try:
        from agent.secret_scope import current_secret_scope
        from hermes_constants import get_hermes_home_override, get_process_hermes_home
    except Exception:
        return {}

    override = get_hermes_home_override()
    if not override:
        return {}
    try:
        if Path(override).resolve() == Path(get_process_hermes_home()).resolve():
            return {}
    except OSError:
        return {}
    scope = current_secret_scope()
    return dict(scope) if scope else {}


def fal_client_for_current_key(fal_module: Any) -> Any:
    """The FAL client to submit with: one per ``FAL_KEY``, resolved through
    the active secret scope.

    ``fal_client.submit`` is a bound method of a module-level ``SyncClient``
    whose credentials are a ``cached_property``: the first key it reads from
    ``os.environ`` serves every later call in the process. Rentals bring
    their own ``FAL_KEY`` (BYOK), so that cache let one client's artwork bill
    whichever client happened to generate first — and once profile keys stop
    reaching ``os.environ`` it would find no key at all. Keyed clients keep
    each profile on its own key. With no key in scope, or a stand-in module
    without ``SyncClient``, the module itself is returned: exactly the old call.
    """
    try:
        from agent.secret_scope import get_secret

        key = (get_secret("FAL_KEY") or "").strip()
    except Exception:
        key = ""
    client_cls = getattr(fal_module, "SyncClient", None)
    if not key or client_cls is None:
        return fal_module
    with _fal_clients_lock:
        client = _fal_clients.get(key)
        if client is None:
            client = _fal_clients[key] = client_cls(key=key)
        return client
