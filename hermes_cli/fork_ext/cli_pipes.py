"""Quiet exit when a CLI command's reader goes away (fork-owned).

``hermes cron runs <id> | head -3`` ends with a ``BrokenPipeError`` traceback: ``head``
exits after three lines and the handler's next ``print`` hits a closed pipe. Standard Unix
tools die silently of SIGPIPE there. ``hermes_cli.main`` wraps subcommand dispatch in
``quiet_broken_pipe()`` to behave the same way.

Scoped to that dispatch on purpose. Restoring the default SIGPIPE disposition process-wide
would also cover ``hermes gateway run``, where a closed log pipe would then kill the
production gateway silently instead of raising.
"""

from __future__ import annotations

import contextlib
import os
import sys
from typing import Iterator

# What a shell reports for a writer killed by SIGPIPE (128 + 13), as for `yes | head -1`.
BROKEN_PIPE_EXIT = 141


@contextlib.contextmanager
def quiet_broken_pipe() -> Iterator[None]:
    try:
        yield
    except BrokenPipeError:
        # Point stdout at /dev/null first: the interpreter flushes it again at shutdown, and
        # that flush would print "Exception ignored ... BrokenPipeError" instead.
        with contextlib.suppress(OSError, ValueError):
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, sys.stdout.fileno())
            finally:
                os.close(devnull)
        sys.exit(BROKEN_PIPE_EXIT)
