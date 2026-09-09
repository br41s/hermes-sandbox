#!/usr/bin/env python3
"""Cron entrypoint for the merge-on-green watcher.

``_run_job_script`` only executes files under ``$HERMES_HOME/scripts/``, so a
copy of this file lives there while the logic it calls stays version-controlled
in the repo. Keep this wrapper thin: everything worth testing belongs in
``merge_on_green/``.

Deploy:
    cp scripts/merge_on_green.py "$HERMES_HOME/scripts/merge_on_green.py"

Cron job (no_agent — the script IS the job, empty stdout delivers nothing):
    cronjob(action="create",
            name="merge-on-green",
            script="merge_on_green.py",
            no_agent=True,
            schedule="*/15 * * * *",
            deliver="telegram")

Configure the repo list in $HERMES_HOME/.env:
    MERGE_ON_GREEN_REPOS=br41s/FinView,br41s/biglobster

Stop everything without touching this job:
    HERMES_AUTONOMY=paused
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _ensure_importable() -> None:
    """Make the ``merge_on_green`` *package* importable.

    Running a script puts its own directory on ``sys.path[0]``, so a wrapper
    named ``merge_on_green.py`` shadows the package it is trying to reach:
    ``import merge_on_green`` binds to this file, which has no ``__main__``
    submodule. Drop the script's directory first, then resolve for real. A
    package has ``__path__``; a lone module does not, which is the check.
    """
    here = str(Path(__file__).resolve().parent)
    sys.path[:] = [p for p in sys.path if p not in ("", ".", here)]
    sys.modules.pop("merge_on_green", None)

    try:
        import merge_on_green
        if hasattr(merge_on_green, "__path__"):
            return
        sys.modules.pop("merge_on_green", None)
    except ImportError:
        pass

    candidates = []
    if os.environ.get("HERMES_REPO_ROOT"):
        candidates.append(Path(os.environ["HERMES_REPO_ROOT"]))
    candidates += [Path("/opt/hermes"), Path(__file__).resolve().parent.parent]

    for root in candidates:
        if (root / "merge_on_green" / "__init__.py").is_file():
            sys.path.insert(0, str(root))
            return

    print(
        "merge-on-green: cannot import the watcher. Set HERMES_REPO_ROOT to the "
        "Hermes checkout, or place this script inside it.",
        file=sys.stderr,
    )
    raise SystemExit(1)


if __name__ == "__main__":
    _ensure_importable()
    from merge_on_green.__main__ import main

    raise SystemExit(main(sys.argv[1:]))
