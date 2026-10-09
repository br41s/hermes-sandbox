"""Scope-aware settings for the shorts studio and its publishers.

Under multiplex every profile's agent runs in one process, so ``os.environ``
is the DEFAULT profile's env for everyone. A bare ``os.environ`` read made a
rented tenant's ``shorts`` agent see ``shorts_studio`` and resolve
BigLobster's own GitHub, YouTube and Meta credentials. Reading through
``agent.secret_scope.get_secret`` gives each run its own profile's view:
the default profile still sees the Zeabur service env (launch scope,
``HERMES_FORK_SCOPE_PROCESS_ENV``), a tenant sees only its ``.env``.

No scope at all while multiplexing is a spawn-site bug that ``get_secret``
raises on; here it reads as "not configured", so the studio stays hidden
and nothing publishes rather than guessing whose key to use.
"""

from __future__ import annotations

import os


def env(name: str, default: str = "") -> str:
    try:
        from agent.secret_scope import UnscopedSecretError, get_secret
    except ImportError:  # outside the Hermes tree (the Actions renderer)
        return (os.environ.get(name) or default).strip()
    try:
        value = get_secret(name)
    except UnscopedSecretError:
        return default
    return (value or default).strip()
