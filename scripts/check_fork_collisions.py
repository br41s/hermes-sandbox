#!/usr/bin/env python3
"""Find paths where OUR additions share a name with UPSTREAM's tree.

A name collision is worse than a merge conflict, because git never asks. Two
bit us in the v2026.7.20 merge:

  * ``infographic/`` — upstream has a directory of the same name holding README
    assets, and its .dockerignore excludes it. Ours holds
    ``infographic-engineer.prompt``, the canonical prompt for a LIVE cron job.
    The .dockerignore change auto-merged (that file never conflicted) and the
    prompt silently stopped shipping. ``incidents.sweep`` resolves
    ``prompt_source`` against /opt/hermes, so the drift watcher would have
    posted a false "prompt source missing" incident every single sweep, and
    ``hermes cron sync-prompt`` would fail for that job. No test could catch it
    — tests/ is excluded from the image too.

  * ``plugins/image_gen/openrouter/`` — we wrote an OpenRouter image provider;
    upstream independently wrote its own at the same path. git reported an
    add/add conflict, and taking either side wholesale silently changes which
    model generates production images.

Run before adding a top-level directory, and as part of the upstream drift
check. Requires the ``upstream`` remote and a fetched ref to compare against.

    python scripts/check_fork_collisions.py                  # vs upstream/main
    python scripts/check_fork_collisions.py --ref v2026.7.20

Exit codes: 0 no collisions, 1 collisions found, 2 could not compare.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections import defaultdict


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout


def _tracked_at(ref: str) -> set[str]:
    return {p for p in _git("ls-tree", "-r", "--name-only", ref).splitlines() if p}


def _top_dirs(paths: set[str]) -> set[str]:
    return {p.split("/", 1)[0] for p in paths if "/" in p}


def _dockerignore_rules() -> tuple[list[str], list[str]]:
    """Return (excluded patterns, re-included '!' patterns) from .dockerignore."""
    excluded: list[str] = []
    reincluded: list[str] = []
    try:
        lines = open(".dockerignore", encoding="utf-8").read().splitlines()
    except OSError:
        return excluded, reincluded
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        (reincluded if line.startswith("!") else excluded).append(line.lstrip("!").rstrip("/"))
    return excluded, reincluded


def _matches_pattern(path: str, pat: str) -> bool:
    """Docker/Go filepath.Match semantics: ``*`` does NOT cross a ``/``.

    This distinction is the whole game. Naive ``fnmatch(path, "*.md")`` matches
    ``docker/profiles/biglobster/SOUL.md`` and reports 49 phantom exclusions —
    but Docker's ``*.md`` only matches .md files at the ROOT of the context,
    and that SOUL.md verifiably ships in the image.
    """
    from fnmatch import fnmatchcase

    p_parts, f_parts = pat.split("/"), path.split("/")
    if p_parts and p_parts[-1] == "**":
        p_parts = p_parts[:-1]
        if len(f_parts) < len(p_parts):
            return False
        f_parts = f_parts[: len(p_parts)]
    elif len(f_parts) < len(p_parts):
        return False
    else:
        # A directory pattern also covers everything beneath it.
        f_parts = f_parts[: len(p_parts)]
    return len(p_parts) == len(f_parts) and all(
        fnmatchcase(f, p) for f, p in zip(f_parts, p_parts)
    )


def _is_excluded(path: str, excluded: list[str], reincluded: list[str]) -> bool:
    """Approximate Docker's matching: an explicit ``!`` re-include wins."""
    return any(_matches_pattern(path, p) for p in excluded) and not any(
        _matches_pattern(path, p) for p in reincluded
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ref", default="upstream/main",
                    help="upstream ref to compare against (default: upstream/main)")
    ap.add_argument("--head", default="HEAD", help="our ref (default: HEAD)")
    args = ap.parse_args()

    try:
        theirs = _tracked_at(args.ref)
        ours = _tracked_at(args.head)
    except subprocess.CalledProcessError:
        print(
            f"error: cannot read {args.ref!r}. Fetch it first:\n"
            f"  git remote add upstream https://github.com/NousResearch/hermes-agent.git\n"
            f"  git fetch upstream --tags",
            file=sys.stderr,
        )
        return 2

    only_ours = ours - theirs
    # A directory we both populate is the collision that matters: upstream's
    # directory-level rules (.dockerignore, packaging globs, docs builds) will
    # apply to OUR files inside it without anyone noticing.
    their_dirs = _top_dirs(theirs)
    shared: dict[str, list[str]] = defaultdict(list)
    for p in sorted(only_ours):
        if "/" in p and p.split("/", 1)[0] in their_dirs:
            shared[p.split("/", 1)[0]].append(p)

    excluded, reincluded = _dockerignore_rules()
    # Directories excluded from the image ON PURPOSE, holding nothing the
    # runtime reads. Flagging these buries the one finding that matters.
    BY_DESIGN = ("tests/", "docs/", "website/", ".github/", "plans/", ".plans/",
                 "packaging/", "nix/", "acp_registry/", "assets/")
    at_risk = [
        p for files in shared.values() for p in files
        if _is_excluded(p, excluded, reincluded) and not p.startswith(BY_DESIGN)
    ]

    if at_risk:
        print(
            f"HIGH RISK — {len(at_risk)} of our file(s) sit in a directory that "
            f".dockerignore EXCLUDES.\nThey are in git but NOT in the image. If "
            f"anything reads them at runtime it breaks in prod only:\n"
        )
        for p in sorted(at_risk):
            print(f"    {p}")
        print(
            "\n  Fix by re-including the specific file (e.g. `!infographic/*.prompt`)\n"
            "  or moving it to a top-level name upstream does not own."
        )
    else:
        print("OK — no file of ours is hidden by an upstream .dockerignore rule.")

    total = sum(len(v) for v in shared.values())
    print(
        f"\nFYI — {total} file(s) of ours live inside {len(shared)} directories "
        f"upstream also owns:\n  "
        + ", ".join(f"{d}/ ({len(f)})" for d, f in
                    sorted(shared.items(), key=lambda kv: -len(kv[1])))
    )
    print(
        "  Mostly benign (tests/, docker/, plugins/ are normal places to add).\n"
        "  The risk is upstream applying a DIRECTORY-level rule to them without a\n"
        "  merge conflict, or adding a file with the same name (add/add)."
    )
    return 1 if at_risk else 0


if __name__ == "__main__":
    raise SystemExit(main())
