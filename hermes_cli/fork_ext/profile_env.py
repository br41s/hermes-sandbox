"""A cron profile run's ``.env`` for the child processes it spawns (fork-owned).

Cron profile jobs no longer load the profile's ``.env`` into ``os.environ``
(``cron/fork_ext/profile_scope.py`` installs it as the run's secret scope
instead), so a child process built from ``os.environ`` would otherwise lose
keys it used to inherit — ``BL_SITE_URL`` for the infographic prompt's
``printenv``, ``PEXELS_API_KEY`` for a rental's scripts, and so on.

``child_env_overlay()`` returns what the run's scope changes relative to
``os.environ``, and ``{}`` outside a cron profile run. The gate is an explicit
flag that only ``_job_profile_context`` sets (``profile_run()``), not "a home
override is active": the dashboard, kanban, memory OAuth and gateway paths set
home overrides too, and their children must keep building exactly the
environment they did before. The caller still applies its usual stripping on
top: the overlay only restores what ``os.environ`` would have held during the
run, it never widens what reaches a child.

Upstream v2026.9.24 solves the same problem with ``served_profile_child_env``;
this is the minimal fork-side bridge until that merge.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator

_IN_PROFILE_RUN: ContextVar[bool] = ContextVar("_fork_in_cron_profile_run", default=False)

# Keyed clients for FAL keys that differ from the process's own (see
# fal_client_for_current_key). Bounded LRU, keyed by a digest rather than the
# raw key; the evicted client is only dropped from the cache — a request
# handle still in flight keeps its own reference.
_FAL_CLIENTS_MAX = 8
_fal_clients: "OrderedDict[str, Any]" = OrderedDict()
_fal_clients_lock = threading.Lock()


@contextmanager
def profile_run() -> Iterator[None]:
    """Mark the current context as a cron profile run for ``child_env_overlay``."""
    token = _IN_PROFILE_RUN.set(True)
    try:
        yield
    finally:
        _IN_PROFILE_RUN.reset(token)


def child_env_overlay() -> Dict[str, str]:
    if not _IN_PROFILE_RUN.get():
        return {}
    try:
        from agent.secret_scope import current_secret_scope
    except Exception:
        return {}
    scope = current_secret_scope()
    if not scope:
        return {}
    # The cron profile scope carries the process environment under the
    # profile's .env; only what differs from os.environ needs overlaying.
    return {k: v for k, v in scope.items() if os.environ.get(k) != v}


def fal_client_for_current_key(fal_module: Any) -> Any:
    """The FAL client to submit with, resolved through the active secret scope.

    ``fal_client.submit`` is a bound method of a module-level ``SyncClient``
    whose credentials are a ``cached_property``: the first key it reads from
    ``os.environ`` serves every later call in the process. Rentals bring
    their own ``FAL_KEY`` (BYOK), so that cache let one client's artwork bill
    whichever client happened to generate first — and once profile keys stop
    reaching ``os.environ`` it would find no key at all.

    With no key in scope, the process's own key, or a stand-in module without
    ``SyncClient``, the module itself is returned: exactly the old call.
    Otherwise a ``SyncClient`` bound to that key. Sharing one across threads
    is what the module-level client already does for every caller; its HTTP
    transport is an ``httpx.Client``, which is safe for concurrent requests.
    """
    try:
        from agent.secret_scope import get_secret

        key = (get_secret("FAL_KEY") or "").strip()
    except Exception:
        key = ""
    client_cls = getattr(fal_module, "SyncClient", None)
    # os.environ only ever holds the process's own FAL_KEY now, so the module
    # client is right whenever the scope agrees with it.
    if not key or key == (os.environ.get("FAL_KEY") or "").strip() or client_cls is None:
        return fal_module
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    with _fal_clients_lock:
        client = _fal_clients.get(digest)
        if client is None:
            client = _fal_clients[digest] = client_cls(key=key)
            while len(_fal_clients) > _FAL_CLIENTS_MAX:
                _fal_clients.popitem(last=False)
        else:
            _fal_clients.move_to_end(digest)
        return client
